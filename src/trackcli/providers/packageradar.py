"""PackageRadar -- the most tolerant keyless source (no CSRF, no cookies).

PackageRadar (packageradar.com) tracks 120+ carriers server-side.  Its
flow is pleasantly ancient-web: POST the number to ``/form``, follow the
redirect to ``/courier/<detected-carrier>/tracking/<number>`` (free
carrier auto-detection!), then read either the server-rendered landing
page or poll ``/ajax/track/<tid>/check`` until its crawler finishes.

No API keys, no CSRF token, no cookie requirement, no fingerprinting --
the most scrape-friendly of all the sources we probed.  Downsides:

* data arrives as HTML fragments, parsed with stdlib regexes here;
* ``datetime=`` attributes carry naive *carrier-local* clock times
  stamped with the site's own +03:00 offset, which is wrong for foreign
  shipments -- so events are marked ``tz_naive`` and we never pretend
  those offsets are absolute;
* no estimated-delivery field on most carriers.
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from ..models import ErrorKind, Event, ProviderError, Shipment
from .base import USER_AGENT, Provider

_HOST = "https://packageradar.com"
_FORM_PATH = "/form"

_CHECKPOINT_LI = re.compile(r"<li[^>]*>(.*?)</li>", re.S)
_DATETIME_ATTR = re.compile(r'datetime="([^"]+)"')
_STATUS_STRONG = re.compile(
    r'<strong class="checkpoint-status">\s*(.*?)\s*</strong>', re.S)
_LOCATION_EM = re.compile(r'<em class="text-muted">\s*(.*?)\s*</em>', re.S)
_TID = re.compile(r'data-tid="([^"]+)"')
_ICON = re.compile(r'<i class="icon ([a-z_ ]+)"')
_TAG = re.compile(r"<[^>]+>")
_MULTISPACE = re.compile(r"\s+")


def _strip(fragment: str) -> str:
    """HTML fragment -> single-spaced plain text."""
    return _MULTISPACE.sub(" ", _TAG.sub(" ", fragment)).strip()


@staticmethod
def _parse_events(landing: str, extra_html: str = "") -> list:
    """Checkpoint <li> rows live in #fragment-checkpoints; the /check
    fragment may append more.  Parse both, dedupe, oldest first."""
    events = []
    seen: set[tuple] = set()
    for source in (landing, extra_html):
        block_match = re.search(
            r'id="fragment-checkpoints"(.*?)</ul>', source, re.S)
        blocks = [block_match.group(1)] if block_match else []
        # The /check payload wraps its fragment in JSON "data".
        if source is extra_html and not block_match and extra_html:
            blocks = [extra_html]
        for block in blocks:
            for li in _CHECKPOINT_LI.findall(block):
                stamp = _DATETIME_ATTR.search(li)
                strong = _STATUS_STRONG.search(li)
                em = _LOCATION_EM.search(li)
                icon = _ICON.search(li)
                if not (strong or stamp):
                    continue
                description = (strong and _strip(strong.group(1))) or ""
                location = (em and _strip(em.group(1))) or ""
                # datetime attrs are naive carrier-local clocks; drop
                # the site's bogus +03:00 offset, keep the wall time.
                iso = stamp.group(1) if stamp else None
                if iso:
                    iso = re.sub(r"[+-]\d{2}:\d{2}$|Z$", "", iso)
                key = (iso, description, location)
                if key in seen:
                    continue
                seen.add(key)
                events.append(
                    Event(timestamp=iso, location=location,
                          description=description, tz_naive=True))
    # PackageRadar renders newest-first; the Shipment contract is
    # oldest -> newest (same as AfterShip and USPS after reversal).
    events.reverse()
    return events


class PackageRadarProvider(Provider):
    name = "packageradar"
    tier = 2

    def _opener(self) -> urllib.request.OpenerDirector:
        # Cookie jar matters: without it PackageRadar occasionally serves
        # a session-less interstitial with neither data-tid nor
        # checkpoints, which would look like "no record".
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor())

    def _landing_html(self, number: str) -> tuple[str, str]:
        """POST /form and return (landing HTML, final URL)."""
        form = urllib.parse.urlencode(
            {"tracking_form[tracking_number]": number}).encode()
        req = urllib.request.Request(
            _HOST + _FORM_PATH, data=form, headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": _HOST,
                "Referer": _HOST + "/",
            })
        with self._opener().open(req, timeout=self.config.timeout) as resp:
            return resp.read().decode("utf-8", "replace"), resp.geturl()

    def _poll_check(self, tid: str, referer: str,
                    max_polls: int = 4) -> dict:
        """Poll the AJAX check endpoint; PackageRadar crawls carriers
        server-side, so the first polls often answer {"result":"waiting"}."""
        last: dict = {}
        for _ in range(max_polls):
            req = urllib.request.Request(
                f"{_HOST}/ajax/track/{tid}/check", headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json, */*",
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": referer,
                })
            with self._opener().open(req, timeout=self.config.timeout) as resp:
                last = json.loads(resp.read().decode("utf-8", "replace"))
            if last.get("result") != "waiting":
                return last
            time.sleep(3)
        return last


    # -------------------------------------------------------------- fetch
    def fetch(self, number: str, carrier: str) -> Shipment:
        landing, final_url = self._landing_html(number)

        # Hard no: a page with neither a tracking id nor a checkpoints
        # block means PackageRadar has no record of this number at all
        # (verified against bogus numbers) -- UNLESS the page is actually
        # a captcha interstitial, which reads as "no record" but means
        # "slow down".
        tid_match = _TID.search(landing)
        has_checkpoints = "fragment-checkpoints" in landing
        if not tid_match and not has_checkpoints:
            if "captcha" in landing.lower():
                raise ProviderError(
                    self.name, ErrorKind.RATE_LIMITED,
                    "PackageRadar served a captcha (IP temporarily "
                    "throttled)", cooldown_seconds=10 * 60)
            raise self._not_found(
                self.name, "PackageRadar has no record of this number")

        extra_html = ""
        if tid_match:
            polled = self._poll_check(tid_match.group(1), final_url)
            if polled.get("result") == "ok":
                extra_html = str(polled.get("data") or "")

        events = _parse_events(landing, extra_html)

        # Carrier slug straight from the redirect URL -- free detection.
        carrier_slug = carrier
        slug_match = re.search(r"/courier/([a-z0-9-]+)/tracking/", final_url)
        if slug_match and slug_match.group(1) not in ("unknown",):
            carrier_slug = slug_match.group(1)

        # The summary sentence ("The package is in transit for 3 days...")
        # sits in the extra fragment -- useful as status_human.
        summary = ""
        if extra_html:
            text = _strip(extra_html)
            sentence = re.search(r"The package[^.<>]*[^.]*(?:\.|$)", text)
            if sentence:
                summary = sentence.group(0).strip()

        shipment = Shipment(
            tracking_number=number,
            carrier=carrier_slug or "unknown",
            status_human=summary,
            events=events,
            provider=self.name,
            provider_tier=self.tier,
            fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            notes=["timestamps are carrier-local (no reliable timezone)"],
        )
        return shipment
