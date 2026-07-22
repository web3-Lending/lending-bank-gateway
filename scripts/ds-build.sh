#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

cmd="${1:-plan}"
case "$cmd" in
  plan|status)
    python3 scripts/buildops.py "$cmd"
    ;;
  local-verify)
    shift || true
    python3 scripts/buildops.py local-verify "$@"
    ;;
  verify)
    shift || true
    python3 scripts/buildops.py verify "$@"
    ;;
  dev-promote)
    shift || true
    python3 scripts/buildops.py dev-promote "$@"
    ;;
  *)
    echo "unknown ds-build command: $cmd" >&2
    echo "usage: scripts/ds-build.sh plan|status|local-verify|verify|dev-promote" >&2
    exit 2
    ;;
esac
