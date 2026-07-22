#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

mode="dry-run"
manifest=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env)
      [[ "${2:-}" == "uat" ]] || { echo "only --env uat is supported" >&2; exit 2; }
      shift 2
      ;;
    --dry-run)
      mode="dry-run"
      shift
      ;;
    --confirm)
      mode="confirm"
      shift
      ;;
    --manifest|--release-manifest)
      manifest="${2:-}"
      shift 2
      ;;
    --version)
      shift 2
      ;;
    *)
      echo "unknown uat-deploy argument: $1" >&2
      exit 2
      ;;
  esac
done

if [[ "$mode" == "dry-run" ]]; then
  if [[ -n "$manifest" ]]; then
    python3 scripts/buildops.py uat-dry-run --manifest "$manifest"
  else
    python3 scripts/buildops.py uat-dry-run
  fi
else
  if [[ -n "$manifest" ]]; then
    python3 scripts/buildops.py uat-confirm --manifest "$manifest"
  else
    python3 scripts/buildops.py uat-confirm
  fi
fi
