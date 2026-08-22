#!/usr/bin/env bash
#
# Assert pack/Dockerfile.acr's COPY list matches the files this fork actually
# changed on top of its upstream base.
#
# The overlay whole-file overwrites the base image's modules. A file we changed
# but forgot to list never reaches production (that is how the
# common_parameters backfill in 2b524710 sat dead from 2026-07-24 until the
# v2.2.3 merge), and a file we list but no longer change silently reverts any
# later upstream fix to it.
#
# UPSTREAM_BASE must name the upstream release this repo is merged up to, and
# must be the same tag BASE_IMAGE is pinned to in pack/Dockerfile.acr.
#
# Usage: hack/check-overlay-list.sh [upstream-ref]
set -euo pipefail

cd "$(dirname "$0")/.."

UPSTREAM_BASE="${1:-$(sed -n 's/^UPSTREAM_BASE=//p' hack/upstream-base.env)}"

if ! git rev-parse --verify -q "${UPSTREAM_BASE}^{commit}" >/dev/null; then
  echo "FATAL: upstream ref '${UPSTREAM_BASE}' not found. Fetch it first:" >&2
  echo "  git fetch https://github.com/gpustack/gpustack.git --tags" >&2
  exit 1
fi

# Deletions are excluded: a COPY cannot express "remove this module", so a file
# we delete relative to upstream is a separate problem the overlay can't solve —
# call it out on its own rather than demanding an impossible COPY line.
deleted="$(git diff --name-only --diff-filter=D "${UPSTREAM_BASE}" -- gpustack static | sort -u)"
changed="$(git diff --name-only --diff-filter=ACMRT "${UPSTREAM_BASE}" -- gpustack static | sort -u)"
# [^[:space:]] not \S: \S is a GNU grep extension, and BSD grep would silently
# match nothing here and report every file as missing.
listed="$(grep -oE '^COPY[[:space:]]+(gpustack|static)/[^[:space:]]+' pack/Dockerfile.acr |
  awk '{print $2}' | sort -u)"

# printf, not echo: echo "" emits a blank line that comm would treat as a member.
missing="$(comm -23 <(printf '%s' "${changed}") <(printf '%s' "${listed}"))"
stale="$(comm -13 <(printf '%s' "${changed}") <(printf '%s' "${listed}"))"

rc=0
if [ -n "${deleted}" ]; then
  echo "FAIL: deleted vs ${UPSTREAM_BASE}; the overlay cannot delete files from the base image:" >&2
  while IFS= read -r f; do echo "  - ${f}" >&2; done <<<"${deleted}"
  rc=1
fi
if [ -n "${missing}" ]; then
  echo "FAIL: changed vs ${UPSTREAM_BASE} but NOT in pack/Dockerfile.acr — these would ship as the base image's version:" >&2
  while IFS= read -r f; do echo "  - ${f}" >&2; done <<<"${missing}"
  rc=1
fi
if [ -n "${stale}" ]; then
  echo "FAIL: listed in pack/Dockerfile.acr but identical to ${UPSTREAM_BASE} — drop them so upstream fixes are not reverted:" >&2
  while IFS= read -r f; do echo "  - ${f}" >&2; done <<<"${stale}"
  rc=1
fi
if [ "${rc}" -eq 0 ]; then
  # grep -c, not wc -l: an empty ${changed} would still count as one line.
  echo "OK: overlay COPY list matches the $(printf '%s' "${changed}" | grep -c '' || true) files changed vs ${UPSTREAM_BASE}."
fi
exit "${rc}"
