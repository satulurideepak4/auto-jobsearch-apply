@echo off
REM ============================================================
REM auto-jobsearch-apply — Windows setup script
REM
REM What this does:
REM   1. Checks Python 3.11+ is installed
REM   2. Checks Docker Desktop is running
REM   3. Starts PostgreSQL container via Docker Compose
REM   4. Creates a Python virtual environment
REM   5. Installs all Python dependencies
REM   6. Installs Playwright Chromium browser
REM   7. Creates .env from .env.example if it doesn't exist
REM
REM After this runs: edit .env, then run run.bat
REM
REM NOTE: Run this script as a regular user (not Administrator).
REM       Right-click Command Prompt → "Run as administrator" is NOT needed.
REM ============================================================

setlocal enabledelayedexpansion

echo.
echo  ============================================================
echo   auto-jobsearch-apply  ^|  Windows Setup
echo  ============================================================
echo.

REM ── Step 1: Check Python version ─────────────────────────────
echo [1/7] Checking Python version...

python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python is not installed or not in PATH.
    echo         Download Python 3.11+ from: https://www.python.org/downloads/
    echo         During install: check "Add Python to PATH"
    pause
    exit /b 1
)

REM Check version is 3.11+
python -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python 3.11 or newer is required.
    for /f "tokens=*" %%i in ('python --version 2^>^&1') do echo         Found: %%i
    echo         Download Python 3.11+ from: https://www.python.org/downloads/
    pause
    exit /b 1
)

for /f "tokens=*" %%i in ('python --version 2^>^&1') do echo [OK]    Using %%i

REM ── Step 2: Check Docker ──────────────────────────────────────
echo.
echo [2/7] Checking Docker...

docker --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Docker is not installed.
    echo         Install Docker Desktop from: https://www.docker.com/products/docker-desktop/
    pause
    exit /b 1
)

docker info >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Docker Desktop is not running.
    echo         Please start Docker Desktop and run this script again.
    pause
    exit /b 1
)
echo [OK]    Docker is running

REM ── Step 3: Start PostgreSQL ──────────────────────────────────
echo.
echo [3/7] Starting PostgreSQL via Docker Compose...

if not exist "docker-compose.yml" (
    echo [ERROR] docker-compose.yml not found.
    echo         Make sure you are running this script from the project root.
    pause
    exit /b 1
)

docker compose up -d
if %errorlevel% neq 0 (
    echo [ERROR] docker compose up failed. Check the error above.
    pause
    exit /b 1
)
echo [OK]    PostgreSQL container is up ^(port 5435^)

REM Wait for Postgres to be ready
echo [INFO]  Waiting for PostgreSQL to be ready...
set RETRIES=20
:pg_wait
docker compose exec -T db pg_isready -U postgres >nul 2>&1
if %errorlevel% equ 0 goto pg_ready
set /a RETRIES-=1
if %RETRIES% equ 0 (
    echo [ERROR] PostgreSQL did not become ready. Run: docker compose logs db
    pause
    exit /b 1
)
timeout /t 1 /nobreak >nul
goto pg_wait
:pg_ready
echo [OK]    PostgreSQL is accepting connections

REM ── Step 4: Create virtual environment ───────────────────────
echo.
echo [4/7] Creating Python virtual environment...

if not exist ".venv" (
    python -m venv .venv
    echo [OK]    Virtual environment created at .venv
) else (
    echo [OK]    Virtual environment already exists at .venv
)

REM ── Step 5: Install dependencies ─────────────────────────────
echo.
echo [5/7] Installing Python dependencies...

call .venv\Scripts\activate.bat

python -m pip install --upgrade pip --quiet
if %errorlevel% neq 0 (
    echo [ERROR] pip upgrade failed.
    pause
    exit /b 1
)

pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo [ERROR] Dependency installation failed. See error above.
    pause
    exit /b 1
)
echo [OK]    Python dependencies installed

REM ── Step 6: Install Playwright browser ───────────────────────
echo.
echo [6/7] Installing Playwright Chromium browser...

playwright install chromium
if %errorlevel% neq 0 (
    echo [ERROR] Playwright browser install failed.
    pause
    exit /b 1
)
echo [OK]    Playwright Chromium installed

REM ── Step 7: Create .env ───────────────────────────────────────
echo.
echo [7/7] Checking .env configuration...

if not exist ".env" (
    if exist ".env.example" (
        copy ".env.example" ".env" >nul
        echo [WARN]  .env created from .env.example
        echo [WARN]  IMPORTANT: Edit .env and fill in your credentials before running.
    ) else (
        echo [WARN]  .env.example not found. Create .env manually.
    )
) else (
    echo [OK]    .env already exists
)

REM ── Done ─────────────────────────────────────────────────────
echo.
echo  ============================================================
echo   Setup complete!
echo  ============================================================
echo.
echo   Next steps:
echo     1. Edit .env and fill in your API keys and credentials
echo        ^(LLM_PROVIDER, API keys, applicant info, etc.^)
echo.
echo     2. If using Gemini ^(Vertex AI^):
echo        - Place your service account key at vertex-ai-credentials.json
echo        - Set GCP_PROJECT_ID in .env
echo.
echo     3. IMPORTANT - Windows limitation:
echo        Portal login ^(Naukri/Instahyre/Wellfound^) reads cookies from
echo        Chrome on macOS. On Windows this feature is not available.
echo        You can still use all other features ^(scraping, apply, etc.^)
echo.
echo     4. Start the app:
echo        run.bat
echo.
echo   Dashboard will open at: http://localhost:8000
echo.
pause
endlocal
