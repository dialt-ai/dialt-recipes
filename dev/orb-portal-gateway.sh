#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIALT_ROOT="${DIALT_ROOT:-$(dirname "$REPO_ROOT")/repos/dialt}"

test -x "$DIALT_ROOT/dev/orb-portal-gateway.sh" || {
  echo "Dialt must be configured as an additional repository at $DIALT_ROOT" >&2
  exit 1
}

export CONVERSE_DEV_STACK_PORT="${DIALT_STACK_PORT:?DIALT_STACK_PORT is required}"
if [ -n "${DIALT_API_ORIGIN:-}" ]; then
  export CONVERSE_DEV_API_ORIGIN="$DIALT_API_ORIGIN"
fi

exec "$DIALT_ROOT/dev/orb-portal-gateway.sh"
