"""Job board driver plugin system.

Architecture
============
Every job board is represented by a BaseJobBoardDriver subclass.
Drivers live in app/scraping/drivers/<name>.py and expose a module-level
DRIVER = MyDriver() instance. The registry auto-discovers them at startup.

Boards in config/job_boards.yaml that have no matching hand-written driver
are served by ConfigDrivenDriver, which delegates job discovery to
UniversalCrawler (LLM + heuristics).

Adding a new job board (two options):

  Option A — Config only (zero Python):
    Add an entry to config/job_boards.yaml. Works for any publicly accessible
    board where job links are findable via CSS selectors or LLM.

  Option B — Custom driver (full control):
    Create app/scraping/drivers/myboard.py, define MyBoardDriver(BaseJobBoardDriver),
    set  DRIVER = MyBoardDriver()  at module level. The registry picks it up
    automatically; the board name must match the key in job_boards.yaml.
"""
from __future__ import annotations

import importlib
import json
import logging
import pkgutil
import random
from abc import ABC, abstractmethod
from pathlib import Path

logger = logging.getLogger(__name__)

_SESSION_DIR = Path.home() / ".auto-jobsearch" / "sessions"
_BOARDS_CONFIG = Path(__file__).parents[3] / "config" / "job_boards.yaml"

# JavaScript injected on every new page to mask automation fingerprints.
_STEALTH_JS = """
() => {
    // Hide webdriver flag
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

    // Spoof plugins (real Chrome has several)
    Object.defineProperty(navigator, 'plugins', {
        get: () => {
            const arr = [
                { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
                { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
                { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' },
            ];
            arr.item = i => arr[i];
            arr.namedItem = n => arr.find(p => p.name === n) || null;
            arr.refresh = () => {};
            return arr;
        }
    });

    // Spoof languages
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en', 'hi'] });

    // Add chrome runtime object that real Chrome has
    if (!window.chrome) {
        window.chrome = {
            app: { isInstalled: false, InstallState: {}, RunningState: {} },
            runtime: { OnInstalledReason: {}, OnRestartRequiredReason: {}, PlatformArch: {},
                        PlatformNaclArch: {}, PlatformOs: {}, RequestUpdateCheckStatus: {} },
        };
    }

    // Override permissions query so bots can't be detected via denied notifications
    const origQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = params =>
        params.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : origQuery(params);
}
"""


def _load_cookies(site_name: str) -> list[dict]:
    """Legacy: load bare cookies list. Prefer load_storage_state() for new code."""
    path = _SESSION_DIR / f"{site_name}_cookies.json"
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except Exception:
        return []


async def _human_delay(page, min_ms: int = 800, max_ms: int = 2200) -> None:
    """Wait a random human-like duration."""
    await page.wait_for_timeout(random.randint(min_ms, max_ms))


async def _apply_stealth(page) -> None:
    """Inject stealth JS and set realistic extra headers."""
    await page.add_init_script(_STEALTH_JS)
    await page.set_extra_http_headers({
        "Accept-Language": "en-US,en;q=0.9,hi;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Upgrade-Insecure-Requests": "1",
    })


async def _natural_scroll(page, steps: int = 3) -> None:
    """Scroll down the page in small natural increments."""
    for _ in range(steps):
        scroll_by = random.randint(200, 500)
        await page.evaluate(f"window.scrollBy(0, {scroll_by})")
        await page.wait_for_timeout(random.randint(300, 700))
    # Scroll back to top
    await page.evaluate("window.scrollTo(0, 0)")
    await page.wait_for_timeout(random.randint(200, 500))


class BaseJobBoardDriver(ABC):
    """Abstract base for all job board drivers."""

    name: str = ""
    home_url: str = ""
    requires_login: bool = False

    def can_handle(self, url: str) -> bool:
        if not self.home_url:
            return False
        domain = self.home_url.rstrip("/").split("//")[-1]
        return domain in url

    async def is_logged_in(self, page) -> bool:
        return True

    @abstractmethod
    async def search(self, page, role: str, location: str, limit: int = 30) -> list[dict]:
        """Return list of job dicts. Receives a live, cookie-injected Playwright page."""

    async def _make_context(self, pw, s, storage_state=None, headless=None):
        """Create a stealth browser + context. Returns (browser, context).

        storage_state: Playwright storage_state dict (cookies + localStorage).
                       When provided, the context starts pre-authenticated.
        headless: override BROWSER_HEADLESS (pass False for login flows).
        """
        width = random.randint(1260, 1400)
        height = random.randint(860, 960)
        browser = await pw.chromium.launch(
            headless=s.BROWSER_HEADLESS if headless is None else headless,
            args=[
                "--no-first-run",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-extensions",
                "--disable-default-apps",
                f"--window-size={width},{height}",
            ],
        )
        ctx_kwargs: dict = {
            "viewport": {"width": width, "height": height},
            "user_agent": s.BROWSER_UA,
            "locale": "en-US",
            "timezone_id": "Asia/Kolkata",
            "color_scheme": "light",
        }
        if storage_state:
            ctx_kwargs["storage_state"] = storage_state
        context = await browser.new_context(**ctx_kwargs)
        return browser, context

    async def run_multi_search(
        self,
        role: str,
        locations: list[str],
        limit: int = 30,
    ) -> list[dict]:
        """Search across ALL locations in a SINGLE browser session.

        One browser open → session loaded → login check once → all locations → close.
        """
        from playwright.async_api import async_playwright
        from app.config import get_settings
        from app.scraping.portal_auth import load_storage_state
        s = get_settings()

        state = load_storage_state(self.name)
        if self.requires_login and not state:
            logger.error(
                "\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "  SESSION MISSING: %s\n"
                "  Go to the Portals tab in the UI and click Connect\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
                self.name,
            )
            return []

        all_results: list[dict] = []

        async with async_playwright() as pw:
            browser, context = await self._make_context(pw, s, storage_state=state)
            page = await context.new_page()
            await _apply_stealth(page)

            try:
                # Login check — done ONCE, not per location
                if self.requires_login and not await self.is_logged_in(page):
                    logger.error(
                        "\n"
                        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        "  SESSION EXPIRED: %s\n"
                        "  Go to the Portals tab in the UI and click Reconnect\n"
                        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
                        self.name,
                    )
                    return []

                for location in locations:
                    try:
                        jobs = await self.search(page, role, location, limit)
                        logger.info("%s (%s): %d jobs", self.name, location, len(jobs))
                        all_results.extend(jobs)
                        if location != locations[-1]:
                            await _human_delay(page, 2000, 5000)
                    except Exception as exc:
                        logger.error("%s (%s): search error: %s", self.name, location, exc)

            finally:
                await page.close()
                await context.close()
                await browser.close()

        return all_results

    async def run_search(self, role: str, location: str, limit: int = 30) -> list[dict]:
        """Single-location search. Delegates to run_multi_search."""
        return await self.run_multi_search(role, [location], limit)

    async def apply_to_job(self, page, job: dict) -> bool:
        """Override in subclass to implement portal-native apply. Returns True on success."""
        return False

    async def run_apply(self, job: dict) -> dict:
        """Apply to a single job using this portal's native apply flow.

        Opens ONE browser, loads cookies, login-checks once, calls apply_to_job(),
        then closes the browser.

        Returns
        -------
        dict with keys:
          applied       bool  – True if application was submitted
          external_url  str   – ATS redirect URL (Greenhouse/Lever/Workday) when the
                                portal itself cannot apply; caller should pass to
                                IntelligentFiller
          error         str   – human-readable error, or None on success
        """
        from playwright.async_api import async_playwright
        from app.config import get_settings
        from app.scraping.portal_auth import load_storage_state
        s = get_settings()

        result: dict = {"applied": False, "external_url": None, "error": None}

        state = load_storage_state(self.name)
        if self.requires_login and not state:
            logger.error(
                "SESSION MISSING: %s — go to Portals tab in UI and click Connect",
                self.name,
            )
            result["error"] = "session_missing"
            return result

        async with async_playwright() as pw:
            browser, context = await self._make_context(pw, s, storage_state=state)
            page = await context.new_page()
            await _apply_stealth(page)

            try:
                if self.requires_login and not await self.is_logged_in(page):
                    logger.error(
                        "SESSION EXPIRED: %s — session exists but login check failed. "
                        "Go to Portals tab in UI and click Reconnect.",
                        self.name,
                    )
                    result["error"] = "session_expired"
                    return result

                logger.info("run_apply [%s]: starting apply for %s", self.name, job.get("job_url", ""))
                applied = await self.apply_to_job(page, job)
                result["applied"] = applied

                # apply_to_job() may set job["external_apply_url"] when the portal
                # redirects to an external ATS (Greenhouse, Lever, Workday, etc.)
                if job.get("external_apply_url"):
                    result["external_url"] = job["external_apply_url"]
                    result["applied"] = False  # not submitted yet — needs ATS form fill

                if applied:
                    logger.info(
                        "run_apply [%s]: ✓ submitted — %s @ %s",
                        self.name, job.get("title"), job.get("company"),
                    )
                elif result["external_url"]:
                    logger.info(
                        "run_apply [%s]: external ATS redirect → %s",
                        self.name, result["external_url"][:80],
                    )
                else:
                    logger.warning(
                        "run_apply [%s]: apply_to_job returned False for %s",
                        self.name, job.get("job_url"),
                    )

            except Exception as exc:
                logger.error("run_apply [%s]: unhandled error: %s", self.name, exc)
                result["error"] = str(exc)
            finally:
                try:
                    await page.close()
                    await context.close()
                    await browser.close()
                except Exception:
                    pass

        return result


class ConfigDrivenDriver(BaseJobBoardDriver):
    """Generic driver for boards defined only in config/job_boards.yaml."""

    def __init__(self, key: str, cfg: dict) -> None:
        self.name = key
        self.home_url = cfg.get("home_url", "")
        self.requires_login = cfg.get("requires_login", False)
        self._search_url = cfg.get("search_url", "")
        self._hints = cfg.get("job_link_hints", [])
        self._next_hints = cfg.get("next_page_hints", [])
        self._scroll = cfg.get("scroll_to_load", False)
        self._region = cfg.get("region", "any")

    @property
    def region(self) -> str:
        return self._region

    async def is_logged_in(self, page) -> bool:
        if not self.home_url:
            return True
        from app.config import get_settings
        s = get_settings()
        try:
            await page.goto(self.home_url, wait_until="domcontentloaded", timeout=s.BROWSER_TIMEOUT_MS)
            await page.wait_for_timeout(s.BROWSER_SHORT_WAIT_MS)
            return True
        except Exception:
            return False

    async def search(self, page, role: str, location: str, limit: int = 30) -> list[dict]:
        from app.scraping.universal_crawler import UniversalCrawler
        from urllib.parse import quote

        if not self._search_url:
            return []

        role_slug = quote(role.lower().replace(" ", "-"), safe="-")
        loc_slug = quote(location.lower().replace(" ", "-"), safe="-")
        url = self._search_url.format(role=role_slug, location=loc_slug, page=1)

        crawler = UniversalCrawler()
        return await crawler.discover_jobs_on_page(
            page,
            url,
            hint_selectors=self._hints,
            next_selectors=self._next_hints,
            scroll_to_load=self._scroll,
            limit=limit,
            source=self.name,
        )


class JobBoardRegistry:
    """Central registry of all active job board drivers."""

    def __init__(self) -> None:
        self._drivers: list[BaseJobBoardDriver] = []

    def register(self, driver: BaseJobBoardDriver) -> None:
        self._drivers.append(driver)
        logger.debug("Registered board: %s (%s)", driver.name, type(driver).__name__)

    def get_driver(self, url: str) -> BaseJobBoardDriver | None:
        for d in self._drivers:
            if d.can_handle(url):
                return d
        return None

    def all_enabled(self) -> list[BaseJobBoardDriver]:
        return list(self._drivers)

    def by_region(self, region: str) -> list[BaseJobBoardDriver]:
        out = []
        for d in self._drivers:
            r = getattr(d, "region", "any")
            if r in (region, "any"):
                out.append(d)
        return out

    def _load_builtin_drivers(self) -> set[str]:
        pkg_dir = Path(__file__).parent
        registered: set[str] = set()
        for module_info in pkgutil.iter_modules([str(pkg_dir)]):
            if module_info.name.startswith("_"):
                continue
            try:
                mod = importlib.import_module(f"app.scraping.drivers.{module_info.name}")
                driver: BaseJobBoardDriver | None = getattr(mod, "DRIVER", None)
                if driver is not None:
                    self.register(driver)
                    registered.add(driver.name)
            except Exception as exc:
                logger.warning("Could not load driver module '%s': %s", module_info.name, exc)
        return registered

    def _load_config_drivers(self, already_registered: set[str]) -> None:
        if not _BOARDS_CONFIG.exists():
            return
        try:
            import yaml
            data = yaml.safe_load(_BOARDS_CONFIG.read_text()) or {}
        except ImportError:
            logger.warning("PyYAML not installed — cannot load job_boards.yaml. Run: pip install pyyaml")
            return
        except Exception as exc:
            logger.warning("Could not parse job_boards.yaml: %s", exc)
            return

        for key, cfg in (data.get("boards") or {}).items():
            if not cfg.get("enabled", True):
                continue
            if key in already_registered:
                continue
            self.register(ConfigDrivenDriver(key, cfg))

    def bootstrap(self) -> "JobBoardRegistry":
        registered = self._load_builtin_drivers()
        self._load_config_drivers(registered)
        return self


_registry: JobBoardRegistry | None = None


def get_registry() -> JobBoardRegistry:
    global _registry
    if _registry is None:
        _registry = JobBoardRegistry().bootstrap()
    return _registry
