#!/usr/bin/env bash
# ============================================================
# auto-jobsearch-apply — macOS / Linux setup script
#
# What this does:
#   1. Checks system requirements (Python 3.11+, Docker)
#   2. Starts the PostgreSQL container via Docker Compose
#   3. Creates a Python virtual environment
#   4. Installs all Python dependencies
#   5. Installs Playwright browser (Chromium)
#   6. Creates .env from .env.example if it doesn't exist
#
# After this runs: edit .env, then run ./run.sh
# ============================================================
set -euo pipefail

# ── Colour helpers ───────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'
info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }
step()    { echo -e "\n${BOLD}── $* ──${NC}"; }

# ── Step 1: Check Python version ─────────────────────────────
step "Checking Python version"

PYTHON=""
for cmd in python3.13 python3.12 python3.11 python3 python; do
  if command -v "$cmd" &>/dev/null; then
    VER=$("$cmd" -c 'import sys; print(sys.version_info[:2])')
    if "$cmd" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
      PYTHON="$cmd"
      break
    fi
  fi
done

if [ -z "$PYTHON" ]; then
  error "Python 3.11 or newer is required but was not found.\n  Install it from https://www.python.org/downloads/ or via your package manager."
fi

PY_VERSION=$("$PYTHON" --version 2>&1)
success "Using $PYTHON ($PY_VERSION)"

# ── Step 2: Check Docker ──────────────────────────────────────
step "Checking Docker"

if ! command -v docker &>/dev/null; then
  error "Docker is not installed. Install Docker Desktop from https://www.docker.com/products/docker-desktop/"
fi

if ! docker info &>/dev/null; then
  error "Docker daemon is not running. Please start Docker Desktop and try again."
fi
success "Docker is running"

# ── Step 3: Start PostgreSQL via Docker Compose ───────────────
step "Starting PostgreSQL (Docker Compose)"

if [ ! -f "docker-compose.yml" ]; then
  error "docker-compose.yml not found. Make sure you are in the project root directory."
fi

docker compose up -d
success "PostgreSQL container is up (port 5435)"

# Wait for PostgreSQL to be ready
info "Waiting for PostgreSQL to be ready..."
RETRIES=20
for i in $(seq 1 $RETRIES); do
  if docker compose exec -T db pg_isready -U postgres &>/dev/null; then
    success "PostgreSQL is accepting connections"
    break
  fi
  if [ "$i" -eq "$RETRIES" ]; then
    error "PostgreSQL did not become ready after ${RETRIES} attempts. Check: docker compose logs db"
  fi
  sleep 1
done

# ── Step 4: Create virtual environment ───────────────────────
step "Creating Python virtual environment"

VENV_DIR=".venv"
if [ ! -d "$VENV_DIR" ]; then
  "$PYTHON" -m venv "$VENV_DIR"
  success "Virtual environment created at $VENV_DIR"
else
  success "Virtual environment already exists at $VENV_DIR"
fi

# Activate
source "$VENV_DIR/bin/activate"
success "Virtual environment activated"

# ── Step 5: Install Python dependencies ──────────────────────
step "Installing Python dependencies"

pip install --upgrade pip --quiet
pip install -r requirements.txt
success "Python dependencies installed"

# ── Step 6: Install Playwright browser ───────────────────────
step "Installing Playwright Chromium browser"

playwright install chromium
success "Playwright Chromium installed"

# ── Step 7: Create .env if missing ───────────────────────────
step "Checking .env configuration"

if [ ! -f ".env" ]; then
  if [ -f ".env.example" ]; then
    cp .env.example .env
    warn ".env created from .env.example — IMPORTANT: edit .env and fill in your credentials before running."
  else
    warn ".env.example not found. You'll need to create .env manually."
  fi
else
  success ".env already exists"
fi

# ── Done ─────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}${BOLD}✓ Setup complete!${NC}"
echo ""
echo "  Next steps:"
echo "    1. Edit ${BOLD}.env${NC} and fill in your API keys and credentials"
echo "       (LLM_PROVIDER, ANTHROPIC_API_KEY / GEMINI_API_KEY, applicant info, etc.)"
echo ""
echo "    2. If using Gemini (Vertex AI): place your service account key at"
echo "       ${BOLD}vertex-ai-credentials.json${NC} in the project root"
echo "       and set GCP_PROJECT_ID in .env"
echo ""
echo "    3. Start the app:"
echo "       ${BOLD}./run.sh${NC}"
echo ""
echo "  Open the dashboard at: ${BLUE}http://localhost:8000${NC}"
echo ""
