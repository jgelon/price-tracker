"""
Scraper for hollandandbarrett.nl (and .com)

H&B NL is a Next.js / Bloomreach app.

Strategy order (updated):
  1. JSON-LD  — H&B now injects this on every PDP and it reflects the live
                selling price (including promotions). Most reliable.
  2. window.universal_variable inline script  — analytics blob with unit_price.
  3. __NEXT_DATA__  — fallback; the huge Next.js blob can contain unrelated
                      price keys (related products, productIDs, etc.) so we
                      only reach here when the above two are absent.
  4. CSS selectors  — last resort.

Known __NEXT_DATA__ shapes (for reference, still used in strategy 3):
  props.pageProps.productDetails.prices.nowPrice.price    <- Bloomreach CMS
  props.pageProps.productDetails.price
  props.pageProps.product.price / salePrice / currentPrice
  props.pageProps.productData.price
  props.pageProps.initialState.*.price                    <- Redux store
"""

import json
import logging
import re

from .base import BaseScraper, ScraperResult

logger = logging.getLogger(__name__)

# Keys whose value IS the current selling price (ordered specific → generic)
_PRICE_KEYS = ("nowPrice", "salePrice", "currentPrice", "promotionPrice", "lowestPrice", "price")

# Sub-tree keys that indicate "was / original" price — skip to avoid grabbing them
_SKIP_KEYS = {"wasPrice", "originalPrice", "rrpPrice", "listPrice", "strikePrice"}


class HollandBarrettScraper(BaseScraper):
    name = "holland_barrett"

    def can_handle(self, url: str) -> bool:
        return "hollandandbarrett" in url

    def scrape(self, url: str) -> ScraperResult:
        logger.info("[holland_barrett] Scraping %s", url)
        soup = self._fetch(url)
        if soup is None:
            return ScraperResult(None, None, error="Failed to fetch page")

        # Strategy 1: JSON-LD — H&B NL now injects this on every PDP and it
        # always reflects the current selling price (incl. discounts).
        price, name = self._extract_json_ld_price(soup)
        if price is not None:
            logger.info("[holland_barrett] JSON-LD -> price=%.2f name=%r", price, name)
            return ScraperResult(price, name)

        # Strategy 2: window.universal_variable inline script
        # H&B injects an analytics blob with {"product": {"unit_price": "3.59", ...}}
        for script in soup.find_all("script"):
            txt = script.string or ""
            if "universal_variable" not in txt or "unit_price" not in txt:
                continue
            try:
                match = re.search(
                    r'window\.universal_variable\s*=\s*(\{.*?\})\s*(?:</script>|;?\s*$)',
                    txt, re.DOTALL
                )
                if match:
                    uv = json.loads(match.group(1))
                    unit_price = uv.get("product", {}).get("unit_price")
                    pname = uv.get("product", {}).get("name") or uv.get("page", {}).get("name")
                    if unit_price is not None:
                        p = self._coerce(unit_price)
                        if p:
                            logger.info("[holland_barrett] universal_variable -> price=%.2f name=%r", p, pname)
                            return ScraperResult(p, pname)
            except Exception as exc:
                logger.debug("[holland_barrett] universal_variable parse error: %s", exc)

        # Strategy 3: __NEXT_DATA__ — fallback for pages without JSON-LD.
        # Note: the recursive scan can latch onto unrelated price keys (related
        # products, productIDs, etc.) inside the huge Next.js blob, so this is
        # kept as a fallback rather than the primary strategy.
        next_data = self._extract_next_data(soup)
        if next_data:
            logger.debug("[holland_barrett] __NEXT_DATA__ found, parsing...")
            result = self._parse_next_data(next_data)
            if result.success:
                logger.info("[holland_barrett] __NEXT_DATA__ -> price=%.2f name=%r", result.price, result.name)
                return result
            logger.warning("[holland_barrett] __NEXT_DATA__ present but no price found; falling back")
        else:
            logger.warning("[holland_barrett] No __NEXT_DATA__ in page — site may have changed")

        # Strategy 4: CSS selectors
        name_tag = soup.select_one("h1.productName, h1[data-test='product-name'], h1.product__name, h1")
        name = name_tag.get_text(strip=True) if name_tag else None
        for sel in ("[data-test='product-price']", "span[class*='price']", "p[class*='price']",
                    ".product-price__sale", ".price--sale", ".priceText"):
            tag = soup.select_one(sel)
            if tag:
                price = self._parse_price(tag.get_text(strip=True))
                if price is not None:
                    logger.info("[holland_barrett] CSS -> price=%.2f name=%r", price, name)
                    return ScraperResult(price, name)

        logger.warning("[holland_barrett] All strategies failed for %s", url)
        return ScraperResult(None, name, error="Price element not found")

    # ------------------------------------------------------------------ #

    def _parse_next_data(self, data: dict) -> ScraperResult:
        pp = self._deep_find(data, "props", "pageProps") or {}

        # Named paths — tried in order, newest schema first
        attempts = [
            # Bloomreach / current schema
            lambda: self._from_price_obj(
                self._deep_find(pp, "productDetails", "prices", "nowPrice"),
                self._deep_find(pp, "productDetails", "name")),
            lambda: self._from_price_obj(
                self._deep_find(pp, "productDetails", "prices", "salePrice"),
                self._deep_find(pp, "productDetails", "name")),
            lambda: self._scalar(
                self._deep_find(pp, "productDetails", "price"),
                self._deep_find(pp, "productDetails", "name")),
            # Standard product nodes
            lambda: self._from_product_node(pp.get("product")),
            lambda: self._from_product_node(pp.get("productData")),
            lambda: self._from_product_node(self._deep_find(pp, "pdpData", "product")),
            lambda: self._from_product_node(self._deep_find(pp, "data", "product")),
            lambda: self._from_product_node(self._deep_find(pp, "initialData", "product")),
            lambda: self._from_product_node(self._deep_find(pp, "serverSideProps", "productData")),
        ]

        for attempt in attempts:
            try:
                result = attempt()
                if result and result.success:
                    return result
            except Exception as exc:
                logger.debug("[holland_barrett] named path error: %s", exc)

        # Full recursive scan — catches any future schema change
        logger.debug("[holland_barrett] Starting full recursive tree scan")
        price, name = self._recursive_scan(data, depth=0, name=None)
        if price is not None:
            return ScraperResult(price, name)

        return ScraperResult(None, None, error="No price in __NEXT_DATA__")

    def _from_price_obj(self, node, name=None):
        """Extract from a {price/amount/value: X} dict."""
        if not isinstance(node, dict):
            return None
        for key in ("price", "amount", "value", "current"):
            raw = node.get(key)
            if raw is not None:
                price = self._coerce(raw)
                if price:
                    return ScraperResult(price, name)
        return None

    def _scalar(self, raw, name=None):
        if raw is None:
            return None
        price = self._coerce(raw)
        return ScraperResult(price, name) if price else None

    def _from_product_node(self, node):
        if not isinstance(node, dict):
            return None
        name = node.get("name") or node.get("title") or node.get("productName")

        # Direct price fields
        for key in _PRICE_KEYS:
            raw = node.get(key)
            if raw is None:
                continue
            if isinstance(raw, dict):
                r = self._from_price_obj(raw, name)
                if r and r.success:
                    return r
            else:
                price = self._coerce(raw)
                if price:
                    return ScraperResult(price, name)

        # Price container sub-objects
        for ck in ("prices", "pricing", "priceInfo", "priceData"):
            container = node.get(ck)
            if not isinstance(container, dict):
                continue
            for pk in _PRICE_KEYS:
                if pk in _SKIP_KEYS:
                    continue
                sub = container.get(pk)
                if sub is None:
                    continue
                price = (self._coerce(sub) if not isinstance(sub, dict)
                         else self._coerce(sub.get("price") or sub.get("amount") or sub.get("value")))
                if price:
                    return ScraperResult(price, name)
        return None

    def _recursive_scan(self, obj, depth: int, name=None):
        """Walk full JSON tree. Skips _SKIP_KEYS sub-trees. Max depth 12."""
        if depth > 12 or obj is None:
            return None, None

        if isinstance(obj, dict):
            candidate_name = obj.get("name") or obj.get("title") or obj.get("productName") or name

            # Prioritise nowPrice / salePrice before generic 'price'
            for pk in ("nowPrice", "salePrice", "currentPrice"):
                sub = obj.get(pk)
                if sub is None:
                    continue
                if isinstance(sub, dict):
                    r = self._from_price_obj(sub, candidate_name)
                    if r and r.success:
                        return r.price, r.name or candidate_name
                else:
                    price = self._coerce(sub)
                    if price and 0.01 < price < 10_000:
                        return price, candidate_name

            # Generic 'price' key (not a dict/list)
            raw = obj.get("price")
            if raw is not None and not isinstance(raw, (dict, list)):
                price = self._coerce(raw)
                if price and 0.01 < price < 10_000:
                    return price, candidate_name

            # Recurse, skipping "was price" sub-trees
            for key, value in obj.items():
                if key in _SKIP_KEYS:
                    continue
                p, n = self._recursive_scan(value, depth + 1, candidate_name)
                if p is not None:
                    return p, n or candidate_name

        elif isinstance(obj, list):
            for item in obj[:30]:
                p, n = self._recursive_scan(item, depth + 1, name)
                if p is not None:
                    return p, n

        return None, None

    @staticmethod
    def _coerce(raw):
        if isinstance(raw, bool):
            return None
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