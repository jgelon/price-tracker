"""
Scraper for hollandandbarrett.nl (and .com)

H&B NL is a Next.js app — product data lives in <script id="__NEXT_DATA__">,
NOT in standard JSON-LD or Open Graph meta tags.

Strategy:
  1. __NEXT_DATA__ JSON blob  (primary — covers all H&B Next.js pages)
  2. JSON-LD                  (fallback — older / variant pages)
  3. H&B-specific CSS         (last resort)
"""

import logging
import re

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

        # --- Strategy 1: __NEXT_DATA__ (Next.js embedded JSON) ---
        next_data = self._extract_next_data(soup)
        if next_data:
            result = self._parse_next_data(next_data)
            if result.success:
                logger.info(
                    "[holland_barrett] __NEXT_DATA__ → price=%.2f name=%r",
                    result.price, result.name,
                )
                return result
            logger.debug("[holland_barrett] __NEXT_DATA__ present but no price extracted")

        # --- Strategy 2: JSON-LD ---
        price, name = self._extract_json_ld_price(soup)
        if price is not None:
            logger.info("[holland_barrett] JSON-LD → price=%.2f name=%r", price, name)
            return ScraperResult(price, name)

        # --- Strategy 3: CSS selectors ---
        name_tag = soup.select_one(
            "h1.productName, h1[data-test='product-name'], h1.product__name, h1"
        )
        name = name_tag.get_text(strip=True) if name_tag else None

        price_tag = soup.select_one(
            "[data-test='product-price'], "
            "span.price, p.price, "
            ".product-price__sale, .price--sale, .priceText"
        )
        if price_tag:
            price = self._parse_price(price_tag.get_text(strip=True))
            if price is not None:
                logger.info("[holland_barrett] CSS → price=%.2f name=%r", price, name)
                return ScraperResult(price, name)

        logger.warning("[holland_barrett] All strategies failed for %s", url)
        return ScraperResult(None, name, error="Price element not found")

    # ------------------------------------------------------------------ #
    #  __NEXT_DATA__ parsing                                               #
    # ------------------------------------------------------------------ #

    def _parse_next_data(self, data: dict) -> ScraperResult:
        """Walk __NEXT_DATA__ looking for product price + name."""
        page_props = self._deep_find(data, "props", "pageProps") or {}

        # Try all known H&B NL page shapes
        candidates = [
            page_props.get("product"),
            page_props.get("productData"),
            self._deep_find(page_props, "initialData", "product"),
            self._deep_find(page_props, "pdpData", "product"),
            self._deep_find(page_props, "data", "product"),
        ]

        for node in candidates:
            if not isinstance(node, dict):
                continue
            result = self._extract_price_from_node(node)
            if result.success:
                return result

        # Broad fallback: recursively scan the entire tree
        price, name = self._recursive_find_price(data, depth=0)
        if price is not None:
            return ScraperResult(price, name)

        return ScraperResult(None, None, error="No price in __NEXT_DATA__")

    def _extract_price_from_node(self, node: dict) -> ScraperResult:
        name = node.get("name") or node.get("title") or node.get("productName")

        for field in ("price", "salePrice", "currentPrice", "lowestPrice", "promotionPrice"):
            raw = node.get(field)
            if raw is None:
                continue
            price = self._coerce_price(raw)
            if price is not None:
                logger.debug("[holland_barrett] node field=%r → price=%.2f", field, price)
                return ScraperResult(price, name)

        # Nested pricing object
        pricing = node.get("pricing") or node.get("priceInfo") or node.get("prices")
        if isinstance(pricing, dict):
            for field in ("price", "salePrice", "value", "current", "finalPrice"):
                raw = pricing.get(field)
                if raw is None:
                    continue
                price = self._coerce_price(raw)
                if price is not None:
                    return ScraperResult(price, name)

        return ScraperResult(None, name)

    def _recursive_find_price(
        self, obj, depth: int, name: str | None = None
    ) -> tuple[float | None, str | None]:
        """Last-resort recursive search. Stops at depth 10."""
        if depth > 10 or not obj:
            return None, None

        if isinstance(obj, dict):
            candidate_name = (
                obj.get("name") or obj.get("title") or obj.get("productName") or name
            )
            for field in ("price", "salePrice", "currentPrice"):
                raw = obj.get(field)
                if raw is None:
                    continue
                price = self._coerce_price(raw)
                if price is not None and 0.01 < price < 10_000:
                    return price, candidate_name
            for value in obj.values():
                price, found_name = self._recursive_find_price(
                    value, depth + 1, candidate_name
                )
                if price is not None:
                    return price, found_name or candidate_name

        elif isinstance(obj, list):
            for item in obj[:20]:
                price, found_name = self._recursive_find_price(item, depth + 1, name)
                if price is not None:
                    return price, found_name

        return None, None

    @staticmethod
    def _coerce_price(raw) -> float | None:
        if isinstance(raw, (int, float)):
            v = float(raw)
            return v if v > 0 else None
        if isinstance(raw, str):
            cleaned = re.sub(r"[^\d.]", "", raw.replace(",", "."))
            try:
                v = float(cleaned)
                return v if v > 0 else None
            except ValueError:
                return None
        return None
