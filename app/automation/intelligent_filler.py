"""Intelligent universal job application form filler.

No ATS-specific code. The LLM is the only brain.

Pipeline for any URL:
  1. Navigate (restore saved session if available)
  2. Dismiss privacy / consent / cookie modals
  3. If no form visible → find & click Apply button (handles new tab + same-tab)
  4. Detect ATS iframe → switch context automatically
  5. Upload resume via hidden file input (no visibility check)
  6. Multi-step fill loop (up to MAX_STEPS):
       a. Dismiss modals
       b. Extract all visible form fields via JS
       c. LLM maps 21-field applicant profile → fill actions
       d. Execute fill actions (fill / select / check)
       e. Fill open-ended Q&A text areas via answer_generator
       f. If submit visible & auto_submit=True → click, done
       g. If Next / Continue button → click, next step
       h. No navigation button → stop
  7. Save session state for next run
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional
from urllib.parse import urlparse

if TYPE_CHECKING:
    from app.llm.base import LLMProvider

logger = logging.getLogger(__name__)

def _max_steps() -> int:
    from app.config import get_settings
    return get_settings().MAX_FORM_STEPS

# ── Apply button patterns ──────────────────────────────────────────────────────

_APPLY_BUTTON_SELECTORS = [
    "button.position-apply-button",
    "a[class*='apply'], button[class*='apply']",
    "a:text-is('Apply Now')", "button:text-is('Apply Now')",
    "a:text-is('Apply')", "button:text-is('Apply')",
    "a:text-is('Apply for this job')", "button:text-is('Apply for this job')",
    "a:text-is('Apply for Job')", "button:text-is('Apply for Job')",
    "[data-automation-id='applyLink']",
    "a[href*='apply'], button[href*='apply']",
]

# ATS iframe domains — switch context when form is embedded
_IFRAME_DOMAINS = [
    "greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com",
    "smartrecruiters.com", "icims.com", "jobvite.com", "breezy.hr",
    "workable.com", "recruitee.com", "eightfold.ai", "taleo.net",
    "bamboohr.com", "pinpointhq.com",
]

# Consent / cookie / privacy modal close patterns
_MODAL_SELECTORS = [
    "button:text-matches('Accept All', 'i')",
    "button:text-matches('Accept Cookies', 'i')",
    "button:text-matches('Accept all cookies', 'i')",
    "button:text-matches('I Accept', 'i')",
    "button:text-matches('I Agree', 'i')",
    "button:text-matches('Agree and Continue', 'i')",
    "button:text-matches('Allow All', 'i')",
    "button:text-matches('Got it', 'i')",
    "button:text-matches('Acknowledge', 'i')",
    "button:text-matches('I understand', 'i')",
    "button:text-matches('OK$', 'i')",
    # Eightfold.ai privacy / data-consent modal
    "button:text-matches('Close', 'i')",
    "button:text-matches('Dismiss', 'i')",
    "button[aria-label='Close']", "button[aria-label='close']",
    "button[aria-label='Dismiss']",
    "[class*='modal'] button[class*='close']",
    "[class*='cookie'] button[class*='close']",
    "[class*='consent'] button[class*='accept']",
    "[class*='privacy'] button[class*='accept']",
    "[class*='privacy-modal'] button",
    "[class*='notice'] button[class*='accept']",
    "[data-testid*='accept']",
    "[data-testid*='consent-accept']",
    "[class*='ef-modal'] button:not([class*='cancel']):not([class*='secondary'])",
]

# Next / Continue step navigation
_NEXT_SELECTORS = [
    "button:text-is('Next')", "button:text-is('Continue')",
    "button:text-is('Next Step')", "button:text-is('Next →')",
    "button:text-is('Save & Continue')", "button:text-is('Save and Continue')",
    "button:text-is('Confirm')",         # Eightfold.ai resume confirmation step
    "button:text-is('Proceed')",
    "input[value='Next']", "input[value='Continue']",
    "[class*='next-button']", "[class*='btn-next']",
    "[data-automation-id*='next']",
    "button[type='button']:text-matches('^Next$', 'i')",
    "button[type='button']:text-matches('^Continue$', 'i')",
    "button[type='button']:text-matches('^Confirm$', 'i')",
]

# Submit patterns
_SUBMIT_SELECTORS = [
    "button[type='submit']", "input[type='submit']",
    "button:text-is('Submit')", "button:text-is('Submit Application')",
    "button:text-is('Submit application')", "button:text-is('Apply')",
    "button:text-is('Send Application')", "button:text-is('Finish')",
    "[data-automation-id='bottom-navigation-review-button']",
    "[data-automation-id='submit-button']",
    "[data-testid='submit-application-button']",
    "#submit_app", "button[id*='submit']",
]

# Skip these labels when filling Q&A (personal data already handled by LLM)
_PERSONAL_LABELS = frozenset({
    "first name", "last name", "first", "last", "name", "full name",
    "email", "email address", "phone", "phone number", "mobile",
    "linkedin", "linkedin profile", "linkedin url",
})

# JS to extract all visible form fields
_FIELD_EXTRACTOR_JS = """() => {
    const SKIP_TYPES = new Set(['hidden','submit','button','reset','image','file']);
    const fields = [];
    const seen  = new Set();

    document.querySelectorAll('input, textarea, select').forEach(el => {
        const type = (el.getAttribute('type') || el.tagName).toLowerCase();
        if (SKIP_TYPES.has(type)) return;

        const rect  = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);
        if (rect.width === 0 || rect.height === 0) return;
        if (style.display === 'none' || style.visibility === 'hidden') return;

        // Build a stable selector
        let selector = el.tagName.toLowerCase();
        if (el.id) {
            selector = '#' + CSS.escape(el.id);
        } else if (el.name) {
            selector = el.tagName.toLowerCase() + '[name="' + el.name + '"]';
        } else if (el.getAttribute('data-automation-id')) {
            selector = el.tagName.toLowerCase() + '[data-automation-id="' + el.getAttribute('data-automation-id') + '"]';
        } else if (el.getAttribute('data-testid')) {
            selector = el.tagName.toLowerCase() + '[data-testid="' + el.getAttribute('data-testid') + '"]';
        }
        if (seen.has(selector)) return;
        seen.add(selector);

        // Resolve label text
        let label = el.getAttribute('aria-label') || el.getAttribute('placeholder') || '';
        if (!label && el.id) {
            const lbl = document.querySelector('label[for="' + el.id + '"]');
            if (lbl) label = lbl.innerText.trim();
        }
        if (!label) {
            let parent = el.parentElement;
            for (let i = 0; i < 5 && parent; i++) {
                const lbl = parent.querySelector('label');
                if (lbl && lbl !== el) { label = lbl.innerText.trim(); break; }
                parent = parent.parentElement;
            }
        }

        // Options for <select>
        const options = el.tagName === 'SELECT'
            ? Array.from(el.options).map(o => o.text.trim()).filter(Boolean).slice(0, 30)
            : [];

        fields.push({
            selector,
            tag:           el.tagName.toLowerCase(),
            type,
            label:         label.replace(/[*:]+/g, '').trim(),
            current_value: el.value || '',
            options,
            required:      el.required,
        });
    });
    return fields;
}"""

# JS to find required fields still empty
_REQUIRED_UNFILLED_JS = """() => {
    const empty = [];
    document.querySelectorAll('input[required], textarea[required], select[required]').forEach(el => {
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden') return;
        if (el.value && el.value.trim()) return;
        let label = el.getAttribute('aria-label') || el.getAttribute('placeholder') || '';
        if (!label && el.id) {
            const lbl = document.querySelector('label[for="' + el.id + '"]');
            if (lbl) label = lbl.innerText.trim();
        }
        if (label) empty.push(label);
    });
    return empty;
}"""


# ── Session helpers ────────────────────────────────────────────────────────────

def _session_dir() -> Path:
    from app.config import get_settings
    p = Path(get_settings().SESSION_DIR).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


def _session_path(url: str) -> Path:
    netloc = urlparse(url).netloc or "unknown"
    safe = netloc.replace(".", "_").replace(":", "_")
    return _session_dir() / f"{safe}.json"


def _load_session(url: str) -> dict | None:
    path = _session_path(url)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return None
    return None


async def _save_session(context, url: str) -> None:
    try:
        state = await context.storage_state()
        _session_path(url).write_text(json.dumps(state))
    except Exception as exc:
        logger.warning("Could not save session: %s", exc)


# ── Core steps (module-level so they can be unit-tested) ─────────────────────

async def _dismiss_modals(page) -> int:
    """Click the first visible consent / cookie / privacy overlay button.

    Returns the number of modals dismissed (0 or 1 per call — chain calls for
    multi-layer modals).
    """
    for sel in _MODAL_SELECTORS:
        try:
            btn = await page.query_selector(sel)
            if btn and await btn.is_visible():
                await btn.click()
                await page.wait_for_timeout(800)
                logger.info("Dismissed modal: %s", sel[:60])
                return 1
        except Exception:
            pass
    return 0


async def _find_ats_iframe(page):
    """Return the frame context if an ATS form is embedded, else the page itself."""
    try:
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            src = frame.url or ""
            if any(d in src for d in _IFRAME_DOMAINS):
                logger.info("Found ATS iframe: %s", src[:80])
                return frame
        iframes = await page.query_selector_all("iframe[src]")
        for el in iframes:
            src = await el.get_attribute("src") or ""
            if any(d in src for d in _IFRAME_DOMAINS):
                frame = await el.content_frame()
                if frame:
                    logger.info("Found ATS iframe (DOM): %s", src[:80])
                    return frame
    except Exception as exc:
        logger.debug("iframe scan: %s", exc)
    return page


async def _page_has_form(page) -> bool:
    """True if the current page is a dedicated job application form.

    We check for strong signals: ATS-specific markers, personal-info fields,
    or a file upload input. We deliberately avoid triggering on job search
    boxes (title + location inputs on listing pages).
    """
    try:
        # Definitive ATS markers
        for sel in (
            "[data-automation-id='stepName']",          # Workday
            "form#application-form", "form.application-form",
            "#application_form", "#apply-form",
            "form[action*='apply']", "form[action*='application']",
        ):
            if await page.query_selector(sel):
                return True

        # Personal-info field patterns — only appear on application forms
        personal_sels = [
            "input[name*='firstName'], input[id*='firstName']",
            "input[name*='lastName'],  input[id*='lastName']",
            "input[type='email']",
            "input[name*='phone'], input[type='tel']",
            "input[name*='resume'], input[type='file']",
        ]
        hits = 0
        for sel in personal_sels:
            if await page.query_selector(sel):
                hits += 1
                if hits >= 2:
                    return True

        # URL is already on a known ATS domain
        url = page.url.lower()
        for domain in _IFRAME_DOMAINS:
            if domain in url:
                return True
    except Exception:
        pass
    return False


async def _navigate_to_form(page, context) -> tuple:
    """From a job listing page, find Apply and navigate to the form.

    Returns (active_page, frame) — the page may change if Apply opens a new tab.
    Priority: always try Apply button first. Only skip if URL is already on a
    dedicated ATS domain (e.g. myworkdayjobs.com).
    """
    # Skip navigation only if we're already on a dedicated ATS URL
    url_lower = page.url.lower()
    already_on_ats = any(d in url_lower for d in _IFRAME_DOMAINS)

    if already_on_ats and await _page_has_form(page):
        logger.info("Already on ATS form page: %s", page.url[:80])
        frame = await _find_ats_iframe(page)
        return page, frame

    # Try to find and click an Apply button
    for sel in _APPLY_BUTTON_SELECTORS:
        try:
            el = await page.query_selector(sel)
            if not el:
                continue

            # Direct href → navigate without click
            href = await el.get_attribute("href") or ""
            if href.startswith("http") and any(d in href for d in _IFRAME_DOMAINS):
                logger.info("Apply href: %s", href[:80])
                await page.goto(href, wait_until="domcontentloaded", timeout=60_000)
                await page.wait_for_timeout(2_000)
                frame = await _find_ats_iframe(page)
                return page, frame

            # Click and detect new tab vs same-tab
            pages_before = len(context.pages)
            prev_url = page.url
            await el.click()
            await page.wait_for_timeout(3_500)

            if len(context.pages) > pages_before:
                new_page = context.pages[-1]
                await new_page.wait_for_load_state("domcontentloaded")
                await new_page.wait_for_timeout(2_500)
                logger.info("Apply opened new tab: %s", new_page.url[:80])
                frame = await _find_ats_iframe(new_page)
                return new_page, frame

            if page.url != prev_url:
                logger.info("Apply same-tab nav: %s", page.url[:80])
                await page.wait_for_timeout(1_500)
                frame = await _find_ats_iframe(page)
                return page, frame

            # Apply button clicked but no navigation — a modal appeared on the same page.
            # Check for a file input (resume upload step) or a full application form.
            file_input = await page.query_selector("input[type='file']")
            if file_input or await _page_has_form(page):
                logger.info("Apply opened modal on same page (file_input=%s)", file_input is not None)
                frame = await _find_ats_iframe(page)
                return page, frame

        except Exception as exc:
            logger.debug("Apply selector %s: %s", sel[:40], exc)

    # Nothing worked — try filling whatever is on the current page
    if await _page_has_form(page):
        logger.info("Falling back to form on current page")
    else:
        logger.warning("No Apply button found and no form detected — proceeding anyway")
    frame = await _find_ats_iframe(page)
    return page, frame


async def _upload_resume(frame, resume_path: str) -> bool:
    """Upload resume to ANY hidden file input on the frame.

    File inputs on every ATS are CSS-hidden (opacity:0 / 1px wide).
    Playwright's set_input_files() works on them without a visibility check.
    """
    if not resume_path or not Path(resume_path).exists():
        logger.warning("Resume file not found: %s", resume_path)
        return False
    try:
        file_inputs = await frame.query_selector_all("input[type='file']")
        for fi in file_inputs:
            try:
                await fi.set_input_files(resume_path)
                # Wait for any upload-processing spinner to clear
                try:
                    await frame.wait_for_selector(
                        "div.spinner, [class*='uploading'], [class*='processing']",
                        state="hidden", timeout=10_000
                    )
                except Exception:
                    await frame.wait_for_timeout(3_000)
                await frame.wait_for_timeout(1_000)
                logger.info("Uploaded resume: %s", Path(resume_path).name)
                return True
            except Exception:
                pass
        # Drag-and-drop zone — click the label to trigger the hidden input
        for sel in [
            "label[for*='resume'], label[for*='file'], label[for*='upload']",
            "button:text-matches('upload|attach|browse', 'i')",
            "[class*='upload-area'], [class*='drop-zone'], [class*='file-drop']",
        ]:
            try:
                btn = await frame.query_selector(sel)
                if btn:
                    await btn.click()
                    await frame.wait_for_timeout(600)
                    for fi in await frame.query_selector_all("input[type='file']"):
                        try:
                            await fi.set_input_files(resume_path)
                            await frame.wait_for_timeout(2_000)
                            logger.info("Uploaded resume via label: %s", Path(resume_path).name)
                            return True
                        except Exception:
                            pass
            except Exception:
                pass
    except Exception as exc:
        logger.warning("Resume upload failed: %s", exc)
    return False


async def _extract_fields(frame) -> list[dict]:
    """Extract all visible form fields from the frame."""
    try:
        return await frame.evaluate(_FIELD_EXTRACTOR_JS) or []
    except Exception as exc:
        logger.warning("Field extraction failed: %s", exc)
        return []


def _build_profile(settings, tailored_material: dict) -> dict:
    """Build the full 21-field applicant profile for LLM consumption."""
    first = settings.APPLICANT_FIRST_NAME or ""
    last  = settings.APPLICANT_LAST_NAME  or ""
    return {
        "first_name":           first,
        "last_name":            last,
        "full_name":            f"{first} {last}".strip(),
        "email":                settings.APPLICANT_EMAIL,
        "phone":                settings.APPLICANT_PHONE,
        "linkedin":             settings.APPLICANT_LINKEDIN,
        "summary":              tailored_material.get("summary", ""),
        "keywords":             tailored_material.get("keywords", []),
        "cover_letter":         tailored_material.get("summary", ""),
        "years_of_experience":  settings.APPLICANT_YEARS_EXP,
        "current_employer":     settings.APPLICANT_CURRENT_EMPLOYER,
        "current_title":        settings.APPLICANT_CURRENT_TITLE,
        "work_authorization":   settings.APPLICANT_WORK_AUTH,
        "visa_sponsorship":     settings.APPLICANT_VISA_NEEDED,
        "willing_to_relocate":  settings.APPLICANT_RELOCATE,
        "salary_expectation":   settings.APPLICANT_SALARY_EXPECT,
        "notice_period":        settings.APPLICANT_NOTICE_PERIOD,
        "github":               settings.APPLICANT_GITHUB,
        "portfolio":            settings.APPLICANT_PORTFOLIO,
        "city":                 settings.APPLICANT_CITY,
        "country":              settings.APPLICANT_COUNTRY,
    }


def _make_llm_prompt(fields: list[dict], profile: dict) -> str:
    return f"""You are filling a job application form on behalf of this applicant.

APPLICANT PROFILE:
{json.dumps(profile, indent=2)}

VISIBLE FORM FIELDS (unfilled only):
{json.dumps(fields, indent=2)}

Return a JSON array of fill actions. Each action must be:
{{
  "selector": "<exact CSS selector from the list above>",
  "action": "fill" | "select" | "check",
  "value": "<string for fill/select — must exactly match an option for select; true/false for check>"
}}

Rules:
- ONLY use selectors that appear verbatim in the field list.
- "fill"   → text input or textarea
- "select" → <select> dropdown; value must exactly match one of the listed options
- "check"  → checkbox; true to check, false to uncheck
- For cover_letter / message / motivation fields → use the profile summary
- For years_of_experience → use profile years_of_experience
- For work_authorization / eligible_to_work → use profile work_authorization
- For notice_period / availability → use profile notice_period
- Skip any field you have no profile data for. Never invent data.
- Return ONLY the raw JSON array. No markdown, no explanation.
"""


async def _llm_map_fields(fields: list[dict], profile: dict, llm: "LLMProvider") -> list[dict]:
    """Ask LLM to produce fill actions for all unfilled fields. Retries once."""
    unfilled = [f for f in fields if not f.get("current_value") and f.get("label")]
    if not unfilled:
        return []

    def _parse(raw: str) -> list[dict]:
        cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned.strip())
        result  = json.loads(cleaned)
        return result if isinstance(result, list) else []

    for attempt, field_list in enumerate([unfilled, [
        {"selector": f["selector"], "label": f["label"],
         "type": f["type"], "options": f.get("options", [])}
        for f in unfilled
    ]]):
        try:
            raw     = llm.generate(_make_llm_prompt(field_list, profile), max_tokens=2000)
            actions = _parse(raw)
            if actions:
                return actions
            if attempt == 0:
                logger.warning("LLM returned empty actions — retrying with simplified fields")
        except Exception as exc:
            logger.warning("LLM field mapping attempt %d failed: %s", attempt + 1, exc)

    return []


async def _execute_actions(frame, actions: list[dict], filled: list, skipped: list) -> None:
    """Execute LLM-generated fill actions."""
    for action in actions:
        selector = action.get("selector", "")
        act      = action.get("action", "fill")
        value    = action.get("value")

        if not selector or value is None or value == "":
            continue
        try:
            el = await frame.query_selector(selector)
            if not el or not await el.is_visible():
                skipped.append(selector[:50])
                continue

            if act == "fill":
                await el.fill(str(value))
                filled.append(selector[:50])

            elif act == "select":
                try:
                    await el.select_option(label=str(value))
                except Exception:
                    await el.select_option(value=str(value))
                filled.append(selector[:50])

            elif act == "check":
                if bool(value) != await el.is_checked():
                    await el.set_checked(bool(value))
                filled.append(selector[:50])

        except Exception as exc:
            logger.debug("Action failed (%s): %s", selector[:50], exc)
            skipped.append(selector[:50])


async def _fill_qa_fields(frame, answer_generator: Callable, filled: list, skipped: list) -> None:
    """Fill open-ended Q&A textareas not covered by the LLM profile fill."""
    try:
        elements = [
            el for el in await frame.query_selector_all("textarea, input[type='text']")
            if await el.is_visible()
        ]
    except Exception:
        return

    for el in elements:
        try:
            label = ""
            el_id = await el.get_attribute("id") or ""
            if el_id:
                lbl = await frame.query_selector(f"label[for='{el_id}']")
                if lbl:
                    label = (await lbl.inner_text()).strip()
            if not label:
                label = (
                    await el.get_attribute("aria-label") or
                    await el.get_attribute("placeholder") or ""
                ).strip()

            if not label or label.lower() in _PERSONAL_LABELS or len(label) < 6:
                continue
            if await el.input_value():
                continue

            answer = answer_generator(label)
            if answer:
                await el.fill(answer)
                filled.append(f"qa:{label[:40]}")
                logger.info("Answered Q&A: %r", label[:80])
        except Exception:
            pass


async def _find_button(frame, selectors: list[str]):
    """Return the first visible+enabled button matching any selector."""
    for sel in selectors:
        try:
            el = await frame.query_selector(sel)
            if el and await el.is_visible():
                return el
        except Exception:
            pass
    return None


async def _required_unfilled(frame) -> list[str]:
    """Return labels of required fields still empty."""
    try:
        return await frame.evaluate(_REQUIRED_UNFILLED_JS) or []
    except Exception:
        return []


# ── Workday ───────────────────────────────────────────────────────────────────

def _is_workday_url(url: str) -> bool:
    return "myworkdayjobs" in url or "myworkday.com" in url


# Workday uses data-automation-id on nearly every interactive element
_WD = {
    # Auth
    "email":             "input[data-automation-id='email'], input[type='email']",
    "password":          "input[type='password']",
    "create_link":       "a[data-automation-id='createAccountLink'], a:text-is('Create Account'), a:text-is('Register')",
    "create_submit":     "button[data-automation-id='createAccountSubmitButton']",
    "signin_submit":     "button[data-automation-id='signInSubmitButton'], button[type='submit']",
    "terms":             "input[type='checkbox'][data-automation-id*='termsAndConditions'], input[type='checkbox'][name*='terms']",
    # Application shortcuts
    "use_last":          "button[data-automation-id='useLastApplication'], a[data-automation-id='useLastApplication']",
    "apply_manually":    "button[data-automation-id='applyManually'], a[data-automation-id='applyManually']",
    # Navigation
    "next":              "button[data-automation-id='bottom-navigation-next-button']",
    "review":            "button[data-automation-id='bottom-navigation-review-button']",
    "submit":            "button[data-automation-id='bottom-navigation-footer-button']",
    # Indicators
    "step_name":         "[data-automation-id='stepName']",
    "file_input":        "input[type='file']",
    "apply_link":        "a[data-automation-id='applyLink'], a:text-is('Apply'), button:text-is('Apply')",
}


class WorkdayHandler:
    """Handles the full Workday multi-step application flow.

    Workday flow:
      1. Navigate to job listing → click Apply
      2. Account wall:  sign in OR create account with email + password
      3. "Use Last Application" shortcut → skip straight to review if available
      4. "Apply Manually" → resume upload → multi-step form (up to 8 steps)
      5. Each step: LLM fills visible fields → click Next
      6. Review step → Submit (if auto_submit)
    """

    def __init__(self, llm: "LLMProvider") -> None:
        self.llm = llm

    async def run(
        self,
        page,
        context,
        settings,
        profile: dict,
        resume_abs: str,
        auto_submit: bool,
        answer_fn: Callable,
        filled: list,
        skipped: list,
    ) -> bool:
        """Return True if application was submitted."""
        await page.wait_for_timeout(2_000)

        # ── 1. Click Apply if on a JD listing page ────────────────────────────
        if not await page.query_selector(_WD["step_name"]):
            apply = await page.query_selector(_WD["apply_link"])
            if apply and await apply.is_visible():
                pages_before = len(context.pages)
                await apply.click()
                await page.wait_for_timeout(3_000)
                if len(context.pages) > pages_before:
                    page = context.pages[-1]
                    await page.wait_for_load_state("domcontentloaded")
                    await page.wait_for_timeout(2_000)
                logger.info("Workday: clicked Apply, url=%s", page.url[:70])

        # ── 2. Account wall ───────────────────────────────────────────────────
        await self._handle_auth(page, settings, filled, skipped)

        # ── 3. "Use Last Application" shortcut ────────────────────────────────
        use_last = await page.query_selector(_WD["use_last"])
        if use_last and await use_last.is_visible():
            logger.info("Workday: using last application")
            await use_last.click()
            await page.wait_for_timeout(3_000)
            filled.append("use_last_application")
            return await self._do_submit(page, auto_submit, filled, skipped)

        # ── 4. Apply manually ─────────────────────────────────────────────────
        manually = await page.query_selector(_WD["apply_manually"])
        if manually and await manually.is_visible():
            await manually.click()
            await page.wait_for_timeout(3_000)
            logger.info("Workday: apply manually selected")

        # ── 5. Resume upload ──────────────────────────────────────────────────
        if resume_abs:
            if await _upload_resume(page, resume_abs):
                filled.append("resume_upload")
            else:
                skipped.append("resume_upload")

        # ── 6. Multi-step fill loop ───────────────────────────────────────────
        from app.config import get_settings as _get_settings
        for step in range(_get_settings().MAX_WORKDAY_STEPS):
            step_label = await self._step_name(page)
            logger.info("Workday: step %d — %s", step + 1, step_label)

            # Dismiss any overlay / consent modal
            await _dismiss_modals(page)

            # LLM fill all visible fields
            fields = await _extract_fields(page)
            if fields:
                actions = await _llm_map_fields(fields, profile, self.llm)
                if actions:
                    await _execute_actions(page, actions, filled, skipped)

            # Open-ended Q&A
            await _fill_qa_fields(page, answer_fn, filled, skipped)

            # Try uploading resume again (upload step can appear mid-flow)
            if "resume_upload" not in filled and resume_abs:
                if await _upload_resume(page, resume_abs):
                    filled.append("resume_upload")

            await page.wait_for_timeout(500)

            # Review button → click then submit
            review = await page.query_selector(_WD["review"])
            if review and await review.is_visible() and await review.is_enabled():
                logger.info("Workday: clicking Review")
                await review.click()
                await page.wait_for_timeout(2_000)
                return await self._do_submit(page, auto_submit, filled, skipped)

            # Next button
            next_btn = await page.query_selector(_WD["next"])
            if not next_btn or not await next_btn.is_visible():
                # No Next and no Review — might be at submit already
                return await self._do_submit(page, auto_submit, filled, skipped)

            if not await next_btn.is_enabled():
                blocking = await _required_unfilled(page)
                if blocking:
                    logger.warning("Workday Next disabled — required empty: %s", blocking[:6])
                    skipped.extend(f"required:{f}" for f in blocking[:6])
                else:
                    logger.warning("Workday Next disabled at step %d", step + 1)
                break

            await next_btn.click()
            await page.wait_for_timeout(2_500)

        return False

    async def _handle_auth(self, page, settings, filled, skipped) -> None:
        """Handle Workday's sign-in / create-account wall."""
        await page.wait_for_timeout(1_500)

        # Already past auth if the step indicator is visible
        if await page.query_selector(_WD["step_name"]):
            return

        email_el = await page.query_selector(_WD["email"])
        if not email_el:
            return  # no auth wall

        email    = settings.APPLICANT_EMAIL or ""
        password = settings.APPLICANT_PASSWORD or ""

        if not email:
            logger.warning("Workday: auth wall found but APPLICANT_EMAIL not set in .env")
            return

        # Fill email
        try:
            await email_el.fill(email)
            filled.append("wd_email")
        except Exception:
            pass

        # Prefer "Create Account" for new accounts; fall back to sign-in
        create_link = await page.query_selector(_WD["create_link"])
        if create_link and await create_link.is_visible():
            await create_link.click()
            await page.wait_for_timeout(2_000)
            # Re-fill email in the create-account form
            email_el2 = await page.query_selector(_WD["email"])
            if email_el2:
                await email_el2.fill(email)

        # Fill password (and confirm if present)
        pw_els = await page.query_selector_all("input[type='password']")
        for pw_el in pw_els:
            try:
                await pw_el.fill(password)
            except Exception:
                pass
        if pw_els:
            filled.append("wd_password")

        # Accept terms
        terms = await page.query_selector(_WD["terms"])
        if terms and not await terms.is_checked():
            try:
                await terms.check()
                filled.append("wd_terms")
            except Exception:
                pass

        # Submit create-account or sign-in
        for submit_sel in (_WD["create_submit"], _WD["signin_submit"]):
            btn = await page.query_selector(submit_sel)
            if btn and await btn.is_visible():
                try:
                    await btn.click()
                    await page.wait_for_timeout(4_000)
                    filled.append("wd_auth_submit")
                    logger.info("Workday: auth submitted")
                    return
                except Exception as exc:
                    logger.warning("Workday auth submit failed: %s", exc)

    async def _step_name(self, page) -> str:
        try:
            el = await page.query_selector(_WD["step_name"])
            return (await el.inner_text()).strip() if el else "unknown"
        except Exception:
            return "unknown"

    async def _do_submit(self, page, auto_submit: bool, filled: list, skipped: list) -> bool:
        if not auto_submit:
            logger.info("Workday: stopped at review/submit (AUTO_SUBMIT=False)")
            return False

        for submit_sel in (_WD["submit"], _WD["next"]):
            btn = await page.query_selector(submit_sel)
            if btn and await btn.is_visible() and await btn.is_enabled():
                await btn.click()
                await page.wait_for_timeout(3_000)
                filled.append("submit_button")
                logger.info("Workday: application submitted")
                return True

        skipped.append("submit_button")
        return False


# ── Main class ────────────────────────────────────────────────────────────────

class IntelligentFiller:
    """Generic, LLM-driven form filler.

    Works on any job application form — no ATS-specific code.
    Pass `llm` (a scoring-tier model is fine) and it will map
    your applicant profile to whatever fields appear on screen.
    """

    def __init__(self, llm: "LLMProvider") -> None:
        self.llm = llm

    async def fill(
        self,
        application_url: str,
        tailored_material: dict,
        resume_path: str,
        auto_submit: bool = False,
        answer_generator: Optional[Callable] = None,
    ) -> dict:
        """Fill the application form at *application_url*.

        Parameters
        ----------
        application_url   URL of the job listing or direct application form.
        tailored_material Dict from ResumeTailor (summary, bullets, keywords).
        resume_path       Absolute path to the tailored DOCX/PDF to upload.
        auto_submit       Click Submit when True.
        answer_generator  Callable(question_text) → str for Q&A fields.
        """
        from playwright.async_api import async_playwright
        from app.config import get_settings

        settings       = get_settings()
        resume_abs     = str(Path(resume_path).resolve()) if resume_path else ""
        profile        = _build_profile(settings, tailored_material)
        storage_state  = _load_session(application_url)

        filled: list[str]  = []
        skipped: list[str] = []
        submitted          = False
        error: str | None  = None
        final_url          = application_url

        def _answer(question: str) -> str:
            return answer_generator(question) if answer_generator else ""

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=False,
                args=["--no-first-run", "--disable-blink-features=AutomationControlled"],
            )
            ctx_kwargs: dict = {
                "viewport":   {"width": 1280, "height": 900},
                "user_agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            }
            if storage_state:
                ctx_kwargs["storage_state"] = storage_state
            context = await browser.new_context(**ctx_kwargs)
            page    = await context.new_page()

            try:
                logger.info("IntelligentFiller: %s", application_url)
                await page.goto(application_url, wait_until="domcontentloaded", timeout=60_000)
                await page.wait_for_timeout(3_000)

                # ── Dismiss landing-page modals ───────────────────────────────
                for _ in range(3):
                    if not await _dismiss_modals(page):
                        break

                # ── Workday — dedicated handler ───────────────────────────────
                if _is_workday_url(page.url) or _is_workday_url(application_url):
                    logger.info("IntelligentFiller: Workday detected — using WorkdayHandler")
                    submitted = await WorkdayHandler(self.llm).run(
                        page=page, context=context, settings=settings,
                        profile=profile, resume_abs=resume_abs,
                        auto_submit=auto_submit, answer_fn=_answer,
                        filled=filled, skipped=skipped,
                    )
                    final_url = page.url
                    await _save_session(context, final_url)
                    return {
                        "fields_filled":  filled,
                        "fields_skipped": skipped,
                        "submitted":       submitted,
                        "error":           None,
                        "url":             final_url,
                    }

                # ── Navigate from listing to form ─────────────────────────────
                active_page, frame = await _navigate_to_form(page, context)
                final_url = active_page.url

                # ── Upload resume (once, before the fill loop) ────────────────
                resume_uploaded = False
                if resume_abs:
                    resume_uploaded = await _upload_resume(frame, resume_abs)
                    if resume_uploaded:
                        filled.append("resume_upload")
                    else:
                        skipped.append("resume_upload")

                # ── Multi-step fill loop ──────────────────────────────────────
                for step in range(_max_steps()):
                    logger.info("IntelligentFiller: step %d", step + 1)

                    # Dismiss any modals that appeared between steps
                    for _ in range(3):
                        dismissed = await _dismiss_modals(active_page)
                        if not dismissed:
                            break
                        await active_page.wait_for_timeout(2_000)  # let next form render

                    # Try uploading resume again if it failed on first attempt
                    # (some ATS only shows the upload field on step 1)
                    if not resume_uploaded and resume_abs:
                        if await _upload_resume(frame, resume_abs):
                            filled.append("resume_upload")
                            resume_uploaded = True

                    # Extract + LLM fill
                    fields = await _extract_fields(frame)
                    if fields:
                        actions = await _llm_map_fields(fields, profile, self.llm)
                        if actions:
                            await _execute_actions(frame, actions, filled, skipped)

                    # Q&A text areas
                    await _fill_qa_fields(frame, _answer, filled, skipped)

                    # Submit?
                    submit_btn = await _find_button(frame, _SUBMIT_SELECTORS)
                    if submit_btn and await submit_btn.is_visible():
                        if auto_submit and await submit_btn.is_enabled():
                            logger.info("IntelligentFiller: submitting (step %d)", step + 1)
                            await submit_btn.click()
                            try:
                                await active_page.wait_for_load_state("networkidle", timeout=15_000)
                            except Exception:
                                pass
                            await active_page.wait_for_timeout(2_000)
                            filled.append("submit_button")
                            submitted = True
                        else:
                            logger.info("IntelligentFiller: reached submit (AUTO_SUBMIT=False)")
                        break

                    # Next step?
                    next_btn = await _find_button(frame, _NEXT_SELECTORS)
                    if not next_btn:
                        logger.info("IntelligentFiller: no Next/Submit — stopping at step %d", step + 1)
                        break

                    if not await next_btn.is_enabled():
                        blocking = await _required_unfilled(frame)
                        if blocking:
                            logger.warning("Next disabled — required empty: %s", blocking[:8])
                            skipped.extend(f"required:{f}" for f in blocking[:8])
                        else:
                            logger.warning("Next disabled at step %d (unknown reason)", step + 1)
                        break

                    logger.info("IntelligentFiller: clicking Next (step %d)", step + 1)
                    await next_btn.click()

                    # Wait for any loading spinner to disappear.
                    # Eightfold.ai shows a full-page `div.spinner` for ~4s while parsing the resume.
                    # Generic fallback covers other ATS spinners via common class names.
                    spinner_cleared = False
                    for spinner_sel in (
                        "div.spinner",
                        "[class*='loading-overlay']",
                        "[class*='full-screen'][class*='modal'] [class*='spinner']",
                    ):
                        try:
                            await active_page.wait_for_selector(
                                spinner_sel, state="hidden", timeout=20_000
                            )
                            spinner_cleared = True
                            logger.info("Spinner cleared: %s", spinner_sel)
                            break
                        except Exception:
                            pass
                    if not spinner_cleared:
                        await active_page.wait_for_timeout(5_000)   # flat fallback
                    await active_page.wait_for_timeout(1_500)   # final DOM render buffer

                    # Re-detect frame after navigation (some ATS reload into a new iframe)
                    frame = await _find_ats_iframe(active_page)

                await _save_session(context, final_url)

            except Exception as exc:
                error = str(exc)
                logger.error("IntelligentFiller error: %s", exc, exc_info=True)
            finally:
                if not auto_submit and not error:
                    await asyncio.sleep(3)
                await browser.close()

        return {
            "fields_filled":  filled,
            "fields_skipped": skipped,
            "submitted":       submitted,
            "error":           error,
            "url":             final_url,
        }

    def fill_sync(
        self,
        application_url: str,
        tailored_material: dict,
        resume_path: str,
        auto_submit: bool = False,
        answer_generator: Optional[Callable] = None,
    ) -> dict:
        return asyncio.run(self.fill(
            application_url=application_url,
            tailored_material=tailored_material,
            resume_path=resume_path,
            auto_submit=auto_submit,
            answer_generator=answer_generator,
        ))
