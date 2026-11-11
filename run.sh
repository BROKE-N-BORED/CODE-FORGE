#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd -- "$ROOT"
if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
  echo "Environment missing or incomplete. Run ./install.sh first." >&2
  exit 1
fi
exec "$ROOT/.venv/bin/python" -m forge.main
