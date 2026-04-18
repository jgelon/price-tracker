"""
Generic scraper — fallback for any shop not handled by a dedicated scraper.

Strategy (tried in order):
  1. JSON-LD structured data
  2. Open Graph price meta
  3. Heuristic CSS class matching
"""

import logging

from .base import BaseScraper, ScraperResult

logger = logging.getLogger(__name__)

_PRICE_SELECTORS = [
    "[itemprop='price']",
    "[class*='sales-price']",
    "[class*='sale-price']",
    "[class*='special-price']",
    "[class*='current-price']",
    "[class*='offer-price']",
    "[class*='price--sale']",
    "[class*='price--current']",
    "[class*='product-price']",
    "[class*='ProductPrice']",
    "[id*='price']",
]

_NAME_SELECTORS = [
    "h1[itemprop='name']",
    "h1.product-title",
    "h1.product-name",
    "h1.productName",
    "h1",
]


class GenericScraper(BaseScraper):
    name = "generic"

    def can_handle(self, url: str) -> bool:
        return True  # catch-all

    def scrape(self, url: str) -> ScraperResult:
        logger.info("[generic] Scraping %s", url)
        soup = self._fetch(url)
        if soup is None:
            return ScraperResult(None, None, error="Failed to fetch page")

        name = self._extract_name(soup)

        # Strategy 1: JSON-LD
        price, ld_name = self._extract_json_ld_price(soup)
        if price is not None:
            logger.info("[generic] JSON-LD → price=%.2f name=%r", price, ld_name or name)
            return ScraperResult(price, ld_name or name)

        # Strategy 2: Open Graph
        price = self._extract_og_price(soup)
        if price is not None:
            logger.info("[generic] OG meta → price=%.2f name=%r", price, name)
            return ScraperResult(price, name)

        # Strategy 3: Heuristic CSS
        for selector in _PRICE_SELECTORS:
            tag = soup.select_one(selector)
            if not tag:
                continue
            # Skip elements that contain a strikethrough (= "was" price)
            if tag.find(["del", "s", "strike"]):
                logger.debug("[generic] Skipping %r – contains strikethrough", selector)
                continue
            raw = tag.get("content") or tag.get_text(strip=True)
            price = self._parse_price(raw)
            if price is not None:
                logger.info("[generic] CSS %r → price=%.2f name=%r", selector, price, name)
                return ScraperResult(price, name)

        logger.warning("[generic] Could not extract price from %s", url)
        return ScraperResult(None, name, error="No price found with any strategy")

    def _extract_name(self, soup) -> str | None:
        for selector in _NAME_SELECTORS:
            tag = soup.select_one(selector)
            if tag:
                text = tag.get_text(strip=True)
                if text:
                    return text[:200]
        title_tag = soup.find("title")
        if title_tag:
            return title_tag.get_text(strip=True)[:200]
        return None
