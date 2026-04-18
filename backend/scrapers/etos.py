"""
Scraper for etos.nl

Strategy:
  1. JSON-LD structured data (preferred)
  2. CSS selectors specific to Etos's HTML layout (fallback)
"""

import logging

from .base import BaseScraper, ScraperResult

logger = logging.getLogger(__name__)


class EtosScraper(BaseScraper):
    name = "etos"

    def can_handle(self, url: str) -> bool:
        return "etos.nl" in url

    def scrape(self, url: str) -> ScraperResult:
        logger.info("[etos] Scraping %s", url)
        soup = self._fetch(url)
        if soup is None:
            return ScraperResult(None, None, error="Failed to fetch page")

        # --- Strategy 1: JSON-LD ---
        price, name = self._extract_json_ld_price(soup)
        if price is not None:
            logger.info("[etos] JSON-LD → price=%.2f name=%r", price, name)
            return ScraperResult(price, name)

        # --- Strategy 2: Etos-specific CSS selectors ---
        # Product name
        name_tag = soup.select_one("h1.product-title, h1[data-test='product-title']")
        name = name_tag.get_text(strip=True) if name_tag else None

        # Price: Etos renders euros and cents in separate spans
        euros_tag = soup.select_one(
            "span.price__euros, [data-test='price-euros'], .product-price .euros"
        )
        cents_tag = soup.select_one(
            "span.price__cents, [data-test='price-cents'], .product-price .cents"
        )

        if euros_tag:
            raw = euros_tag.get_text(strip=True)
            if cents_tag:
                raw += "." + cents_tag.get_text(strip=True).lstrip(",").lstrip(".")
            price = self._parse_price(raw)
            if price is not None:
                logger.info("[etos] CSS fallback → price=%.2f name=%r", price, name)
                return ScraperResult(price, name)

        # Last resort: any element with a price-like class
        generic = soup.select_one(".price, [class*='price']")
        if generic:
            price = self._parse_price(generic.get_text(strip=True))
            if price is not None:
                logger.info("[etos] generic CSS → price=%.2f", price)
                return ScraperResult(price, name)

        logger.warning("[etos] Could not extract price from %s", url)
        return ScraperResult(None, name, error="Price element not found")
