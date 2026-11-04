"""Headless-browser last resort (tier 3 -- only after every API failed).

Used when all keyless API sources are exhausted (rate-limited, blocked)
or -- most commonly -- when the client IP is heavily datacenter-flagged
and even PackageRadar is refusing.  A real Chromium passes the checks
that plain HTTP fails, *if* the IP is not outright banned; from
residential connections this works essentially always.

Design notes:

* playwright is a *lazy, optional* import -- the CLI stays fast and
  dependency-free for the 99% path, and only touches playwright when
  this provider actually runs.  Install with::

      pip install playwright && playwright install chromium

* we block images/fonts/stylesheets to keep the page light;
* extraction is marker-based and tolerant: USPS's tracking page embeds
  the status and "Expected Delivery" text plus an events table; we try
  DOM selectors first and fall back to plain-text regexes;
* per-carrier target URLs -- USPS official page for USPS numbers,
  carrier pages for UPS/FedEx, 17TRACK's web UI for everything else
  (a real browser passes 17TRACK's fingerprint sign that blocked us
  at the API layer).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from ..models import ErrorKind, ProviderError, Shipment, Status
from ..status import normalize
from .base import Provider

_URLS = {
    "usps": ("https://tools.usps.com/go/TrackConfirmAction?tLabels={num}",),
    "ups": ("https://www.ups.com/track?tracknum={num}",),
    "fedex": ("https://www.fedex.com/fedextrack/?trknbr={num}",),
    "dhl": ("https://www.dhl.com/en/shipment-tracking.html?AWB={num}",),
}
_FALLBACK_URL = "https://t.17track.net/en#nums={num}"

_ABORT = ("image", "font", "stylesheet", "media")

_ETA_PATTERNS = [
    re.compile(r"expected delivery[:\s]+([A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4}[^,\n<]*)",
               re.I),
    re.compile(r"arriving\s+([a-z]{3,9}\s+\d{1,2})", re.I),
]


class BrowserProvider(Provider):
    name = "browser"
    tier = 3

    def fetch(self, number: str, carrier: str) -> Shipment:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise ProviderError(
                self.name, ErrorKind.CONFIG,
                "playwright is not installed -- "
                "`pip install playwright && playwright install chromium` "
                "(or run with --no-browser)",
            ) from exc

        template = (_URLS.get(carrier) or _URLS.get("usps"))[0]
        url = template.format(num=number)
        html, text = self._render(sync_playwright, url)
        return self._extract(number, carrier, url, text, html)

    # ------------------------------------------------------------ browser
    def _render(self, sync_playwright, url: str) -> tuple[str, str]:
        """Load the page, return (html, visible_text)."""
        timeout_ms = max(self.config.timeout, 20) * 1000
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                try:
                    page = browser.new_page(
                        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; "
                                    "x64) AppleWebKit/537.36 (KHTML, like "
                                    "Gecko) Chrome/124.0.0.0 Safari/537.36"))
                    # Keep the page lean: kill heavy asset types.
                    page.route(
                        "**/*", lambda route: route.abort()
                        if route.request.resource_type in _ABORT
                        else route.continue_())
                    page.goto(url, wait_until="domcontentloaded",
                              timeout=timeout_ms)
                    page.wait_for_timeout(3500)   # let XHRs fill in
                    html = page.content()
                    text = page.inner_text("body")
                    return html, text
                finally:
                    browser.close()
        except Exception as exc:  # noqa: BLE001  -- any playwright failure
            raise ProviderError(
                self.name, ErrorKind.NETWORK,
                f"browser failed on {url}: {type(exc).__name__}: {exc}",
            ) from exc

    # ----------------------------------------------------------- extract
    def _extract(self, number: str, carrier: str, url: str,
                 text: str, html: str) -> Shipment:
        blob = f"{text}\n{html}"

        # Bot-wall detection: Akamai serves a short "Access Denied" page
        # with neither the number nor any tracking vocabulary.  Fail
        # loudly -- a silent empty "success" is poison for agents.
        if "access denied" in text.lower():
            raise ProviderError(
                self.name, ErrorKind.BLOCKED,
                "carrier page served an Akamai 'Access Denied' page "
                "(datacenter-grade IP blocking); a residential IP or a "
                "stealth browser setup is required")
        if number not in blob:
            markers = ("in transit", "delivered", "expected delivery",
                       "track", "shipment")
            if not any(m in text.lower() for m in markers):
                raise ProviderError(
                    self.name, ErrorKind.PARSE,
                    "page loaded but contained no tracking data "
                    "(likely a JS challenge); see docs/PROVIDERS.md")

        status_human = ""
        status = normalize(text)
        eta: str | None = None
        for pattern in _ETA_PATTERNS:
            match = pattern.search(text)
            if match:
                eta = match.group(1).strip()
                break

        # Try to lift the exact headline status phrase out of the text.
        headline = re.search(
            r"(Delivered|In-?Transit[^|\n<]*|Out for Delivery|"
            r"Shipping Label Created[^\n<]*|UPS[^\n<]{0,40}Received)",
            text, re.I)
        if headline:
            status_human = headline.group(1).strip()
        if status is Status.UNKNOWN:
            status = normalize(status_human or text)

        events = self._events_from_tables(html)

        return Shipment(
            tracking_number=number,
            carrier=carrier or "unknown",
            status_human=status_human or "scraped from carrier page",
            estimated_delivery=eta,
            events=events,
            provider=self.name,
            provider_tier=self.tier,
            fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            notes=[f"scraped via headless browser: {url}"],
        )

    @staticmethod
    def _events_from_tables(html: str):
        """USPS renders event rows in simple table/list markup; pull the
        obvious date+text rows without over-committing to exact classes."""
        from ..models import Event

        events: list[Event] = []
        row_pattern = re.compile(
            r"<t[rh][^>]*>\s*<td[^>]*>([^<]{2,40})</td>\s*"
            r"<td[^>]*>([^<]{2,60})</td>\s*<td[^>]*>(.*?)</td>",
            re.S)
        for date, time_, description in row_pattern.findall(html)[:40]:
            clean = re.compile(r"<[^>]+>")
            events.append(Event(
                timestamp=f"{date.strip()} {time_.strip()}".strip(),
                location="",
                description=re.sub(r"\s+", " ",
                                   clean.sub(" ", description)).strip(),
                tz_naive=True))
        # Carrier tables list newest-first; the Shipment contract is
        # oldest -> newest.
        events.reverse()
        return events
