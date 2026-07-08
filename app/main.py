from __future__ import annotations

from dotenv import load_dotenv
load_dotenv()  # must run before any os.environ reads or google-auth initialisation

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.scheduler import create_scheduler
from app.storage.db import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# Suppress noisy third-party SDK loggers
for _noisy in (
    "google_genai.models",
    "google.api_core.bidi",
    "urllib3.connectionpool",
    "httpx",
    "httpcore",
    "anthropic",
    "openai._base_client",
):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application startup and shutdown lifecycle."""
    # Startup
    logger.info("Starting job-agent...")

    import google.auth
    try:
        _creds, _project = google.auth.default()
        print(f"[AUTH] Project: {_project}, Credentials type: {type(_creds).__name__}")
    except Exception as _auth_err:
        print(f"[AUTH] WARNING: google.auth.default() failed — {_auth_err}")

    await init_db()
    logger.info("Database tables initialised.")

    scheduler = create_scheduler(app)
    scheduler.start()
    logger.info("Scheduler started.")

    # Store scheduler on app state so it can be accessed if needed
    app.state.scheduler = scheduler

    yield

    # Shutdown
    logger.info("Shutting down job-agent...")
    scheduler.shutdown(wait=False)
    logger.info("Scheduler stopped.")


app = FastAPI(
    title="Job Agent",
    description="Automated job search, scoring, and application assistant.",
    version="1.0.0",
    lifespan=lifespan,
)

_STATIC = Path(__file__).parent / "static"

# Serve the dashboard UI at root
@app.get("/", include_in_schema=False)
async def serve_ui():
    return FileResponse(_STATIC / "index.html")

# Health check (used by scripts / monitoring)
@app.get("/health", tags=["health"])
async def health_check():
    return {"status": "ok", "service": "job-agent"}

# Mount the API router
app.include_router(router, prefix="/api/v1", tags=["api"])
