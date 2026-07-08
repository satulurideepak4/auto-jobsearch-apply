#!/usr/bin/env bash
set -euo pipefail

VENV_DIR=".venv"

if [ ! -f ".env" ]; then
  echo "ERROR: .env file not found."
  echo "Copy .env.example to .env and fill in your values first."
  exit 1
fi

source "$VENV_DIR/bin/activate"

if [ "${1:-}" = "--scrape-now" ]; then
  echo "Running one-off scrape pipeline..."
  python -c "
import asyncio
from app.scheduler import run_nightly_pipeline
from app.storage.db import init_db
async def main():
    await init_db()
    await run_nightly_pipeline()
asyncio.run(main())
"
else
  echo "Starting job-agent server..."
  uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
fi
