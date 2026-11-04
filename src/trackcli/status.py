"""Status normalization: turn any provider's wording into a `Status` enum.

Each provider reports status differently:

* AfterShip:  ``latest_status`` like "InTransit" / "Delivered" / "Pending"
* PackageRadar: CSS icon classes + English phrases ("Package accepted")
* USPS XML API: code strings like ``In Transit, Arriving On Time``
* Browser scrape: free-form page text

`normalize()` runs a careful regex ladder -- ordered so the most specific
match wins (e.g. "out for delivery" must beat the generic "in transit"
that also contains the word "delivery").
"""

from __future__ import annotations

import re

from .models import Status

# AfterShip's machine statuses map cleanly -- check this table first.
_AFTERSHIP_MAP: dict[str, Status] = {
    "pending": Status.PRE_TRANSIT,
    "inforeceived": Status.PRE_TRANSIT,
    "intransit": Status.IN_TRANSIT,
    "outfordelivery": Status.OUT_FOR_DELIVERY,
    "delivered": Status.DELIVERED,
    "exception": Status.EXCEPTION,
    "expired": Status.EXPIRED,
    "attemptfail": Status.EXCEPTION,
    "transit": Status.IN_TRANSIT,
}

# Ordered ladder of (regex, status).  First hit wins.
_LADDER: list[tuple[re.Pattern[str], Status]] = [
    (re.compile(r"out for delivery|outfordelivery|on delivery vehicle"),
     Status.OUT_FOR_DELIVERY),
    (re.compile(r"delivered|delivered to|left at\b"), Status.DELIVERED),
    (re.compile(r"return(ed)? to sender|being returned|return to shipper"),
     Status.RETURNED),
    (re.compile(r"expired|unclaimed|no longer"), Status.EXPIRED),
    (re.compile(r"exception|undeliver|failed attempt|held in customs|"
                r"customs hold|delivery attempted|address.*issue|refused"),
     Status.EXCEPTION),
    # General movement.  "accepted at facility" is movement in carrier
    # terms (AfterShip marks it InTransit), so it lives below the
    # exception ladder but above label-created.
    (re.compile(r"in transit|departed|arrived|processed|accepted|"
                r"depart|arrive|sorting|distribution|transfer|dispatch|"
                r"usps in possession|picked up"),
     Status.IN_TRANSIT),
    (re.compile(r"label created|shipping label|info received|electronic "
                r"notification|pre-?transit|pending|awaiting"),
     Status.PRE_TRANSIT),
]


def normalize(text: str | None, machine_status: str | None = None) -> Status:
    """Map provider wording onto a `Status`.

    `machine_status` is a provider's own enum-ish string (AfterShip's
    ``latest_status``); it is checked against the explicit map first,
    then free text goes through the regex ladder.
    """
    if machine_status:
        key = re.sub(r"[^a-z]", "", machine_status.lower())
        if key in _AFTERSHIP_MAP:
            return _AFTERSHIP_MAP[key]
    if not text:
        return Status.UNKNOWN
    # Canonicalize separators so "In-Transit" / "Out_For_Delivery" match
    # the ladder (USPS's official status strings are hyphenated).
    haystack = re.sub(r"[-_]+", " ", text.lower())
    for pattern, status in _LADDER:
        if pattern.search(haystack):
            return status
    return Status.UNKNOWN


def best_status(*candidates: tuple[str | None, str | None]) -> Status:
    """Pick the most informative `Status` from several (text, machine) pairs.

    Providers sometimes expose both a summary and a machine field that
    disagree; an explicit machine mapping beats regex-guessed text, and
    any non-UNKNOWN beats UNKNOWN.
    """
    fallback = Status.UNKNOWN
    for text, machine in candidates:
        status = normalize(text, machine)
        if status is not Status.UNKNOWN:
            # Out-for-delivery / delivered / exception are "stronger"
            # signals than the generic in-transit; prefer the first.
            if status is not Status.IN_TRANSIT:
                return status
            if fallback is Status.UNKNOWN:
                fallback = status
    return fallback
