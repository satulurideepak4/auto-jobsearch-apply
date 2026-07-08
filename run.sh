#!/usr/bin/env bash
# ============================================================
# auto-jobsearch-apply — start the server (macOS / Linux)
#
# Usage:
#   ./run.sh               — start the web server on :8000
#   ./run.sh --scrape-now  — run a one-off scrape pipeline now
#
# Prerequisites: run ./requirements.sh once first.
# ============================================================
set -euo pipefail

VENV_DIR=".venv"

# ── Sanity checks ────────────────────────────────────────────
if [ ! -f ".env" ]; then
  echo "ERROR: .env file not found."
  echo "  Copy .env.example to .env and fill in your credentials first:"
  echo "    cp .env.example .env"
  exit 1
fi

if [ ! -d "$VENV_DIR" ]; then
  echo "ERROR: Virtual environment not found at $VENV_DIR"
  echo "  Run ./requirements.sh first to set up the environment."
  exit 1
fi

# ── Ensure PostgreSQL is running ─────────────────────────────
if command -v docker &>/dev/null && docker info &>/dev/null 2>&1; then
  if ! docker compose exec -T db pg_isready -U postgres &>/dev/null 2>&1; then
    echo "[INFO] PostgreSQL is not running — starting it now..."
    docker compose up -d
    sleep 2
  fi
fi

# ── Activate virtual environment ─────────────────────────────
source "$VENV_DIR/bin/activate"

# ── Run ──────────────────────────────────────────────────────
if [ "${1:-}" = "--scrape-now" ]; then
  echo "[INFO] Running one-off scrape pipeline..."
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
  echo "[INFO] Starting job-agent server..."
  echo "[INFO] Dashboard: http://localhost:8000"
  echo "[INFO] Press Ctrl+C to stop."
  echo ""
  uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
fi
