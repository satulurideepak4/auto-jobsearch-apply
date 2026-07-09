from __future__ import annotations

import logging
import urllib.parse
import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


class SocialDorker:
    """Zero-risk OSINT Scraper that uses search engine dorking to discover social media hiring posts."""

    def __init__(self, user_agent: str | None = None) -> None:
        self.user_agent = user_agent or (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
        self.headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "DNT": "1",
        }

    def _search_ddg(self, query: str) -> list[dict]:
        """Perform a search on DuckDuckGo HTML and parse links and snippets."""
        encoded_query = urllib.parse.quote_plus(query)
        url = f"https://html.duckduckgo.com/html/?q={encoded_query}"
        results: list[dict] = []

        try:
            with httpx.Client(headers=self.headers, follow_redirects=True, timeout=15.0) as client:
                response = client.get(url)
                if response.status_code != 200:
                    logger.warning("DuckDuckGo returned status code %d for query %r", response.status_code, query)
                    return []
                html = response.text
        except Exception as exc:
            logger.error("Failed to fetch search results from DuckDuckGo: %s", exc)
            return []

        soup = BeautifulSoup(html, "lxml")
        # DuckDuckGo HTML structure has search results inside elements with class 'result'
        result_divs = soup.find_all("div", class_="result")

        # Broad fallbacks to ensure future resilience if DDG updates class names
        if not result_divs:
            result_divs = soup.find_all("div", class_=["web-result", "links_main"])

        for div in result_divs:
            try:
                # Find link element
                link_el = div.find("a", class_="result__url") or div.find("a", class_="result__snippet")
                if not link_el:
                    # Fallback: find any anchor inside the div
                    link_el = div.find("a")

                if not link_el or not link_el.get("href"):
                    continue

                href = link_el["href"]
                # DDG sometimes wraps outgoing links in redirect URLs: /l/?kh=-1&uddg=https%3A%2F%2F...
                if "/l/?" in href:
                    parsed = urllib.parse.urlparse(href)
                    queries = urllib.parse.parse_qs(parsed.query)
                    if "uddg" in queries:
                        href = queries["uddg"][0]

                # Find title element
                title_el = div.find("a", class_="result__title") or div.find("h2")
                title = title_el.get_text(strip=True) if title_el else "Social Media Post"

                # Find snippet/content element
                snippet_el = div.find("a", class_="result__snippet") or div.find("span", class_="result__snippet")
                snippet = snippet_el.get_text(strip=True) if snippet_el else ""

                if snippet:
                    results.append({
                        "title": title,
                        "url": href,
                        "snippet": snippet
                    })
            except Exception as e:
                logger.debug("Skipping parsing single DDG search result: %s", e)

        logger.info("DuckDuckGo dork query %r returned %d raw results.", query, len(results))
        return results

    def dork_linkedin_posts(self, role: str) -> list[dict]:
        """Scrape DuckDuckGo for recently indexed LinkedIn hiring posts for a role."""
        query = f'site:linkedin.com/posts "hiring" "{role}"'
        raw_results = self._search_ddg(query)
        posts: list[dict] = []

        for r in raw_results:
            url = r["url"]
            # Filter for real posts
            if "/posts/" in url or "/feed/update/" in url:
                posts.append({
                    "title": r["title"],
                    "job_url": url,
                    "description": r["snippet"],
                    "source": "linkedin_post",
                    "location": "Remote",
                    "company": "LinkedIn Poster"
                })
        return posts

    def dork_twitter_posts(self, role: str) -> list[dict]:
        """Scrape DuckDuckGo for recently indexed Twitter/X hiring posts for a role."""
        query = f'site:twitter.com OR site:x.com "hiring" "{role}"'
        raw_results = self._search_ddg(query)
        posts: list[dict] = []

        for r in raw_results:
            url = r["url"]
            # Exclude noise links (like login, signup, help, terms)
            if any(noise in url.lower() for noise in ["/status/", "/statuses/"]) and not any(
                n in url.lower() for n in ["/login", "/signup", "/tos", "/privacy", "/help"]
            ):
                posts.append({
                    "title": r["title"],
                    "job_url": url,
                    "description": r["snippet"],
                    "source": "twitter_post",
                    "location": "Remote",
                    "company": "Twitter Poster"
                })
        return posts
