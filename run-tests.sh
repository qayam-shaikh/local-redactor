#!/usr/bin/env bash
set -euo pipefail
APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
  echo "Install first with ./install.sh"
  exit 1
fi
cd "$APP_DIR"
exec "$APP_DIR/.venv/bin/python" -m unittest discover -s tests -v
