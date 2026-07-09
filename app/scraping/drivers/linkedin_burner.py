"""LinkedIn Burner Account social post scraper."""
from __future__ import annotations

import logging
import random
import urllib.parse
from app.scraping.drivers import BaseJobBoardDriver, _apply_stealth, _human_delay, _natural_scroll

logger = logging.getLogger(__name__)

_LOGIN_URL = "https://www.linkedin.com/login"
# Search content/posts globally, sorting by latest to capture fresh hidden opportunities
_SEARCH_URL = "https://www.linkedin.com/search/results/content/?keywords=hiring%20{role}&sortBy=%22date_posted%22"


class LinkedInBurnerDriver(BaseJobBoardDriver):
    name = "linkedin_burner"
    home_url = "https://www.linkedin.com"
    login_url = _LOGIN_URL
    requires_login = True
    region = "international"

    async def is_logged_in(self, page) -> bool:
        from app.config import get_settings
        s = get_settings()
        try:
            await _apply_stealth(page)
            await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=s.BROWSER_TIMEOUT_MS)
            await _human_delay(page, 2000, 4000)

            # Check if the URL redirected to /feed/ or if profile elements are visible
            current_url = page.url
            if "/feed" in current_url or "/search/" in current_url:
                return True

            profile = await page.query_selector(
                ".global-nav__me, .global-nav__me-photo, img[class*='nav-me'], "
                "[class*='feed-identity'], a[href*='/in/'], [class*='me-photo']"
            )
            if profile:
                return True

            # If login form fields are visible → not logged in
            login_form = await page.query_selector("#username, #password, input[name='session_key']")
            return login_form is None
        except Exception as e:
            logger.debug("LinkedIn is_logged_in check failed: %s", e)
            return False

    async def search(self, page, role: str, location: str, limit: int = 15) -> list[dict]:
        """Perform a global LinkedIn search for hiring posts.

        Utilizes humanized delays, natural mouse scroll movements, and multi-layered DOM fallbacks.
        """
        from app.config import get_settings
        s = get_settings()

        encoded_role = urllib.parse.quote(role)
        url = _SEARCH_URL.format(role=encoded_role)
        logger.info("LinkedIn Burner: searching posts globally via %s", url)

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=s.BROWSER_TIMEOUT_MS)
            await _human_delay(page, 3000, 6000)
            
            # Scroll down naturally a couple of times to trigger lazy-loaded posts
            for _ in range(random.randint(2, 4)):
                await _natural_scroll(page, steps=2)
                await _human_delay(page, 1500, 3000)
        except Exception as exc:
            logger.error("LinkedIn Burner: search navigation failed: %s", exc)
            return []

        jobs: list[dict] = []
        
        # Primary container for posts in LinkedIn search results: .reusable-search__result-container
        cards = await page.query_selector_all(
            "li.reusable-search__result-container, "
            "[class*='search-results__list-item'], "
            "[class*='search-result__wrapper'], "
            "div.search-content__result"
        )
        
        if not cards:
            # Fallback to general update cards if search layout is embedded differently
            cards = await page.query_selector_all(
                "[data-urn*='urn:li:activity:'], "
                "[data-urn*='urn:li:share:'], "
                "div[class*='feed-shared-update-v2']"
            )

        logger.info("LinkedIn Burner: found %d raw post cards", len(cards))

        for card in cards:
            if len(jobs) >= limit:
                break
            try:
                # 1. Extract URN and Post URL
                urn = await card.getAttribute("data-urn") or await card.getAttribute("data-id")
                post_url = None
                if urn and ("urn:li:" in urn or urn.isdigit()):
                    post_urn = urn if "urn:li:" in urn else f"urn:li:activity:{urn}"
                    post_url = f"https://www.linkedin.com/feed/update/{post_urn}/"
                
                # Link selector fallback
                if not post_url:
                    link_el = await card.query_selector(
                        "a[href*='/feed/update/'], a[href*='/posts/']"
                    )
                    if link_el:
                        post_url = await link_el.get_attribute("href")
                        if post_url and not post_url.startswith("http"):
                            post_url = f"https://www.linkedin.com{post_url}"

                if not post_url:
                    continue

                # Deduplicate
                if any(j["job_url"] == post_url for j in jobs):
                    continue

                # 2. Extract Author/Company Name
                author_el = await card.query_selector(
                    "[class*='actor__name'], "
                    "[class*='feed-shared-actor__name'], "
                    "[class*='title'] a, "
                    "a[href*='/in/']"
                )
                author_name = "LinkedIn Poster"
                if author_el:
                    author_name = (await author_el.inner_text()).strip()
                    # Clean up "View profile" or extra sub-spans
                    author_name = author_name.split("\n")[0]

                # 3. Extract Post Content/Text
                text_el = await card.query_selector(
                    "[class*='feed-shared-update-v2__description'], "
                    "[class*='update-v2__description'], "
                    "span.break-words, "
                    "[class*='search-content__result-description'], "
                    ".feed-shared-text"
                )
                
                post_text = ""
                if text_el:
                    post_text = (await text_el.inner_text()).strip()

                if not post_text or len(post_text) < 15:
                    continue

                jobs.append({
                    "title": f"Hiring Post by {author_name}",
                    "company": author_name,
                    "location": "Remote",
                    "job_url": post_url,
                    "source": "linkedin_post",
                    "id": None,
                    "description": post_text,
                    "salary_min": None,
                    "salary_max": None,
                    "currency": None,
                    "date_posted": None,
                    "raw_data": post_text,
                })
            except Exception as e:
                logger.debug("Skipping parsing single LinkedIn search post: %s", e)

        logger.info("LinkedIn Burner: successfully extracted %d job posts", len(jobs))
        return jobs

    async def apply_to_job(self, page, job: dict) -> bool:
        """Applying to social posts is inherently manual or outreach-based (DM/email).

        Returns False to route the handoff to the custom Social Outreach drafter pipeline.
        """
        logger.info("LinkedIn Burner: application is social/outreach-based — saving post for outreach.")
        return False


# Export instantiated driver for automatic dynamic bootstrapping by JobBoardRegistry
DRIVER = LinkedInBurnerDriver()
