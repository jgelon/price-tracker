"""
Scraper registry.

To add a new shop scraper:
  1. Create scrapers/myshop.py and subclass BaseScraper
  2. Import it here and add an instance to SCRAPERS (before GenericScraper)
"""

import logging

from .base import ScraperResult
from .etos import EtosScraper
from .holland_barrett import HollandBarrettScraper
from .generic import GenericScraper
from .playwright_scraper import PlaywrightScraper

logger = logging.getLogger(__name__)

# Order matters: more specific scrapers before GenericScraper
SCRAPERS = [
    EtosScraper(),
    HollandBarrettScraper(),
    GenericScraper(),   # must remain last
]

_playwright = PlaywrightScraper()

# HTTP status codes / error substrings that warrant a Playwright retry
_BLOCKED_SIGNALS = ("403", "blocked", "cloudflare", "challenge", "captcha", "failed to fetch")


def scrape_url(url: str) -> ScraperResult:
    """
    1. Try the first matching scraper normally.
    2. If that scraper returns a blocked/403 error, retry with Playwright.
    Always returns a ScraperResult — never raises.
    """
    for scraper in SCRAPERS:
        if not scraper.can_handle(url):
            continue

        logger.debug("Dispatching %r to scraper %r", url, scraper.name)
        try:
            result = scraper.scrape(url)
        except Exception as exc:
            logger.exception("Unhandled exception in scraper %r for %r", scraper.name, url)
            result = ScraperResult(None, None, error=f"Scraper crashed: {exc}")

        logger.info(
            "scrape_url: scraper=%r url=%r success=%s price=%s error=%r",
            scraper.name, url, result.success, result.price, result.error,
        )

        # If blocked, escalate to Playwright
        if not result.success and _is_blocked(result.error):
            logger.info("scrape_url: blocked result detected, retrying with Playwright for %r", url)
            try:
                pw_result = _playwright.scrape(url)
            except Exception as exc:
                logger.exception("Playwright scraper crashed for %r", url)
                pw_result = ScraperResult(None, None, error=f"Playwright crashed: {exc}")

            logger.info(
                "scrape_url: playwright url=%r success=%s price=%s error=%r",
                url, pw_result.success, pw_result.price, pw_result.error,
            )
            return pw_result

        return result

    logger.error("No scraper matched URL %r", url)
    return ScraperResult(None, None, error="No suitable scraper found")


def _is_blocked(error: str | None) -> bool:
    if not error:
        return False
    err_lower = error.lower()
    return any(sig in err_lower for sig in _BLOCKED_SIGNALS)


__all__ = ["scrape_url", "ScraperResult", "SCRAPERS"]
