"""
Scraper for hollandandbarrett.nl (and .com)

Strategy:
  1. JSON-LD structured data (preferred)
  2. H&B-specific CSS selectors (fallback)
  3. Open Graph price meta tag (last resort)
"""

import logging

from .base import BaseScraper, ScraperResult

logger = logging.getLogger(__name__)


class HollandBarrettScraper(BaseScraper):
    name = "holland_barrett"

    def can_handle(self, url: str) -> bool:
        return "hollandandbarrett" in url

    def scrape(self, url: str) -> ScraperResult:
        logger.info("[holland_barrett] Scraping %s", url)
        soup = self._fetch(url)
        if soup is None:
            return ScraperResult(None, None, error="Failed to fetch page")

        # --- Strategy 1: JSON-LD ---
        price, name = self._extract_json_ld_price(soup)
        if price is not None:
            logger.info("[holland_barrett] JSON-LD → price=%.2f name=%r", price, name)
            return ScraperResult(price, name)

        # --- Strategy 2: H&B-specific CSS ---
        # Product name
        name_tag = soup.select_one(
            "h1.productName, h1[data-test='product-name'], h1.product__name"
        )
        name = name_tag.get_text(strip=True) if name_tag else None

        # Price selectors observed on H&B NL pages
        price_tag = soup.select_one(
            "span.price, "
            "p.price, "
            "[data-test='product-price'], "
            ".product-price__sale, "
            ".price--sale, "
            ".priceText"
        )
        if price_tag:
            price = self._parse_price(price_tag.get_text(strip=True))
            if price is not None:
                logger.info("[holland_barrett] CSS → price=%.2f name=%r", price, name)
                return ScraperResult(price, name)

        # --- Strategy 3: Open Graph ---
        price = self._extract_og_price(soup)
        if price is not None:
            logger.info("[holland_barrett] OG meta → price=%.2f", price)
            return ScraperResult(price, name)

        logger.warning("[holland_barrett] Could not extract price from %s", url)
        return ScraperResult(None, name, error="Price element not found")
