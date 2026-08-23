"""Apply revision c4d7e8f9a0b1 (the v2.2.2 bundle) that alembic believes already ran.

Why this is needed
------------------
Upstream re-issued the SAME revision id with more DDL folded in, and our ACR
overlay rewrites our first migration's down_revision onto the base image's
alembic head. Every base image so far reported head b2c3d4e5f6a7, so the
production chain was

    ... -> b2c3d4e5f6a7 -> e1f2a3b4c5d6 -> f3a1b2c4d5e7

and c4d7e8f9a0b1 never ran. The v2.2.3 base puts it back in the chain, but the
DB is already stamped at the head, so `alembic upgrade head` computes
current == head, runs nothing and exits 0 — alembic never checks that ancestors
actually ran.

Why it does NOT go through `alembic stamp`
------------------------------------------
The obvious fix (stamp back -> upgrade -> stamp forward) is not crash safe: each
stamp commits in its own transaction, so an interruption between them leaves
alembic_version pointing BEHIND reality. The server then tries to re-run our
migrations on next start, hits CREATE TABLE on an existing table, and refuses to
boot. That happened on 2026-08-23.

Here the migration body runs inside one transaction and alembic_version is never
written. The revision is already an ancestor of the recorded head, so leaving the
version untouched is exactly right. Either the DDL commits or the database is
byte-identical to before — there is no in-between state to clean up.

Locking
-------
The bundle needs ACCESS EXCLUSIVE on principals (ALTER COLUMN TYPE) and on
model_usages (DROP CONSTRAINT). A queued ACCESS EXCLUSIVE request blocks every
later reader of those tables too, so if anything holds a long-lived transaction
the whole server stalls behind it. Stop the app services first (postgres can stay
up), and keep lock_timeout as the backstop: fail in seconds instead of taking the
control plane down.

Usage:
    DATABASE_URL=postgresql://root@127.0.0.1:5432/gpustack python3 apply-missing-bundle.py
    ... --check      report only, change nothing
"""

import importlib.util
import os
import sys

import sqlalchemy as sa
from alembic.config import Config
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

REVISION = "c4d7e8f9a0b1"
LOCK_TIMEOUT = os.environ.get("LOCK_TIMEOUT", "5s")

# Schema markers proving the bundle ran. (label, sql returning one value, expected)
MARKERS = [
    (
        "model_usages.consumer_name",
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_name='model_usages' AND column_name='consumer_name'",
        "1",
    ),
    (
        "model_usages.consumer_principal_kind",
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_name='model_usages' AND column_name='consumer_principal_kind'",
        "1",
    ),
    (
        "metered_usage.consumer_principal_kind",
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_name='metered_usage' AND column_name='consumer_principal_kind'",
        "1",
    ),
    (
        "gpu_instance_persistent_volumes.status",
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_name='gpu_instance_persistent_volumes' AND column_name='status'",
        "1",
    ),
    (
        "api_keys.owner_principal_id is nullable",
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_name='api_keys' AND column_name='owner_principal_id'",
        "YES",
    ),
    (
        "principals.source is not an enum",
        "SELECT udt_name FROM information_schema.columns "
        "WHERE table_name='principals' AND column_name='source'",
        "varchar",
    ),
]


def report(conn):
    ok = True
    for label, sql, expected in MARKERS:
        actual = conn.execute(sa.text(sql)).scalar()
        hit = str(actual) == expected
        ok = ok and hit
        print(f"  [{'ok' if hit else '--'}] {label}: {actual}")
    return ok


def main() -> int:
    check_only = "--check" in sys.argv
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL must be set", file=sys.stderr)
        return 2

    pkg = importlib.util.find_spec("gpustack").submodule_search_locations[0]
    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(pkg, "migrations"))
    revision = ScriptDirectory.from_config(cfg).get_revision(REVISION)

    engine = sa.create_engine(url.replace("+asyncpg", "").replace("+psycopg", ""))

    with engine.connect() as conn:
        version = conn.execute(
            sa.text("SELECT version_num FROM alembic_version")
        ).scalar()
        print(f"alembic_version = {version}")
        print(f"{REVISION} markers:")
        already = report(conn)

    if already:
        print(f"\nOK: {REVISION} is already applied — nothing to do.")
        return 0
    if check_only:
        print(f"\nDRIFT: {REVISION} is recorded as passed but its DDL is absent.")
        print("Re-run without --check to apply it. Stop the app services first.")
        return 2

    print(
        f"\nApplying {REVISION} in a single transaction (lock_timeout={LOCK_TIMEOUT})..."
    )
    with engine.begin() as conn:
        conn.exec_driver_sql(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            revision.module.upgrade()

    with engine.connect() as conn:
        version_after = conn.execute(
            sa.text("SELECT version_num FROM alembic_version")
        ).scalar()
        print(
            f"\nalembic_version = {version_after} (untouched: {version_after == version})"
        )
        print(f"{REVISION} markers:")
        if not report(conn):
            print("\nFAILED: markers still absent after apply.", file=sys.stderr)
            return 1

    print("\nAPPLIED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
