"""
Base scraper class that all site-specific scrapers inherit from.
Provides shared utilities: session management, JSON-LD parsing, OG meta parsing.
"""

import logging
import re
import json
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
    Abstract base scraper.  Subclasses must implement `scrape(url)`.
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
        Returns None if parsing fails.
        """
        if not raw:
            return None
        # Remove currency symbols and whitespace
        cleaned = re.sub(r"[^\d,.]", "", raw.strip())
        # Normalise European comma-decimal: '12,95' → '12.95'
        # but keep thousands separators correct: '1.234,56' → '1234.56'
        if "," in cleaned and "." in cleaned:
            # Thousands dot + decimal comma  e.g. 1.234,56
            cleaned = cleaned.replace(".", "").replace(",", ".")
        elif "," in cleaned:
            # Only comma → treat as decimal separator  e.g. 12,95
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

            # Handle both single objects and arrays/graphs
            items = []
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                items = data.get("@graph", [data])

            for item in items:
                if not isinstance(item, dict):
                    continue
                item_type = item.get("@type", "")
                if item_type not in ("Product", "IndividualProduct"):
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
