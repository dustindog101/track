"""USPS official APIs (tier 0 -- free, but require a registered key).

Why bother with keys when AfterShip works keyless?  Because USPS's own
API host is the one USPS surface *not* behind Akamai (verified live: the
XML endpoint answers from datacenter IPs with clean XML even when the
tracking web pages 403).  Keys are free:

* Legacy Web Tools (simplest): register at usps.com/business/web-tools-apis
  and receive a ``USERID`` by email.
    GET https://secure.shippingapis.com/ShippingAPI.dll?API=TrackV2&XML=...
* New USPS API platform (2024+): create an app at developers.usps.com,
  get ``client_id``/``client_secret`` (OAuth2 client_credentials).
    POST https://api.usps.com/oauth2/v3/token
    GET  https://api.usps.com/tracking/v3/tracking/<number>

Both are implemented; legacy is used when ``user_id`` is configured,
OAuth2 when ``client_id``+``client_secret`` are.  Only activates for
USPS-shaped numbers (see carriers.py).
"""

from __future__ import annotations

import base64
import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from ..models import ErrorKind, ProviderError, Shipment
from .base import Provider

_LEGACY_HOST = "secure.shippingapis.com"
_OAUTH_HOST = "api.usps.com"


def _xml_escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;"))


class UspsApiProvider(Provider):
    name = "usps-api"
    tier = 0

    def supports(self, number: str, carrier: str) -> bool:
        return carrier == "usps" and (
            self.config.has_usps_legacy or self.config.has_usps_oauth)

    # --------------------------------------------------------- legacy XML
    def _fetch_legacy(self, number: str) -> Shipment:
        xml = (f'<TrackRequest USERID="{_xml_escape(self.config.usps_user_id)}">'
               f'<TrackID ID="{_xml_escape(number)}"/></TrackRequest>')
        url = (_LEGACY_HOST + "/ShippingAPI.dll?API=TrackV2&XML="
               + urllib.parse.quote(xml))
        status, raw = self._http(
            "GET", _LEGACY_HOST, url.split(_LEGACY_HOST, 1)[1],
            headers=self._browser_headers(Accept="application/xml"),
            timeout=self.config.timeout)
        try:
            root = ET.fromstring(raw.decode("utf-8", "replace"))
        except ET.ParseError as exc:
            raise ProviderError(self.name, ErrorKind.PARSE,
                                f"USPS XML unparsable: {exc}") from exc

        error = root.find(".//Error")
        if error is not None:
            description = (error.findtext("Description") or "").strip()
            if "auth" in description.lower() or "userid" in description.lower():
                raise ProviderError(self.name, ErrorKind.CONFIG,
                                    f"USPS rejected credentials: {description}")
            raise self._not_found(self.name, description or "USPS: no such item")

        info = root.find(".//TrackInfo")
        if info is None:
            raise ProviderError(self.name, ErrorKind.PARSE,
                                "USPS XML missing TrackInfo")

        summary = info.findtext("TrackSummary") or ""
        events = [
            self._event(None, (d.get("EventCity", "") + ", " +
                               d.get("EventState", "")).strip(", "),
                        d.text or "")
            for d in info.findall("TrackDetail")
        ]
        # USPS lists newest first, like most carriers.
        events.reverse()

        expected = info.get("ExpectedDeliveryDate") or None
        return Shipment(
            tracking_number=number,
            carrier="usps",
            status_human=summary,
            estimated_delivery=expected,
            events=events,
            provider=self.name,
            provider_tier=self.tier,
            fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

    # ------------------------------------------------------- OAuth2 REST
    def _oauth_token(self) -> str:
        basic = base64.b64encode(
            f"{self.config.usps_client_id}:{self.config.usps_client_secret}"
            .encode()).decode()
        status, raw = self._http(
            "POST", _OAUTH_HOST, "/oauth2/v3/token",
            headers={"Authorization": f"Basic {basic}",
                     "Content-Type": "application/x-www-form-urlencoded"},
            body=b"grant_type=client_credentials",
            timeout=self.config.timeout)
        payload = self._parse_json(raw, "usps oauth token")
        if status != 200 or "access_token" not in payload:
            raise ProviderError(self.name, ErrorKind.CONFIG,
                                f"USPS OAuth failed: HTTP {status}")
        return str(payload["access_token"])

    def _fetch_rest(self, number: str) -> Shipment:
        token = self._oauth_token()
        path = f"/tracking/v3/tracking/{urllib.parse.quote(number)}"
        status, raw = self._http(
            "GET", _OAUTH_HOST, path,
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/json"},
            timeout=self.config.timeout)
        payload = self._parse_json(raw, "usps tracking v3")
        if status == 404:
            raise self._not_found(self.name, "unknown to USPS")
        if status != 200:
            raise ProviderError(self.name, ErrorKind.NETWORK,
                                f"USPS v3 HTTP {status}")

        events = [
            self._event(e.get("eventTimestamp"), e.get("eventCity", ""),
                        e.get("eventDescription", ""))
            for e in reversed(payload.get("eventHistory") or [])
        ]
        return Shipment(
            tracking_number=number,
            carrier="usps",
            status_human=payload.get("status", ""),
            estimated_delivery=payload.get("expectedDeliveryDate"),
            events=events,
            provider=self.name,
            provider_tier=self.tier,
            fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

    # -------------------------------------------------------------- fetch
    def fetch(self, number: str, carrier: str) -> Shipment:
        if self.config.has_usps_legacy:
            return self._fetch_legacy(number)
        return self._fetch_rest(number)
