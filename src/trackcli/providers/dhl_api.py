"""DHL official shipment-tracking API (tier 0, free key).

developer.dhl.com gives a free API key in about a minute; the endpoint
is a clean JSON REST API with no bot wall (verified live: keyless calls
answer 401 JSON rather than an Akamai challenge).

    GET https://api-eu.dhl.com/track/shipments?trackingNumber=<num>
    Header: DHL-API-Key: <key>

Response shape: ``shipments[0].{status:{status, details}, estimatedTimeOfDelivery,
events:[{timestamp, location:{address}, description}]}``.

FedEx is deliberately absent at tier 0: its free developer keys exist,
but the OAuth app-approval flow is heavier and the aggregator chain
covers FedEx well.  See docs/PROVIDERS.md for the notes if you want to
add ``FedexApiProvider`` -- it slots in exactly like this one.
"""

from __future__ import annotations

import urllib.parse
from datetime import datetime, timezone

from ..models import ErrorKind, ProviderError, Shipment
from .base import Provider

_HOST = "api-eu.dhl.com"


class DhlApiProvider(Provider):
    name = "dhl-api"
    tier = 0

    def supports(self, number: str, carrier: str) -> bool:
        return carrier == "dhl" and self.config.has_dhl

    def fetch(self, number: str, carrier: str) -> Shipment:
        path = ("/track/shipments?trackingNumber="
                + urllib.parse.quote(number))
        status, raw = self._http(
            "GET", _HOST, path,
            headers={"DHL-API-Key": self.config.dhl_api_key,
                     "Accept": "application/json"},
            timeout=self.config.timeout)
        payload = self._parse_json(raw, "dhl tracking")
        if status == 404:
            raise self._not_found(self.name, "unknown to DHL")
        if status in (401, 403):
            raise ProviderError(self.name, ErrorKind.CONFIG,
                                f"DHL rejected the API key (HTTP {status})")
        if status != 200:
            raise ProviderError(self.name, ErrorKind.NETWORK,
                                f"DHL HTTP {status}")

        shipments = payload.get("shipments") or []
        if not shipments:
            raise self._not_found(self.name, "DHL returned no shipments")
        info = shipments[0]

        events = []
        for event in reversed(info.get("events") or []):
            address = ((event.get("location") or {}).get("address") or {})
            place = ", ".join(filter(None, [
                address.get("addressLocality"), address.get("countryCode")]))
            events.append(self._event(event.get("timestamp"), place,
                                      event.get("description", "")))

        status_info = info.get("status") or {}
        eta = (info.get("estimatedTimeOfDelivery")
               or info.get("estimatedDeliveryDate"))
        return Shipment(
            tracking_number=number,
            carrier="dhl",
            status_human=status_info.get("status", ""),
            estimated_delivery=eta,
            events=events,
            provider=self.name,
            provider_tier=self.tier,
            fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
