"""Provider base class + tiny stdlib HTTP helpers shared by all sources.

A *provider* is one data source.  To add one, subclass `Provider`,
implement :meth:`fetch`, and register it in ``providers/__init__.py``.
The engine handles ordering, caching, cooldowns and error routing --
a provider only needs to answer one question: *given a number, what do
you know?*  It communicates failure exclusively by raising
:class:`~trackcli.models.ProviderError` so the engine can route by kind.
"""

from __future__ import annotations

import gzip
import http.client
import json
import ssl
import urllib.parse
import zlib
from abc import ABC, abstractmethod
from typing import Any, Optional

from ..config import Config
from ..models import ErrorKind, Event, ProviderError, Shipment

# A plausible browser identity.  Sources behind CDNs (Akamai etc.) grade
# requests by header plausibility; python-urllib's default UA is an
# instant 403, so every provider sends this unless a source needs
# something specific.
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

_TLS = ssl.create_default_context()


class Provider(ABC):
    """Contract every data source implements."""

    #: unique slug used in output + cooldown keys
    name: str = "abstract"
    #: 0 = official keyed API, 1-2 = keyless, 3 = headless browser
    tier: int = 1

    def __init__(self, config: Config) -> None:
        self.config = config

    # --------------------------------------------------------- interface
    def supports(self, number: str, carrier: str) -> bool:
        """Eligibility gate.  Default: every number.  Override to restrict
        (e.g. the USPS XML API only speaks USPS)."""
        return True

    @abstractmethod
    def fetch(self, number: str, carrier: str) -> Shipment:
        """Return a Shipment or raise ProviderError.  Never return None."""

    # ------------------------------------------------------- HTTP helpers
    @staticmethod
    def _browser_headers(**extra: str) -> dict[str, str]:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
        }
        headers.update(extra)
        return headers

    @staticmethod
    def _decode(resp: http.client.HTTPResponse) -> bytes:
        """Read a response body, transparently unwrapping compression."""
        raw = resp.read()
        encoding = (resp.getheader("Content-Encoding") or "").lower()
        if "gzip" in encoding:
            raw = gzip.decompress(raw)
        elif "deflate" in encoding:
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)
        return raw

    @staticmethod
    def _http(method: str, host: str, path: str,
              headers: dict[str, str],
              body: Optional[bytes] = None,
              timeout: int = 20) -> tuple[int, bytes]:
        """One keep-alive-capable HTTPS request.  Returns (status, body).

        Uses http.client (not urllib) so flows that must keep the same
        TCP/TLS connection across calls (AfterShip's IP-bound signatures)
        can pass the same connection object around.
        """
        conn = http.client.HTTPSConnection(host, timeout=timeout, context=_TLS)
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            return resp.status, Provider._decode(resp)
        finally:
            conn.close()

    @staticmethod
    def _parse_json(raw: bytes, context: str) -> Any:
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except ValueError as exc:
            raise ProviderError(
                provider="?", kind=ErrorKind.PARSE,
                message=f"{context}: unparsable JSON ({exc})") from exc

    @staticmethod
    def _event(timestamp: Optional[str], location: str,
               description: str, tz_naive: bool = False) -> Event:
        return Event(timestamp=timestamp, location=location.strip(),
                     description=description.strip(), tz_naive=tz_naive)

    @staticmethod
    def _not_found(provider: str, message: str) -> ProviderError:
        # Soft failure: the chain should still try other sources.
        return ProviderError(provider, ErrorKind.NOT_FOUND, message)

