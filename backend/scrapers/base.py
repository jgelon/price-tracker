"""
Base scraper class that all site-specific scrapers inherit from.
Provides shared utilities: HTTP fetch, price parsing, JSON-LD, OG meta, __NEXT_DATA__.
"""

import json
import logging
import re
from abc import ABC, abstractmethod

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7",
}

REQUEST_TIMEOUT = 15  # seconds


class ScraperResult:
    """Holds the outcome of a scrape attempt."""

    def __init__(self, price: float | None, name: str | None, error: str | None = None):
        self.price = price
        self.name = name
        self.error = error

    @property
    def success(self) -> bool:
        return self.price is not None

    def __repr__(self):
        if self.success:
            return f"<ScraperResult price={self.price} name={self.name!r}>"
        return f"<ScraperResult FAILED error={self.error!r}>"


class BaseScraper(ABC):
    """
    Abstract base scraper.  Subclasses must implement `can_handle(url)` and `scrape(url)`.
    Shared helpers are available as protected methods.
    """

    name: str = "base"

    @abstractmethod
    def can_handle(self, url: str) -> bool:
        """Return True if this scraper knows how to handle the given URL."""

    @abstractmethod
    def scrape(self, url: str) -> ScraperResult:
        """Fetch the page and extract price + product name."""

    # ------------------------------------------------------------------ #
    #  Shared helpers                                                       #
    # ------------------------------------------------------------------ #

    def _fetch(self, url: str) -> BeautifulSoup | None:
        """Download a page and return a BeautifulSoup object, or None on error."""
        try:
            logger.debug("[%s] GET %s", self.name, url)
            resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            logger.debug("[%s] HTTP %s for %s", self.name, resp.status_code, url)
            return BeautifulSoup(resp.text, "html.parser")
        except requests.exceptions.Timeout:
            logger.warning("[%s] Timeout fetching %s", self.name, url)
        except requests.exceptions.HTTPError as exc:
            logger.warning("[%s] HTTP error %s for %s", self.name, exc.response.status_code, url)
        except requests.exceptions.RequestException as exc:
            logger.warning("[%s] Request error for %s: %s", self.name, url, exc)
        return None

    @staticmethod
    def _parse_price(raw: str) -> float | None:
        """
        Convert a messy price string like '€ 12,95' or '12.95' to a float.
        Handles both European (1.234,56) and US (1,234.56) formats.
        Returns None if parsing fails.
        """
        if not raw:
            return None
        # Strip currency symbols, whitespace, and other non-numeric characters
        cleaned = re.sub(r"[^\d,.]", "", raw.strip())
        if not cleaned:
            return None
        # Normalise decimal separator
        if "," in cleaned and "." in cleaned:
            # European thousands + decimal: 1.234,56 → 1234.56
            cleaned = cleaned.replace(".", "").replace(",", ".")
        elif "," in cleaned:
            # Comma as decimal: 12,95 → 12.95
            cleaned = cleaned.replace(",", ".")
        try:
            value = float(cleaned)
            logger.debug("Parsed price %.2f from %r", value, raw)
            return value
        except ValueError:
            logger.debug("Could not parse price from %r", raw)
            return None

    @staticmethod
    def _extract_json_ld_price(soup: BeautifulSoup) -> tuple[float | None, str | None]:
        """
        Look for JSON-LD <script type="application/ld+json"> blocks and
        extract price and name from the first Product-type entry found.
        Returns (price, name).
        """
        for tag in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(tag.string or "")
            except (json.JSONDecodeError, TypeError):
                continue

            # Handle single objects, arrays, and @graph
            items: list = []
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                items = data.get("@graph", [data])

            for item in items:
                if not isinstance(item, dict):
                    continue
                if item.get("@type") not in ("Product", "IndividualProduct"):
                    continue

                name = item.get("name")
                offers = item.get("offers", {})
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}

                raw_price = offers.get("price") or offers.get("lowPrice")
                if raw_price is not None:
                    try:
                        price = float(str(raw_price).replace(",", "."))
                        logger.debug("JSON-LD price=%.2f name=%r", price, name)
                        return price, name
                    except (ValueError, TypeError):
                        pass
        return None, None

    @staticmethod
    def _extract_og_price(soup: BeautifulSoup) -> float | None:
        """Extract price from Open Graph product:price:amount meta tag."""
        tag = soup.find("meta", property="product:price:amount")
        if tag and tag.get("content"):
            try:
                return float(tag["content"].replace(",", "."))
            except (ValueError, TypeError):
                pass
        return None

    @staticmethod
    def _extract_next_data(soup: BeautifulSoup) -> dict | None:
        """
        Extract and parse the __NEXT_DATA__ JSON blob injected by Next.js apps.
        Returns the parsed dict, or None if not found / invalid.
        """
        tag = soup.find("script", id="__NEXT_DATA__")
        if not tag or not tag.string:
            return None
        try:
            data = json.loads(tag.string)
            logger.debug("__NEXT_DATA__ parsed (top-level keys=%s)", list(data.keys()))
            return data
        except (json.JSONDecodeError, TypeError) as exc:
            logger.debug("__NEXT_DATA__ parse error: %s", exc)
            return None

    @staticmethod
    def _deep_find(obj, *keys):
        """
        Traverse a nested dict/list following the given keys in order.
        Returns the value if found, else None.
        Example: _deep_find(data, 'props', 'pageProps', 'product', 'price')
        """
        current = obj
        for key in keys:
            if isinstance(current, dict):
                current = current.get(key)
            elif isinstance(current, list) and isinstance(key, int):
                current = current[key] if key < len(current) else None
            else:
                return None
            if current is None:
                return None
        return current
