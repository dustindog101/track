"""AfterShip public-track widget API -- the strongest keyless source.

AfterShip's own tracking widget (track.aftership.com) talks to an
internal JSON API protected by two HMAC signatures whose keys ship
inside its public JavaScript bundle (``track-widgets.aftership.com``
``static/main.js``).  No account, no API key, 2000+ carriers, clean
JSON with carrier auto-detection, timezone-correct ISO timestamps and
an estimated delivery date.

Flow (must run over ONE TLS connection -- the app signature embeds the
server-visible client IP; a rotating NAT between the two calls breaks it):

1. ``GET /api/v2/trackings-ticket-info`` -> ``{"timestamp": ms, "ip": str}``
2. app signature  = HMAC-SHA256(APP_KEY, "<num>-<ip>-<UTC date of ts>-<APP_KEY>")
3. body           = compact JSON batch request for the number
4. gateway signature (Kong hmac-auth) = base64(HMAC-SHA256(GW_KEY, canonical))
   where canonical = "POST <path> track.aftership.com\\n"
                     "am-sign-time: <unix>\\nam-sign-version: v1\\n"
                     "digest: <base64(md5(body))>"
5. ``POST /api/v2/direct-trackings/batch`` with both signatures.

Quirks learned in live testing (2026-11):

* ≈one fresh query per (number, client IP) per window; repeat queries
  answer HTTP 402 ``failed to validate recaptcha`` -> we treat that as a
  15-minute cooldown for the number, never hammer it.
* First-ever query of a number may return 201 with empty checkpoints
  while their crawler warms up -- we surface whatever we got plus a note.
* ``detected_slugs`` is their ranked carrier guess list; ``slug`` the
  winner.  Response codes: 20100 created / 402 recaptcha / 40101 auth.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from ..models import ErrorKind, ProviderError, Shipment
from .base import Provider

# Extracted verbatim from the public widget bundle.  These are widget
# credentials, not user secrets -- but they are also the first thing to
# break if AfterShip rotates them; bump them here if 40101s appear.
_APP_KEY = "AqVH1xxEbLMPWELJiKmxGcfAiRsPgG95"
_GW_KEY = b"8eMrcmkiElSWiwCSrQrSSfAlG11rLHz1"

_HOST = "track.aftership.com"
_TICKET_PATH = "/api/v2/trackings-ticket-info"
_BATCH_PATH = "/api/v2/direct-trackings/batch"

#: how long to avoid a number that just answered HTTP 402
COOLDOWN_402 = 15 * 60


class AfterShipProvider(Provider):
    name = "aftership"
    tier = 1

    # ------------------------------------------------------------ signing
    @staticmethod
    def _gateway_headers(method: str, path: str, body: bytes) -> dict[str, str]:
        digest = base64.b64encode(hashlib.md5(body).digest()).decode()
        now = str(int(time.time()))
        canonical = (f"{method} {path} {_HOST}\n"
                     f"am-sign-time: {now}\nam-sign-version: v1\n"
                     f"digest: {digest}")
        signature = base64.b64encode(
            hmac.new(_GW_KEY, canonical.encode(), hashlib.sha256).digest()
        ).decode()
        return {
            "am-sign-time": now,
            "am-sign-version": "v1",
            "digest": digest,
            "authorization": (
                'hmac username="track.aftership.com", '
                'algorithm="hmac-sha256", headers="digest", '
                f'signature="{signature}"'),
        }

    def _fetch_raw(self, number: str) -> tuple[int, dict[str, Any]]:
        """Run the two-call signed flow on a single connection."""
        timeout = self.config.timeout
        conn = http.client.HTTPSConnection(_HOST, timeout=timeout)
        try:
            common = self._browser_headers(
                Origin="https://www.aftership.com",
                Referer="https://www.aftership.com/",
            )

            # Step 1: ticket (also gateway-signed -- the gateway hmac-auth
            # plugin guards every route, not just the batch one).
            signed = self._gateway_headers("GET", _TICKET_PATH, b"")
            conn.request("GET", _TICKET_PATH, headers={**common, **signed})
            resp = conn.getresponse()
            ticket = self._parse_json(self._decode(resp), "ticket-info")
            if resp.status != 200 or "timestamp" not in ticket:
                raise ProviderError(
                    self.name, ErrorKind.BLOCKED,
                    f"ticket-info HTTP {resp.status}: "
                    f"{ticket.get('meta', {}).get('message', 'unknown')}",
                    cooldown_seconds=60)

            # Step 2: application signature binds number + observed IP + day.
            ts_ms = int(ticket["timestamp"])
            ip = str(ticket.get("ip", ""))
            day = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc
                                         ).strftime("%Y-%m-%d")
            app_sig = hmac.new(
                _APP_KEY.encode(),
                f"{number}-{ip}-{day}-{_APP_KEY}".encode(),
                hashlib.sha256).hexdigest()

            # Step 3: compact JSON -- the MD5 digest must byte-match what
            # their JS would send, so no spaces between separators.
            body = json.dumps(
                {"directTrackings": [{"tracking_number": number,
                                      "additionalFields": {}}],
                 "slugs": [], "translateTo": "en"},
                separators=(",", ":")).encode()

            headers = {
                **common,
                "Content-Type": "application/json",
                "visitor-id": str(uuid.uuid4()),
                "aftership-signature": app_sig,
                **self._gateway_headers("POST", _BATCH_PATH, body),
            }
            conn.request("POST", _BATCH_PATH, body=body, headers=headers)
            resp = conn.getresponse()
            return resp.status, self._parse_json(self._decode(resp), "batch")
        finally:
            conn.close()

    # -------------------------------------------------------------- fetch
    def fetch(self, number: str, carrier: str) -> Shipment:
        status_code, payload = self._fetch_raw(number)

        if status_code == 402 or payload.get("meta", {}).get("code") == 402:
            raise ProviderError(
                self.name, ErrorKind.RATE_LIMITED,
                "AfterShip quota for this number/IP used up "
                "(recaptcha required)",
                cooldown_seconds=COOLDOWN_402)
        if status_code in (401, 403) or payload.get("meta", {}).get("code") == 40101:
            raise ProviderError(
                self.name, ErrorKind.BLOCKED,
                "signature rejected -- widget keys may have rotated",
                cooldown_seconds=60)
        if status_code >= 500:
            raise ProviderError(self.name, ErrorKind.NETWORK,
                                f"AfterShip HTTP {status_code}")

        entries = (payload.get("data") or {}).get("direct_trackings") or []
        if not entries:
            raise ProviderError(self.name, ErrorKind.PARSE,
                                "batch response had no direct_trackings")
        entry = entries[0]

        tracking = entry.get("tracking")
        if not tracking:
            code = entry.get("error_code")
            raise self._not_found(
                self.name,
                f"unknown to AfterShip (error_code={code})")

        events = [
            self._event(cp.get("date_time"),
                        (cp.get("address") or {}).get("raw_location", ""),
                        cp.get("message", ""))
            for cp in (tracking.get("checkpoints") or [])
        ]
        # AfterShip lists checkpoints newest-first; the rest of the CLI
        # speaks oldest-first.
        events.reverse()

        shipment = Shipment(
            tracking_number=number,
            carrier=entry.get("slug") or carrier or "unknown",
            status_human=entry.get("latest_status") or "",
            estimated_delivery=tracking.get("estimated_delivery_date")
            or tracking.get("delivery_date"),
            events=events,
            provider=self.name,
            provider_tier=self.tier,
            fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        detected = entry.get("detected_slugs") or []
        if detected:
            shipment.notes.append("possible carriers: " + ", ".join(detected[:5]))
        if not events and tracking.get("is_instant_track"):
            shipment.notes.append(
                "source is still fetching carrier data; retry shortly")
        return shipment
