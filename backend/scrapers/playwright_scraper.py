"""
Playwright-based scraper — used as fallback when a site returns 403/blocked.

Launches a real headless Chromium browser, waits for the page to fully render
(including JS challenges like Cloudflare), then extracts price data using the
same strategies as the regular scrapers.

Requires: playwright Python package + chromium browser (installed in Dockerfile).
"""

import json
import logging
import re

from .base import BaseScraper, ScraperResult

logger = logging.getLogger(__name__)


class PlaywrightScraper(BaseScraper):
    """
    Headless-browser fallback scraper.
    can_handle() always returns False — it is invoked explicitly by the
    scraper registry when a primary scraper returns a 403/blocked error.
    """
    name = "playwright"

    def can_handle(self, url: str) -> bool:
        return False  # never auto-selected; called explicitly

    def scrape(self, url: str) -> ScraperResult:
        logger.info("[playwright] Launching headless Chromium for %s", url)
        try:
            from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
        except ImportError:
            logger.error("[playwright] playwright package not installed")
            return ScraperResult(None, None, error="Playwright not installed")

        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                    ],
                )
                context = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    locale="nl-NL",
                    viewport={"width": 1280, "height": 800},
                )
                page = context.new_page()

                # Block images/fonts/media to speed up load
                page.route(
                    "**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf,mp4,mp3}",
                    lambda route: route.abort(),
                )

                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                    # Give JS frameworks a moment to hydrate
                    page.wait_for_timeout(2500)
                except PWTimeout:
                    logger.warning("[playwright] Page load timed out for %s", url)
                    browser.close()
                    return ScraperResult(None, None, error="Page load timeout")

                html = page.content()
                browser.close()

            logger.debug("[playwright] Got %d bytes of HTML", len(html))
            return self._extract_from_html(html, url)

        except Exception as exc:
            logger.exception("[playwright] Unexpected error scraping %s", url)
            return ScraperResult(None, None, error=f"Playwright error: {exc}")

    def _extract_from_html(self, html: str, url: str) -> ScraperResult:
        """Run all extraction strategies on the rendered HTML."""
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")

        # 1. __NEXT_DATA__
        nd_tag = soup.find("script", id="__NEXT_DATA__")
        if nd_tag and nd_tag.string:
            try:
                data = json.loads(nd_tag.string)
                result = self._parse_next_data_generic(data)
                if result.success:
                    logger.info("[playwright] __NEXT_DATA__ -> price=%.2f name=%r", result.price, result.name)
                    return result
            except (json.JSONDecodeError, TypeError) as exc:
                logger.debug("[playwright] __NEXT_DATA__ parse error: %s", exc)

        # 2. JSON-LD
        price, name = self._extract_json_ld_price(soup)
        if price is not None:
            logger.info("[playwright] JSON-LD -> price=%.2f", price)
            return ScraperResult(price, name)

        # 3. OG meta
        price = self._extract_og_price(soup)
        if price is not None:
            logger.info("[playwright] OG meta -> price=%.2f", price)
            name = (soup.find("h1") or soup.new_tag("x")).get_text(strip=True)
            return ScraperResult(price, name or None)

        # 4. itemprop price
        tag = soup.find(attrs={"itemprop": "price"})
        if tag:
            raw = tag.get("content") or tag.get_text(strip=True)
            price = self._parse_price(raw)
            if price:
                h1 = soup.find("h1")
                logger.info("[playwright] itemprop -> price=%.2f", price)
                return ScraperResult(price, h1.get_text(strip=True) if h1 else None)

        # 5. Inline script scan for price pattern near known value
        for script in soup.find_all("script"):
            txt = script.string or ""
            if not txt:
                continue
            # Look for "price": 8.29 or "price":"8.29" patterns
            matches = re.findall(r'"(?:price|nowPrice|salePrice|currentPrice)"\s*:\s*"?([\d.]+)"?', txt)
            for m in matches:
                try:
                    price = float(m)
                    if 0.01 < price < 10_000:
                        h1 = soup.find("h1")
                        logger.info("[playwright] inline script -> price=%.2f", price)
                        return ScraperResult(price, h1.get_text(strip=True) if h1 else None)
                except ValueError:
                    continue

        logger.warning("[playwright] Could not extract price from rendered page %s", url)
        return ScraperResult(None, None, error="Price not found in rendered page")

    def _parse_next_data_generic(self, data: dict) -> ScraperResult:
        """Generic __NEXT_DATA__ scan — works for any Next.js site."""
        price, name = self._recursive_scan(data, depth=0, name=None)
        return ScraperResult(price, name) if price else ScraperResult(None, None)

    def _recursive_scan(self, obj, depth: int, name=None):
        if depth > 12 or obj is None:
            return None, None
        skip = {"wasPrice", "originalPrice", "rrpPrice", "listPrice", "strikePrice"}
        if isinstance(obj, dict):
            candidate_name = obj.get("name") or obj.get("title") or obj.get("productName") or name
            for pk in ("nowPrice", "salePrice", "currentPrice"):
                sub = obj.get(pk)
                if sub is None:
                    continue
                if isinstance(sub, dict):
                    for vk in ("price", "amount", "value"):
                        raw = sub.get(vk)
                        if raw is not None:
                            p = self._coerce(raw)
                            if p:
                                return p, candidate_name
                else:
                    p = self._coerce(sub)
                    if p and 0.01 < p < 10_000:
                        return p, candidate_name
            raw = obj.get("price")
            if raw is not None and not isinstance(raw, (dict, list)):
                p = self._coerce(raw)
                if p and 0.01 < p < 10_000:
                    return p, candidate_name
            for key, value in obj.items():
                if key in skip:
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
