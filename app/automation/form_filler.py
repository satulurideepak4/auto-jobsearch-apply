"""Universal application form filler.

Works on any job application form anywhere on the web.

Strategy (in order):
  1. Detect known ATS from URL  →  fast selector-based fill
     Supported: Workday, Greenhouse, Lever, Ashby
  2. LLM form analysis          →  for any unknown or custom form
     Extracts every visible field, asks LLM to map applicant profile → fields,
     then executes the fill instructions.
  3. Heuristic fallback         →  if no LLM is available
     Matches fields by name/id/placeholder patterns.

All three paths end with _fill_visible_questions() to handle free-text Q&A
fields using the answer_generator callable.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional
from urllib.parse import urlparse

if TYPE_CHECKING:
    from app.llm.base import LLMProvider

logger = logging.getLogger(__name__)


# ── ATS detection ──────────────────────────────────────────────────────────────

_ATS_URL_MAP = [
    ("greenhouse.io", "greenhouse"),
    ("lever.co", "lever"),
    ("jobs.ashbyhq.com", "ashby"),
    ("ashby.com", "ashby"),
    ("myworkdayjobs", "workday"),
    ("myworkday.com", "workday"),
    ("taleo.net", "taleo"),
    ("icims.com", "icims"),
    ("smartrecruiters.com", "smartrecruiters"),
    ("jobvite.com", "jobvite"),
    ("breezy.hr", "breezy"),
    ("bamboohr.com", "bamboohr"),
    ("eightfold.ai", "eightfold"),
    ("recruitee.com", "generic"),
    ("workable.com", "generic"),
    ("pinpointhq.com", "generic"),
    ("dover.com", "generic"),
]

# Domains that may appear inside iframes on a company's own careers page
_IFRAME_ATS_DOMAINS = [
    "greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com",
    "smartrecruiters.com", "icims.com", "jobvite.com", "breezy.hr",
    "workable.com", "recruitee.com", "eightfold.ai",
]

_APPLY_LINK_SELECTORS = [
    "a[href*='myworkdayjobs']",
    "a[href*='greenhouse.io/job']",
    "a[href*='lever.co/']",
    "a[href*='jobs.ashbyhq.com']",
    "a[href*='taleo.net']",
    "a[href*='icims.com']",
    "a[href*='smartrecruiters.com/go']",
    "a[href*='jobvite.com/']",
    "a[href*='breezy.hr/']",
    "a[href*='workable.com/j/']",
]

_APPLY_BUTTON_TEXT_SELECTORS = [
    "a:text-is('Apply')", "a:text-is('Apply Now')",
    "a:text-is('Apply for this job')", "a:text-is('Apply for Job')",
    "button:text-is('Apply')", "button:text-is('Apply Now')",
    "button:text-matches('Apply', 'i')",
]

_PERSONAL_LABELS = frozenset({
    "first name", "last name", "first", "last", "name", "full name",
    "email", "email address", "phone", "phone number", "mobile",
    "linkedin", "linkedin profile", "linkedin url",
})


# ── Helpers ───────────────────────────────────────────────────────────────────

def _detect_platform(url: str) -> str:
    url_lower = url.lower()
    for fragment, platform in _ATS_URL_MAP:
        if fragment in url_lower:
            return platform
    return "generic"


def _load_selectors(platform: str) -> dict:
    if platform == "generic":
        return {}
    try:
        module = importlib.import_module(f"app.automation.selectors.{platform}")
        return module.SELECTORS
    except (ImportError, AttributeError) as exc:
        logger.warning("No selector module for '%s': %s", platform, exc)
        return {}


def _session_dir() -> Path:
    from app.config import get_settings
    p = Path(get_settings().SESSION_DIR).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


def _session_path(url: str) -> Path:
    netloc = urlparse(url).netloc or "unknown"
    safe = netloc.replace(".", "_").replace(":", "_")
    return _session_dir() / f"{safe}.json"


def _load_storage_state(url: str) -> dict | None:
    path = _session_path(url)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return None
    return None


async def _save_storage_state(context, url: str) -> None:
    try:
        state = await context.storage_state()
        _session_path(url).write_text(json.dumps(state))
    except Exception as exc:
        logger.warning("Could not save session: %s", exc)


async def _find_ats_frame(page):
    """Return the iframe context if an ATS form is embedded, else return the page itself.

    Many companies embed Greenhouse / Lever / Workday inside an iframe on their
    own careers page. Selectors run on the host page find nothing. We detect the
    iframe, switch into it, and return its frame handle so callers can use it
    transparently just like a page.
    """
    try:
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            src = frame.url or ""
            if any(domain in src for domain in _IFRAME_ATS_DOMAINS):
                logger.info("Found ATS iframe: %s", src)
                return frame
        # Also check iframes in DOM whose src we can read
        iframes = await page.query_selector_all("iframe[src]")
        for iframe_el in iframes:
            src = await iframe_el.get_attribute("src") or ""
            if any(domain in src for domain in _IFRAME_ATS_DOMAINS):
                frame = await iframe_el.content_frame()
                if frame:
                    logger.info("Found ATS iframe (DOM): %s", src)
                    return frame
    except Exception as exc:
        logger.debug("iframe scan error: %s", exc)
    return page


async def _page_has_form(page) -> bool:
    try:
        inputs = await page.query_selector_all(
            "input[type='text'], input[type='email'], input[type='tel'], textarea"
        )
        if len(inputs) >= 2:
            return True
        if await page.query_selector("[data-automation-id='stepName']"):
            return True
        if await page.query_selector("form#application-form, form.application-form, #application_form"):
            return True
    except Exception:
        pass
    return False


async def _resolve_apply_url(page, context=None) -> str | None:
    """Find the actual application form URL from a job listing page.

    Handles three cases:
      1. Direct href on an anchor (no click needed)
      2. Click → same-tab navigation
      3. Click → new tab opens (target="_blank")
    """
    # 1. Href-pattern links — no click needed
    for sel in _APPLY_LINK_SELECTORS:
        try:
            el = await page.query_selector(sel)
            if el:
                href = await el.get_attribute("href")
                if href and href.startswith("http"):
                    logger.info("Resolved apply URL (href pattern): %s", href)
                    return href
        except Exception:
            pass

    # 2 & 3. Button/link click — watch for same-tab nav OR new tab
    for sel in _APPLY_BUTTON_TEXT_SELECTORS:
        try:
            el = await page.query_selector(sel)
            if not el:
                continue

            href = await el.get_attribute("href")
            if href and href.startswith("http") and any(d in href for d in _IFRAME_ATS_DOMAINS):
                logger.info("Resolved apply URL (text button href): %s", href)
                return href

            prev_url = page.url
            pages_before = len(context.pages) if context else 0

            await el.click()
            await page.wait_for_timeout(3_000)

            # New tab opened?
            if context and len(context.pages) > pages_before:
                new_page = context.pages[-1]
                await new_page.wait_for_load_state("domcontentloaded")
                new_url = new_page.url
                if new_url and new_url != "about:blank":
                    logger.info("Resolved apply URL (new tab): %s", new_url)
                    return new_url

            # Same-tab navigation?
            if page.url != prev_url:
                logger.info("Resolved apply URL (same-tab nav): %s", page.url)
                return page.url

        except Exception:
            pass
    return None


# ── LLM form analysis ─────────────────────────────────────────────────────────

_FORM_FIELD_EXTRACTOR_JS = """() => {
    const SKIP_TYPES = new Set(['hidden','submit','button','reset','image','file']);
    const fields = [];
    const seen = new Set();

    document.querySelectorAll('input, textarea, select').forEach(el => {
        const type = (el.getAttribute('type') || el.tagName).toLowerCase();
        if (SKIP_TYPES.has(type)) return;

        // Visibility check
        const rect = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);
        if (rect.width === 0 || rect.height === 0) return;
        if (style.display === 'none' || style.visibility === 'hidden') return;

        // Build selector
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

        // Find label text
        let label = el.getAttribute('aria-label') || el.getAttribute('placeholder') || '';
        if (!label && el.id) {
            const lbl = document.querySelector('label[for="' + el.id + '"]');
            if (lbl) label = lbl.innerText.trim();
        }
        if (!label) {
            let parent = el.parentElement;
            for (let i = 0; i < 4 && parent; i++) {
                const lbl = parent.querySelector('label');
                if (lbl && lbl !== el) { label = lbl.innerText.trim(); break; }
                parent = parent.parentElement;
            }
        }

        // Options for select
        const options = el.tagName === 'SELECT'
            ? Array.from(el.options).map(o => o.text.trim()).filter(Boolean).slice(0, 20)
            : [];

        fields.push({
            selector,
            tag: el.tagName.toLowerCase(),
            type,
            label: label.replace(/[*:]/g, '').trim(),
            current_value: el.value || '',
            options,
            required: el.required,
        });
    });
    return fields;
}"""


async def _extract_form_fields(page) -> list[dict]:
    try:
        return await page.evaluate(_FORM_FIELD_EXTRACTOR_JS) or []
    except Exception as exc:
        logger.warning("Form field extraction failed: %s", exc)
        return []


async def _llm_map_fields(
    fields: list[dict],
    profile: dict,
    llm: "LLMProvider",
) -> list[dict]:
    """Ask the LLM to produce fill instructions for each visible field.

    Retries once with a simplified prompt if the first attempt returns nothing.
    """
    if not fields:
        return []

    unfilled = [f for f in fields if not f.get("current_value") and f.get("label")]
    if not unfilled:
        return []

    def _make_prompt(field_list: list[dict]) -> str:
        return f"""You are filling a job application form on behalf of this applicant.

APPLICANT PROFILE:
{json.dumps(profile, indent=2)}

VISIBLE FORM FIELDS (only unfilled fields shown):
{json.dumps(field_list, indent=2)}

Return a JSON array of fill actions. Each action:
{{
  "selector": "<exact CSS selector from the field list above>",
  "action": "fill" | "select" | "check",
  "value": "<string for fill/select, true/false for check>"
}}

Rules:
- ONLY use selectors that appear verbatim in the field list above.
- "fill"   → text input or textarea.
- "select" → <select> dropdown — value must exactly match one of the listed options.
- "check"  → checkbox — true to check, false to uncheck.
- Skip any field you have no profile data for. Never invent data.
- For cover_letter/summary/message fields use the profile summary.
- For work_authorization/eligible to work → use profile work_authorization value.
- For years_of_experience fields → use profile years_of_experience value.
- Return ONLY the raw JSON array. No markdown fences, no explanation.
"""

    def _parse(raw: str) -> list[dict]:
        cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned.strip())
        result = json.loads(cleaned)
        return result if isinstance(result, list) else []

    # First attempt — full field list
    try:
        raw = llm.generate(_make_prompt(unfilled), max_tokens=2000)
        actions = _parse(raw)
        if actions:
            return actions
        logger.warning("LLM returned empty action list — retrying with simplified fields")
    except Exception as exc:
        logger.warning("LLM field mapping attempt 1 failed: %s", exc)

    # Retry — strip non-essential metadata to reduce noise
    try:
        simplified = [
            {"selector": f["selector"], "label": f["label"],
             "type": f["type"], "options": f.get("options", [])}
            for f in unfilled
        ]
        raw = llm.generate(_make_prompt(simplified), max_tokens=2000)
        return _parse(raw)
    except Exception as exc:
        logger.warning("LLM field mapping attempt 2 failed: %s", exc)
    return []


async def _execute_fill_actions(page, actions: list[dict], fields_filled: list, fields_skipped: list) -> None:
    """Execute LLM-generated fill actions on the page."""
    for action in actions:
        selector = action.get("selector", "")
        act = action.get("action", "fill")
        value = action.get("value")

        if not selector or value is None or value == "":
            continue
        try:
            el = await page.query_selector(selector)
            if not el or not await el.is_visible():
                fields_skipped.append(selector[:50])
                continue

            if act == "fill":
                await el.fill(str(value))
                fields_filled.append(selector[:50])
                logger.debug("LLM fill: %s = %r", selector[:50], str(value)[:40])

            elif act == "select":
                try:
                    await el.select_option(label=str(value))
                except Exception:
                    await el.select_option(value=str(value))
                fields_filled.append(selector[:50])

            elif act == "check":
                if bool(value) != await el.is_checked():
                    await el.set_checked(bool(value))
                fields_filled.append(selector[:50])

        except Exception as exc:
            logger.debug("Fill action failed (%s): %s", selector[:50], exc)
            fields_skipped.append(selector[:50])


# ── Shared Q&A filler ─────────────────────────────────────────────────────────

async def _fill_visible_questions(
    page,
    answer_generator: Callable,
    fields_filled: list,
    fields_skipped: list,
) -> None:
    """Fill visible textarea/text inputs that look like open-ended questions."""
    try:
        all_elements = await page.query_selector_all("textarea, input[type='text']")
        elements = []
        for el in all_elements:
            try:
                if await el.is_visible():
                    elements.append(el)
            except Exception:
                pass
    except Exception:
        return

    for element in elements:
        try:
            element_id = await element.get_attribute("id") or ""
            label_text = ""
            if element_id:
                lbl = await page.query_selector(f"label[for='{element_id}']")
                if lbl:
                    label_text = (await lbl.inner_text()).strip()
            if not label_text:
                label_text = (
                    await element.get_attribute("aria-label") or
                    await element.get_attribute("placeholder") or ""
                ).strip()

            if not label_text or label_text.lower() in _PERSONAL_LABELS:
                continue
            if len(label_text) < 6:
                continue

            current_val = await element.input_value()
            if current_val:
                continue

            try:
                answer = answer_generator(label_text)
                if answer:
                    await element.fill(answer)
                    fields_filled.append(f"answer:{label_text[:40]}")
                    logger.info("Answered: %r", label_text[:80])
            except Exception as exc:
                logger.warning("answer_generator failed for %r: %s", label_text[:60], exc)
        except Exception:
            pass


async def _upload_resume_to_frame(frame, resume_abs_path: str) -> bool:
    """Upload resume to ANY file input on the frame — handles hidden inputs.

    Root cause fix: file inputs on virtually every ATS are CSS-hidden
    (opacity:0 / position:absolute / width:1px) so is_visible() returns False.
    Playwright's set_input_files() works on hidden elements directly — we just
    must NOT filter by visibility.
    """
    if not resume_abs_path or not Path(resume_abs_path).exists():
        return False
    try:
        file_inputs = await frame.query_selector_all("input[type='file']")
        for fi in file_inputs:
            try:
                await fi.set_input_files(resume_abs_path)
                logger.info("Uploaded resume to file input")
                await frame.wait_for_timeout(2_000)
                return True
            except Exception:
                pass
        # Some ATS use a drag-and-drop zone with a hidden input behind a label
        # Try clicking the label/button that triggers file picker, then set files
        for sel in [
            "label[for*='resume'], label[for*='file'], label[for*='upload']",
            "button:text-matches('upload|attach|resume', 'i')",
            "[class*='upload']:not(input)",
        ]:
            try:
                btn = await frame.query_selector(sel)
                if btn:
                    # Find the associated file input after clicking
                    await btn.click()
                    await frame.wait_for_timeout(500)
                    fis = await frame.query_selector_all("input[type='file']")
                    for fi in fis:
                        try:
                            await fi.set_input_files(resume_abs_path)
                            logger.info("Uploaded resume via label click")
                            await frame.wait_for_timeout(2_000)
                            return True
                        except Exception:
                            pass
            except Exception:
                pass
    except Exception as exc:
        logger.warning("Resume upload failed: %s", exc)
    return False


async def _detect_required_unfilled(frame) -> list[str]:
    """Return labels of required fields that are still empty — for diagnostics."""
    try:
        return await frame.evaluate("""() => {
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
        }""") or []
    except Exception:
        return []


# ── Consent / cookie / privacy modal dismissal ───────────────────────────────

_MODAL_DISMISS_SELECTORS = [
    # Generic accept/agree buttons
    "button:text-matches('Accept All', 'i')",
    "button:text-matches('Accept Cookies', 'i')",
    "button:text-matches('Accept all cookies', 'i')",
    "button:text-matches('I Accept', 'i')",
    "button:text-matches('I Agree', 'i')",
    "button:text-matches('Agree', 'i')",
    "button:text-matches('Allow All', 'i')",
    "button:text-matches('OK', 'i')",
    "button:text-matches('Got it', 'i')",
    "button:text-matches('Continue', 'i')",
    "button:text-matches('Proceed', 'i')",
    # Close buttons
    "button[aria-label='Close']",
    "button[aria-label='Dismiss']",
    "button[aria-label='close']",
    "[class*='modal'] button[class*='close']",
    "[class*='cookie'] button[class*='close']",
    "[class*='consent'] button[class*='close']",
    # ATS-specific privacy modals (Eightfold, Greenhouse, etc.)
    "button:text-matches('Acknowledge', 'i')",
    "button:text-matches('I understand', 'i')",
    "button:text-matches('Confirm', 'i')",
    "[data-testid*='accept']",
    "[data-testid*='consent']",
]


async def _dismiss_modals(page) -> int:
    """Click visible consent / cookie / privacy modal buttons. Returns count dismissed."""
    dismissed = 0
    for sel in _MODAL_DISMISS_SELECTORS:
        try:
            btn = await page.query_selector(sel)
            if btn and await btn.is_visible():
                await btn.click()
                await page.wait_for_timeout(800)
                dismissed += 1
                logger.info("Dismissed modal: %s", sel)
                break  # one dismiss per call — re-call to dismiss chained modals
        except Exception:
            pass
    return dismissed


# ── Workday multi-step handler ────────────────────────────────────────────────

class _WorkdayHandler:

    async def run(self, page, context, settings, tailored_material, resume_abs_path,
                  auto_submit, answer_generator, fields_filled, fields_skipped) -> bool:
        await page.wait_for_timeout(2_000)

        use_last = await page.query_selector(
            "button[data-automation-id='useLastApplication'], "
            "a[data-automation-id='useLastApplication']"
        )
        if use_last and await use_last.is_visible():
            logger.info("Workday: using last application")
            await use_last.click()
            await page.wait_for_timeout(3_000)
            fields_filled.append("use_last_application")
            return await self._click_to_submit(page, auto_submit, fields_filled, fields_skipped)

        apply_manually = await page.query_selector(
            "button[data-automation-id='applyManually'], "
            "a[data-automation-id='applyManually']"
        )
        if apply_manually and await apply_manually.is_visible():
            logger.info("Workday: apply manually")
            await apply_manually.click()
            await page.wait_for_timeout(3_000)

        await self._handle_account(page, settings, fields_filled, fields_skipped)
        await self._upload_resume(page, resume_abs_path, fields_filled, fields_skipped)

        for step_num in range(18):
            logger.info("Workday: filling step %d", step_num + 1)
            await self._fill_step(page, settings, tailored_material, answer_generator, fields_filled, fields_skipped)
            await page.wait_for_timeout(1_000)

            review_btn = await page.query_selector(
                "button[data-automation-id='bottom-navigation-review-button']"
            )
            if review_btn and await review_btn.is_visible():
                logger.info("Workday: clicking Review")
                await review_btn.click()
                await page.wait_for_timeout(2_000)
                return await self._click_to_submit(page, auto_submit, fields_filled, fields_skipped)

            next_btn = await page.query_selector(
                "button[data-automation-id='bottom-navigation-next-button']"
            )
            if not next_btn or not await next_btn.is_visible():
                break
            if not await next_btn.is_enabled():
                logger.warning("Workday: Next disabled at step %d", step_num + 1)
                break

            await next_btn.click()
            await page.wait_for_timeout(2_500)

        return await self._click_to_submit(page, auto_submit, fields_filled, fields_skipped)

    async def _handle_account(self, page, settings, fields_filled, fields_skipped):
        await page.wait_for_timeout(1_500)
        if await page.query_selector("[data-automation-id='stepName']"):
            return
        email_el = await page.query_selector("input[type='email'], input[data-automation-id='email']")
        if not email_el:
            return
        email = settings.APPLICANT_EMAIL or os.environ.get("APPLICANT_EMAIL", "")
        password = settings.APPLICANT_PASSWORD or os.environ.get("APPLICANT_PASSWORD", "")
        if not email or not password:
            logger.warning("Workday: sign-in page found but APPLICANT_EMAIL/PASSWORD not set")
            return
        try:
            await email_el.fill(email)
            fields_filled.append("wd_email")
        except Exception:
            pass

        create_link = await page.query_selector(
            "a[data-automation-id='createAccountLink'], a:text-is('Create Account'), a:text-is('Register')"
        )
        if create_link and await create_link.is_visible():
            await create_link.click()
            await page.wait_for_timeout(2_000)
            email_el = await page.query_selector("input[type='email'], input[data-automation-id='email']")
            if email_el:
                await email_el.fill(email)

        pw_el = await page.query_selector("input[type='password']")
        if pw_el:
            try:
                await pw_el.fill(password)
                fields_filled.append("wd_password")
            except Exception:
                pass

        pw_els = await page.query_selector_all("input[type='password']")
        if len(pw_els) >= 2:
            try:
                await pw_els[1].fill(password)
                fields_filled.append("wd_confirm_password")
            except Exception:
                pass

        checkbox = await page.query_selector(
            "input[type='checkbox'][data-automation-id*='termsAndConditions'], "
            "input[type='checkbox'][name*='terms'], input[type='checkbox'][name*='agree']"
        )
        if checkbox and not await checkbox.is_checked():
            try:
                await checkbox.check()
                fields_filled.append("wd_terms_checkbox")
            except Exception:
                pass

        submit_btn = await page.query_selector(
            "button[data-automation-id='createAccountSubmitButton'], "
            "button[data-automation-id='signInSubmitButton'], button[type='submit']"
        )
        if submit_btn:
            try:
                await submit_btn.click()
                await page.wait_for_timeout(4_000)
                fields_filled.append("wd_account_submit")
            except Exception as exc:
                logger.warning("Workday account submit failed: %s", exc)

    async def _upload_resume(self, page, resume_abs_path, fields_filled, fields_skipped):
        if not resume_abs_path or not Path(resume_abs_path).exists():
            fields_skipped.append("resume_upload")
            return
        if await _upload_resume_to_frame(page, resume_abs_path):
            fields_filled.append("resume_upload")
        else:
            fields_skipped.append("resume_upload")

    async def _fill_step(self, page, settings, tailored_material, answer_generator, fields_filled, fields_skipped):
        selectors = _load_selectors("workday")
        personal = _build_personal(settings)

        for field, selector in selectors.items():
            if field in ("submit_button", "review_button", "resume_upload"):
                continue
            value = tailored_material.get("summary", "") if field == "cover_letter" else personal.get(field, "")
            if not value:
                continue
            try:
                el = await page.query_selector(selector)
                if el and await el.is_visible():
                    current = await el.input_value()
                    if current:
                        continue
                    await el.fill(value)
                    fields_filled.append(f"wd_{field}")
            except Exception as exc:
                logger.debug("Workday fill %s: %s", field, exc)

        if answer_generator:
            await _fill_visible_questions(page, answer_generator, fields_filled, fields_skipped)

    async def _click_to_submit(self, page, auto_submit, fields_filled, fields_skipped) -> bool:
        if not auto_submit:
            logger.info("Workday: stopped at review (AUTO_SUBMIT=false)")
            return False
        submit_btn = await page.query_selector(
            "button[data-automation-id='bottom-navigation-next-button']"
        )
        if submit_btn and await submit_btn.is_visible() and await submit_btn.is_enabled():
            await submit_btn.click()
            await page.wait_for_timeout(3_000)
            fields_filled.append("submit_button")
            return True
        fields_skipped.append("submit_button")
        return False


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_personal(settings) -> dict:
    return {
        "first_name": settings.APPLICANT_FIRST_NAME or os.environ.get("APPLICANT_FIRST_NAME", ""),
        "last_name": settings.APPLICANT_LAST_NAME or os.environ.get("APPLICANT_LAST_NAME", ""),
        "email": settings.APPLICANT_EMAIL or os.environ.get("APPLICANT_EMAIL", ""),
        "phone": settings.APPLICANT_PHONE or os.environ.get("APPLICANT_PHONE", ""),
        "linkedin": settings.APPLICANT_LINKEDIN or os.environ.get("APPLICANT_LINKEDIN", ""),
    }


def _build_profile(settings, tailored_material: dict) -> dict:
    """Build a rich applicant profile sent to the LLM for field mapping.

    The richer this dict, the more fields the LLM can fill accurately.
    Add any extra facts here — years of experience, work auth, etc.
    """
    p = _build_personal(settings)
    first = p["first_name"]
    last  = p["last_name"]
    return {
        **p,
        "full_name":           f"{first} {last}".strip(),
        "summary":             tailored_material.get("summary", ""),
        "keywords":            tailored_material.get("keywords", []),
        "cover_letter":        tailored_material.get("summary", ""),
        # Common ATS questions — LLM uses these to fill extra form fields
        "years_of_experience": settings.APPLICANT_YEARS_EXP,
        "current_employer":    settings.APPLICANT_CURRENT_EMPLOYER,
        "current_title":       settings.APPLICANT_CURRENT_TITLE,
        "work_authorization":  settings.APPLICANT_WORK_AUTH,
        "visa_sponsorship":    settings.APPLICANT_VISA_NEEDED,
        "willing_to_relocate": settings.APPLICANT_RELOCATE,
        "salary_expectation":  settings.APPLICANT_SALARY_EXPECT,
        "notice_period":       settings.APPLICANT_NOTICE_PERIOD,
        "github":              settings.APPLICANT_GITHUB,
        "portfolio":           settings.APPLICANT_PORTFOLIO,
        "city":                settings.APPLICANT_CITY,
        "country":             settings.APPLICANT_COUNTRY,
    }


# ── FormFiller ────────────────────────────────────────────────────────────────

class FormFiller:
    """Universal job application form filler.

    Works on any website:
      - Known ATS (Workday, Greenhouse, Lever, Ashby) → fast selector-based fill
      - Unknown / company-specific forms → LLM-powered field analysis
      - Heuristic fallback when no LLM is configured

    Parameters
    ----------
    llm   Optional LLMProvider instance. When supplied, unknown forms are
          analysed by the LLM which maps applicant profile data to each
          visible field automatically.
    """

    def __init__(self, llm: Optional["LLMProvider"] = None) -> None:
        self.llm = llm

    async def fill(
        self,
        application_url: str,
        tailored_material: dict,
        resume_path: str,
        answers: dict[str, str],
        auto_submit: bool = False,
        answer_generator: Optional[Callable] = None,
    ) -> dict:
        """Fill a job application form at *application_url*.

        Parameters
        ----------
        application_url   URL of the job listing or direct application form.
        tailored_material Dict from ResumeTailor (summary, bullets, keywords).
        resume_path       Path to the tailored resume file (DOCX or PDF).
        answers           Pre-computed {question: answer} dict for fast fills.
        auto_submit       Click the submit button if True.
        answer_generator  Callable(question_text) -> str for on-the-fly answers.
        """
        from playwright.async_api import async_playwright
        from app.config import get_settings

        settings = get_settings()
        resume_abs_path = str(Path(resume_path).resolve())

        fields_filled: list[str] = []
        fields_skipped: list[str] = []
        submitted = False
        fill_error: str | None = None
        final_url = application_url
        platform = "unknown"

        def _answer_fn(question: str) -> str:
            q_lower = question.lower()
            for q, a in answers.items():
                if a and (q_lower in q.lower() or q.lower() in q_lower):
                    return a
            if answer_generator:
                return answer_generator(question)
            return ""

        storage_state = _load_storage_state(application_url)

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=False)
            ctx_kwargs: dict = {
                "viewport": {"width": 1280, "height": 900},
                "user_agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            }
            if storage_state:
                ctx_kwargs["storage_state"] = storage_state
            context = await browser.new_context(**ctx_kwargs)
            page = await context.new_page()

            try:
                logger.info("FormFiller: navigating to %s", application_url)
                await page.goto(application_url, wait_until="domcontentloaded", timeout=60_000)
                await page.wait_for_timeout(3_000)
                await _dismiss_modals(page)

                # ── JD listing hop ───────────────────────────────────────────
                if not await _page_has_form(page):
                    logger.info("No form detected — looking for Apply link")
                    apply_url = await _resolve_apply_url(page, context)
                    if apply_url and apply_url != application_url:
                        logger.info("Following apply URL: %s", apply_url)
                        # New tab may already be open from _resolve_apply_url click
                        if context and len(context.pages) > 1:
                            active_page = context.pages[-1]
                            await active_page.wait_for_load_state("domcontentloaded")
                        else:
                            await page.goto(apply_url, wait_until="domcontentloaded", timeout=60_000)
                            active_page = page
                        await active_page.wait_for_timeout(3_000)
                        final_url = active_page.url
                        page = active_page  # work on the new tab going forward
                    else:
                        logger.warning("Could not find apply link on listing page")

                # ── Iframe detection — switch context if form is inside an iframe ──
                frame = await _find_ats_frame(page)
                if frame is not page:
                    logger.info("Switching to ATS iframe for form filling")

                platform = _detect_platform(page.url)
                if frame is not page:
                    platform = _detect_platform(frame.url)
                logger.info("Platform: %s  url: %s", platform, page.url)

                # ── Workday ──────────────────────────────────────────────────
                if platform == "workday":
                    if not await _page_has_form(frame):
                        wd_apply = await frame.query_selector(
                            "a[data-automation-id='applyLink'], "
                            "a:text-is('Apply'), button:text-is('Apply')"
                        )
                        if wd_apply:
                            await wd_apply.click()
                            await page.wait_for_timeout(3_000)
                            final_url = page.url
                    submitted = await _WorkdayHandler().run(
                        page=frame, context=context, settings=settings,
                        tailored_material=tailored_material,
                        resume_abs_path=resume_abs_path,
                        auto_submit=auto_submit, answer_generator=_answer_fn,
                        fields_filled=fields_filled, fields_skipped=fields_skipped,
                    )

                # ── Known ATS (selector-based fast path) ─────────────────────
                elif platform in ("greenhouse", "lever", "ashby", "smartrecruiters", "icims"):
                    await self._fill_ats_form(
                        page=frame, platform=platform, settings=settings,
                        tailored_material=tailored_material,
                        resume_abs_path=resume_abs_path, auto_submit=auto_submit,
                        answer_generator=_answer_fn,
                        fields_filled=fields_filled, fields_skipped=fields_skipped,
                    )

                # ── Any other form — LLM or heuristic ────────────────────────
                else:
                    await self._fill_universal(
                        page=frame, settings=settings,
                        tailored_material=tailored_material,
                        answer_generator=_answer_fn,
                        fields_filled=fields_filled, fields_skipped=fields_skipped,
                        auto_submit=auto_submit,
                        resume_abs_path=resume_abs_path,
                    )

                await _save_storage_state(context, final_url)

            except Exception as exc:
                fill_error = str(exc)
                logger.error("FormFiller error: %s", exc, exc_info=True)
            finally:
                if not auto_submit and not fill_error:
                    await asyncio.sleep(3)
                await browser.close()

        return {
            "platform": platform,
            "fields_filled": fields_filled,
            "fields_skipped": fields_skipped,
            "submitted": submitted,
            "error": fill_error,
            "url": final_url,
        }

    # ── Known ATS (selector-based) ────────────────────────────────────────────

    async def _fill_ats_form(
        self, page, platform, settings, tailored_material, resume_abs_path,
        auto_submit, answer_generator, fields_filled, fields_skipped,
    ) -> None:
        selectors = _load_selectors(platform)
        personal = _build_personal(settings)

        for field in ("first_name", "last_name", "email", "phone", "linkedin"):
            sel = selectors.get(field)
            value = personal.get(field, "")
            # When the ATS has no separate last_name field (e.g. Lever uses a single
            # full-name input), concatenate first + last into the first_name slot.
            if field == "first_name" and not selectors.get("last_name"):
                last = personal.get("last_name", "")
                value = f"{value} {last}".strip() if last else value
            if not sel or not value:
                fields_skipped.append(field)
                continue
            try:
                el = await page.query_selector(sel)
                if el:
                    await el.fill(value)
                    fields_filled.append(field)
                    logger.info("Filled %s", field)
                else:
                    fields_skipped.append(field)
            except Exception as exc:
                logger.warning("Could not fill %s (%s): %s", field, sel, exc)
                fields_skipped.append(field)

        if Path(resume_abs_path).exists():
            if await _upload_resume_to_frame(page, resume_abs_path):
                fields_filled.append("resume_upload")
            else:
                fields_skipped.append("resume_upload")
        else:
            fields_skipped.append("resume_upload")

        cover_sel = selectors.get("cover_letter")
        cover_text = tailored_material.get("summary", "")
        if cover_sel and cover_text:
            try:
                await page.fill(cover_sel, cover_text)
                fields_filled.append("cover_letter")
            except Exception as exc:
                logger.warning("Cover letter fill failed: %s", exc)

        await _fill_visible_questions(page, answer_generator, fields_filled, fields_skipped)

        if auto_submit:
            submit_sel = selectors.get("submit_button")
            if submit_sel:
                try:
                    await page.click(submit_sel)
                    fields_filled.append("submit_button")
                except Exception as exc:
                    logger.error("Submit failed: %s", exc)
                    fields_skipped.append("submit_button")

    # ── Universal fill (LLM + heuristic) ─────────────────────────────────────

    async def _fill_universal(
        self, page, settings, tailored_material, answer_generator,
        fields_filled, fields_skipped, auto_submit, resume_abs_path: str = "",
    ) -> None:
        """Fill any unknown form.

        Tries multi-step detection (Next buttons) then uses LLM analysis
        if available, otherwise falls back to name/id heuristics.
        """
        for step_attempt in range(15):  # was 8 — enterprise ATS can have 12+ steps
            await _dismiss_modals(page)
            await self._fill_page_once(
                page, settings, tailored_material, answer_generator,
                fields_filled, fields_skipped, resume_abs_path=resume_abs_path,
            )
            await page.wait_for_timeout(1_000)

            # Check for a submit button
            if auto_submit:
                submit_btn = await self._find_submit_button(page)
                if submit_btn and await submit_btn.is_visible() and await submit_btn.is_enabled():
                    logger.info("Universal: clicking submit (step %d)", step_attempt + 1)
                    await submit_btn.click()
                    await page.wait_for_timeout(3_000)
                    fields_filled.append("submit_button")
                    return

            # Check for next-step button
            next_btn = await self._find_next_button(page)
            if not next_btn or not await next_btn.is_visible():
                break

            if not await next_btn.is_enabled():
                # Diagnose what is blocking Next — required fields not filled
                blocking = await _detect_required_unfilled(page)
                if blocking:
                    logger.warning(
                        "Next disabled — required fields still empty: %s",
                        blocking[:10],
                    )
                    fields_skipped.extend(f"required:{f}" for f in blocking[:10])
                else:
                    logger.warning("Next button disabled at step %d (unknown reason)", step_attempt + 1)
                break

            logger.info("Universal: clicking Next (step %d)", step_attempt + 1)
            await next_btn.click()
            await page.wait_for_timeout(2_500)

    async def _fill_page_once(
        self, page, settings, tailored_material, answer_generator,
        fields_filled, fields_skipped, resume_abs_path: str = "",
    ) -> None:
        """Fill all fillable fields on the current page state."""
        if self.llm:
            # LLM path: extract all fields, let LLM map profile → values
            profile = _build_profile(settings, tailored_material)
            field_data = await _extract_form_fields(page)
            actions = await _llm_map_fields(field_data, profile, self.llm)
            if actions:
                await _execute_fill_actions(page, actions, fields_filled, fields_skipped)
                logger.info("LLM filled %d fields", len(fields_filled))
            else:
                # LLM returned nothing — fall through to heuristic
                await self._fill_heuristic(
                    page, settings, tailored_material, fields_filled, fields_skipped
                )
        else:
            await self._fill_heuristic(
                page, settings, tailored_material, fields_filled, fields_skipped
            )

        # Always try Q&A fields via answer_generator
        if answer_generator:
            await _fill_visible_questions(page, answer_generator, fields_filled, fields_skipped)

        # Always try resume upload — use tailored resume path, fall back to base
        upload_path = resume_abs_path or str(Path(settings.RESUME_PATH).expanduser().resolve())
        if upload_path and Path(upload_path).exists():
            if "resume_upload" not in fields_filled:
                if await _upload_resume_to_frame(page, upload_path):
                    fields_filled.append("resume_upload")

    async def _fill_heuristic(
        self, page, settings, tailored_material, fields_filled, fields_skipped
    ) -> None:
        """Name/id/placeholder-pattern based fill when LLM is unavailable."""
        personal = _build_personal(settings)
        variants: dict[str, list[str]] = {
            "first_name": ["first_name", "firstname", "first-name", "fname", "given_name", "given-name"],
            "last_name": ["last_name", "lastname", "last-name", "lname", "surname", "family_name"],
            "email": ["email", "e-mail", "emailaddress", "email_address", "email-address"],
            "phone": ["phone", "telephone", "mobile", "phonenumber", "phone_number", "contact"],
            "linkedin": ["linkedin", "linkedin_url", "linkedinurl", "linkedin-url", "linkedIn"],
        }
        for field, var_list in variants.items():
            value = personal.get(field, "")
            if not value:
                fields_skipped.append(field)
                continue
            filled = False
            for var in var_list:
                try:
                    el = await page.query_selector(
                        f"input[name='{var}'], input[id='{var}'], "
                        f"input[placeholder*='{var}'], input[autocomplete='{field}']"
                    )
                    if el and await el.is_visible():
                        await el.fill(value)
                        fields_filled.append(field)
                        filled = True
                        break
                except Exception:
                    pass
            if not filled:
                fields_skipped.append(field)

        # Cover letter / summary
        summary = tailored_material.get("summary", "")
        if summary:
            for sel in ["textarea[name*='cover'], textarea[id*='cover'], textarea[name*='summary']"]:
                try:
                    el = await page.query_selector(sel)
                    if el and await el.is_visible():
                        current = await el.input_value()
                        if not current:
                            await el.fill(summary)
                            fields_filled.append("cover_letter")
                            break
                except Exception:
                    pass

    async def _find_next_button(self, page):
        selectors = [
            "button:text-is('Next')", "button:text-is('Continue')",
            "button:text-is('Next Step')", "button:text-is('Next →')",
            "input[value='Next']", "input[value='Continue']",
            "[class*='next-button']", "[class*='btn-next']",
            "[data-automation-id*='next']",
        ]
        for sel in selectors:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    return el
            except Exception:
                pass
        return None

    async def _find_submit_button(self, page):
        selectors = [
            "button[type='submit']", "input[type='submit']",
            "button:text-is('Submit')", "button:text-is('Submit Application')",
            "button:text-is('Apply')", "button:text-is('Send Application')",
            "[data-automation-id='submit-button']", "[data-testid='submit-application-button']",
            "#submit_app", "button[id*='submit']",
        ]
        for sel in selectors:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    return el
            except Exception:
                pass
        return None

    def fill_sync(
        self,
        application_url: str,
        tailored_material: dict,
        resume_path: str,
        answers: dict[str, str],
        auto_submit: bool = False,
        answer_generator: Optional[Callable] = None,
    ) -> dict:
        return asyncio.run(
            self.fill(
                application_url=application_url,
                tailored_material=tailored_material,
                resume_path=resume_path,
                answers=answers,
                auto_submit=auto_submit,
                answer_generator=answer_generator,
            )
        )
