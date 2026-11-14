#!/usr/bin/env bash
set -euo pipefail
APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v tesseract >/dev/null 2>&1; then
  echo "Installing Tesseract OCR and Qt runtime libraries..."
  sudo apt update
  sudo apt install -y tesseract-ocr libxcb-cursor0 libxkbcommon-x11-0 libxcb-xinerama0 libegl1 libgl1
else
  sudo apt install -y libxcb-cursor0 libxkbcommon-x11-0 libxcb-xinerama0 libegl1 libgl1
fi

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/python" -m pip install --upgrade pip
"$APP_DIR/.venv/bin/python" -m pip install -r "$APP_DIR/requirements.txt"

# Download the official spaCy English large pipeline once. It is installed
# locally and runs on-device. If the model download is unavailable, the app
# still opens with OCR/identifier-pattern detection, but no general NER.
if ! "$APP_DIR/.venv/bin/python" -c 'import en_core_web_lg' >/dev/null 2>&1; then
  echo
  echo "Installing the local PII NER model (spaCy en_core_web_lg)..."
  if ! "$APP_DIR/.venv/bin/python" -m spacy download en_core_web_lg; then
    echo "WARNING: the spaCy model could not be downloaded. Local OCR and identifier patterns will still work."
    echo "After connecting to the internet, run:"
    echo "  $APP_DIR/.venv/bin/python -m spacy download en_core_web_lg"
  fi
fi

chmod +x "$APP_DIR/run.sh"
DESKTOP_DIR="$HOME/.local/share/applications"
mkdir -p "$DESKTOP_DIR"
cat > "$DESKTOP_DIR/local-redactor.desktop" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=Local Redactor
Comment=Offline document and screenshot privacy redaction
Exec="$APP_DIR/run.sh"
Icon=$APP_DIR/icon.svg
Terminal=false
Categories=Utility;Graphics;Office;
StartupNotify=true
EOF
chmod 644 "$DESKTOP_DIR/local-redactor.desktop"
update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true

echo
echo "Installed. Launch Local Redactor from the Ubuntu application menu, or run:"
echo "  $APP_DIR/run.sh"
echo
echo "The first install downloads dependencies and the local PII model."
echo "Document processing itself runs on this machine and makes no network requests."
