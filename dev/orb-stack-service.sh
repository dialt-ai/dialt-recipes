#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIALT_ROOT="${DIALT_ROOT:-$(dirname "$REPO_ROOT")/repos/dialt}"
STACK="$DIALT_ROOT/dev/stack.sh"

test -x "$STACK" || {
  echo "Dialt must be configured as an additional repository at $DIALT_ROOT" >&2
  exit 1
}
: "${PORT:?Amp must supply PORT}"
: "${DIALT_PUBLIC_BASE_URL:?browser portal URL is required}"
: "${DIALT_PUBLIC_API_URL:?API portal URL is required}"

inner_port="${DIALT_INNER_STACK_PORT:-24110}"
export CONVERSE_DEV_PORT_BASE="$inner_port"
export CONVERSE_DEV_PUBLIC_BASE_URL="$DIALT_PUBLIC_BASE_URL"
export CONVERSE_DEV_PUBLIC_API_URL="$DIALT_PUBLIC_API_URL"

run_stack() {
  (cd "$DIALT_ROOT" && "$STACK" "$@")
}

cleanup() {
  run_stack down || true
}
trap cleanup EXIT INT TERM

run_stack up --fake=all

stack_env=""
for checkout_file in "${XDG_STATE_HOME:-$HOME/.local/state}/converse/dev-stack"/*/checkout; do
  if [ -f "$checkout_file" ] && [ "$(cat "$checkout_file")" = "$DIALT_ROOT" ]; then
    stack_env="$(dirname "$checkout_file")/stack.env"
    break
  fi
done
test -f "$stack_env" || {
  echo "Could not find the Dialt stack environment" >&2
  exit 1
}

set -a
# shellcheck disable=SC1090
source "$stack_env"
set +a
export DIALT_RECIPES_ENV="$REPO_ROOT/.env"
(
  cd "$DIALT_ROOT"
  uv run --quiet python - <<'PY'
import os
from pathlib import Path

from converse_store import billing

path = Path(os.environ["DIALT_RECIPES_ENV"])
lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
values = dict(line.split("=", 1) for line in lines if "=" in line and not line.lstrip().startswith("#"))
key = values.get("DIALT_API_KEY", "").strip()
if billing.resolve_key(key) != "demo@dialt.com":
    billing.grant_signup("demo@dialt.com", "orb recipes")
    key = billing.issue_key("demo@dialt.com", "Amp orb recipes")

updates = {
    "DIALT_API_KEY": key,
    "DIALT_URL": "ws://127.0.0.1:24100/v1/realtime",
    "DIALT_EVALS_URL": "http://127.0.0.1:24100",
}
written = set()
output = []
for line in lines:
    name = line.split("=", 1)[0] if "=" in line else ""
    if name in updates:
        if name not in written:
            output.append(f"{name}={updates[name]}")
            written.add(name)
    else:
        output.append(line)
for name, value in updates.items():
    if name not in written:
        output.append(f"{name}={value}")
path.write_text("\n".join(output) + "\n", encoding="utf-8")
PY
)
echo "Configured $REPO_ROOT/.env for the local Dialt stack"

run_stack health

state_dir="${XDG_STATE_HOME:-$HOME/.local/state}/dialt-recipes"
config="$state_dir/orb-stack.Caddyfile"
mkdir -p "$state_dir"
cat > "$config" <<EOF
{
	admin off
	auto_https off
}

:$PORT {
	@local_machine {
		host 127.0.0.1 api.localhost
		path /v1/* /internal/v1 /internal/v1/*
	}
	handle @local_machine {
		reverse_proxy 127.0.0.1:$inner_port {
			header_up Host api.localhost:$inner_port
		}
	}
	@local_browser host localhost
	handle @local_browser {
		reverse_proxy 127.0.0.1:$inner_port {
			header_up Host localhost:$inner_port
		}
	}
	handle {
		reverse_proxy 127.0.0.1:$inner_port
	}
}
EOF

# Bind the Amp service port only after every inner-stack component and the recipe credential are
# ready. This keeps Amp's HTTP readiness check from racing the broker's model warmup.
exec caddy run --config "$config" --adapter caddyfile
