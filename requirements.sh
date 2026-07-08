#!/usr/bin/env bash
set -euo pipefail

VENV_DIR=".venv"

if [ ! -d "$VENV_DIR" ]; then
  echo "Creating virtual environment..."
  /opt/homebrew/bin/python3.14 -m venv "$VENV_DIR"
fi

echo "Activating virtual environment..."
source "$VENV_DIR/bin/activate"

echo "Installing Python dependencies..."
pip3 install --upgrade pip
pip3 install -r requirements.txt

echo "Installing Playwright browsers..."
playwright install chromium

echo ""
echo "✓ Setup complete. Next steps:"
echo "  1. Copy .env.example to .env and fill in your values"
echo "  2. Run: ./run.sh"
