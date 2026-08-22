#!/usr/bin/env bash
#
# Pre-upgrade check for the silent schema drift that `alembic upgrade head`
# cannot see.
#
# Why this exists
# ---------------
# Upstream folds several pre-release schema changes into ONE revision id and
# keeps that id stable: `c4d7e8f9a0b1` shipped as "principal.source enum ->
# VARCHAR" when this fork branched, then upstream re-issued the SAME revision id
# as the six-part "v2.2.2 database changes" bundle.
#
# Our ACR overlay rewrites our first migration's down_revision to the base
# image's alembic head at build time, so a deployment whose base image predated
# `c4d7e8f9a0b1` has a chain that skips straight past it. After the base image
# moves to v2.2.3 the chain contains it again — but the DB is already stamped at
# our head, so alembic computes "current == head", runs NOTHING, reports success,
# and the bundle's DDL is never applied.
#
# The failure then surfaces at runtime, not at upgrade time: v2.2.3's usage read
# path selects model_usages.consumer_name / consumer_principal_kind, which do not
# exist. Verified end-to-end against PostgreSQL 17 on 2026-08-23.
#
# Usage:
#   DATABASE_URL=postgresql://user:pass@host:5432/gpustack hack/preflight-upstream-db.sh
#   DATABASE_URL=...                                       hack/preflight-upstream-db.sh --fix
#
# Read-only without --fix. With --fix it runs, inside one alembic invocation each:
#   alembic stamp <newest fully-applied upstream revision>
#   alembic upgrade c4d7e8f9a0b1      # applies ONLY the missing bundle
#   alembic stamp <the head it was at>
# `stamp` only rewrites alembic_version, so our own migrations never re-run and
# video_generation_tasks (and its rows) are left untouched.
set -euo pipefail

cd "$(dirname "$0")/.."

FIX=false
[ "${1:-}" = "--fix" ] && FIX=true

: "${DATABASE_URL:?DATABASE_URL must be set, e.g. postgresql://user:pass@host:5432/gpustack}"

# The bundle whose DDL goes missing, and the marker that proves it ran.
BUNDLE_REV=c4d7e8f9a0b1
BUNDLE_MARKER="SELECT count(*) FROM information_schema.columns WHERE table_name='model_usages' AND column_name='consumer_name'"

q() { uv run python -c "
import sys, sqlalchemy as sa
e = sa.create_engine(sys.argv[1].replace('+asyncpg','').replace('+psycopg',''))
with e.connect() as c:
    r = c.execute(sa.text(sys.argv[2])).fetchall()
    print('\n'.join(str(x[0]) for x in r) if r else '')
" "${DATABASE_URL}" "$1"; }

current="$(q 'SELECT version_num FROM alembic_version' || true)"
if [ -z "${current}" ]; then
  echo "FATAL: no alembic_version row — this does not look like a GPUStack database." >&2
  exit 1
fi
echo "alembic_version = ${current}"

if [ "$(q "${BUNDLE_MARKER}")" = "1" ]; then
  echo "OK: revision ${BUNDLE_REV} (v2.2.2 bundle) is applied — no drift, upgrade normally."
  exit 0
fi

echo
echo "DRIFT: revision ${BUNDLE_REV} is recorded as passed but its DDL is absent."
echo "Missing (v2.2.3 code reads these):"
for chk in \
  "model_usages.consumer_name|SELECT count(*) FROM information_schema.columns WHERE table_name='model_usages' AND column_name='consumer_name'" \
  "model_usages.consumer_principal_kind|SELECT count(*) FROM information_schema.columns WHERE table_name='model_usages' AND column_name='consumer_principal_kind'" \
  "metered_usage.consumer_principal_kind|SELECT count(*) FROM information_schema.columns WHERE table_name='metered_usage' AND column_name='consumer_principal_kind'" \
  "gpu_instance_persistent_volumes.status|SELECT count(*) FROM information_schema.columns WHERE table_name='gpu_instance_persistent_volumes' AND column_name='status'" \
  ; do
  name="${chk%%|*}"; sql="${chk#*|}"
  [ "$(q "${sql}")" = "0" ] && echo "  - ${name}"
done
if [ "$(q "SELECT is_nullable FROM information_schema.columns WHERE table_name='api_keys' AND column_name='owner_principal_id'")" = "NO" ]; then
  echo "  - api_keys.owner_principal_id is still NOT NULL"
fi

# Stamp target: the newest upstream revision whose DDL is actually present.
# Ordered oldest -> newest; the last one that exists wins.
target=""
for pair in \
  "7c5e3f9a2d18|SELECT count(*) FROM information_schema.tables WHERE table_name='principals'" \
  "61929acb0676|SELECT count(*) FROM information_schema.tables WHERE table_name='gpu_instances'" \
  "b2c3d4e5f6a7|SELECT count(*) FROM information_schema.tables WHERE table_name='metered_usage'" \
  ; do
  rev="${pair%%|*}"; sql="${pair#*|}"
  [ "$(q "${sql}")" = "1" ] && target="${rev}"
done
if [ -z "${target}" ]; then
  echo >&2
  echo "FATAL: could not identify a safe stamp target — the database is older than expected." >&2
  echo "Do not --fix; investigate by hand." >&2
  exit 1
fi

echo
echo "Remediation (stamp only rewrites alembic_version; no table is dropped):"
echo "  alembic stamp ${target}"
echo "  alembic upgrade ${BUNDLE_REV}"
echo "  alembic stamp ${current}"

if [ "${FIX}" != true ]; then
  echo
  echo "Re-run with --fix to apply. Take a database backup first."
  exit 2
fi

echo
echo "Applying..."
uv run alembic stamp "${target}"
uv run alembic upgrade "${BUNDLE_REV}"
uv run alembic stamp "${current}"

if [ "$(q "${BUNDLE_MARKER}")" = "1" ]; then
  echo "OK: ${BUNDLE_REV} applied; alembic_version restored to $(q 'SELECT version_num FROM alembic_version')."
else
  echo "FAILED: the bundle marker is still absent after the fix." >&2
  exit 1
fi
