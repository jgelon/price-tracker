"""
Scraper registry.

Usage:
    from scrapers import scrape_url
    result = scrape_url("https://www.etos.nl/...")
    if result.success:
        print(result.price, result.name)
    else:
        print("Failed:", result.error)

To add a new shop scraper:
  1. Create scrapers/myshop.py and subclass BaseScraper
  2. Import it here and add an instance to SCRAPERS (before GenericScraper)
"""

import logging

from .base import ScraperResult
from .etos import EtosScraper
from .holland_barrett import HollandBarrettScraper
from .generic import GenericScraper

logger = logging.getLogger(__name__)

# Order matters: more specific scrapers must come before GenericScraper
SCRAPERS = [
    EtosScraper(),
    HollandBarrettScraper(),
    GenericScraper(),  # must remain last
]


def scrape_url(url: str) -> ScraperResult:
    """
    Find the first scraper that can handle *url* and run it.
    Always returns a ScraperResult — never raises.
    """
    for scraper in SCRAPERS:
        if scraper.can_handle(url):
            logger.debug("Dispatching %r to scraper %r", url, scraper.name)
            try:
                result = scraper.scrape(url)
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "Unhandled exception in scraper %r for %r: %s", scraper.name, url, exc
                )
                result = ScraperResult(None, None, error=f"Scraper crashed: {exc}")
            logger.info(
                "scrape_url result: scraper=%r url=%r success=%s price=%s error=%r",
                scraper.name, url, result.success, result.price, result.error,
            )
            return result

    logger.error("No scraper matched URL %r", url)
    return ScraperResult(None, None, error="No suitable scraper found")


__all__ = ["scrape_url", "ScraperResult", "SCRAPERS"]
