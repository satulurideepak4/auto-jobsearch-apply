@echo off
REM ============================================================
REM auto-jobsearch-apply — start the server (Windows)
REM
REM Usage:
REM   run.bat               — start the web server on :8000
REM   run.bat --scrape-now  — run a one-off scrape pipeline now
REM
REM Prerequisites: run requirements.bat once first.
REM ============================================================

setlocal enabledelayedexpansion

REM ── Sanity checks ────────────────────────────────────────────
if not exist ".env" (
    echo [ERROR] .env file not found.
    echo         Copy .env.example to .env and fill in your credentials first:
    echo           copy .env.example .env
    pause
    exit /b 1
)

if not exist ".venv" (
    echo [ERROR] Virtual environment not found at .venv
    echo         Run requirements.bat first to set up the environment.
    pause
    exit /b 1
)

REM ── Ensure PostgreSQL is running ─────────────────────────────
docker info >nul 2>&1
if %errorlevel% equ 0 (
    docker compose exec -T db pg_isready -U postgres >nul 2>&1
    if %errorlevel% neq 0 (
        echo [INFO] PostgreSQL is not running - starting it now...
        docker compose up -d
        timeout /t 3 /nobreak >nul
    )
)

REM ── Activate virtual environment ─────────────────────────────
call .venv\Scripts\activate.bat

REM ── Run ──────────────────────────────────────────────────────
if "%1"=="--scrape-now" (
    echo [INFO] Running one-off scrape pipeline...
    python -c "import asyncio; from app.scheduler import run_nightly_pipeline; from app.storage.db import init_db; asyncio.run((lambda: (init_db(), run_nightly_pipeline()))())"
) else (
    echo [INFO] Starting job-agent server...
    echo [INFO] Dashboard: http://localhost:8000
    echo [INFO] Press Ctrl+C to stop.
    echo.
    uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
)

endlocal
