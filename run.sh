#!/usr/bin/env bash
set -euo pipefail
APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
  echo "Local Redactor is not installed yet. Run: ./install.sh"
  exit 1
fi
export QT_AUTO_SCREEN_SCALE_FACTOR=1
exec "$APP_DIR/.venv/bin/python" "$APP_DIR/main.py" "$@"
