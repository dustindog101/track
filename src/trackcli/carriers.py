"""Carrier auto-detection from tracking-number shape.

Detection answers two questions:

1. Which tier-0 (official-key) providers are *eligible* for this number?
   e.g. the USPS XML API should never be asked to track a 1Z... UPS code.
2. Which URL should the last-resort browser provider open?

Detection is deliberately conservative -- when unsure we say "unknown"
and let the aggregator providers (which detect carriers server-side)
take over.  Unknown is never wrong; a wrong guess can be.
"""

from __future__ import annotations

import re

# (carrier, regex, human name) -- evaluated top to bottom.
_RULES: list[tuple[str, re.Pattern[str], str]] = [
    # UPS: 1Z + 16 alphanumeric (the check-digit layout is validated
    # server-side; we only gate on the unmistakable prefix).
    ("ups", re.compile(r"^1Z[0-9A-Z]{16}$"), "UPS"),

    # USPS: 20-22 digit IMpb codes starting 94/93/92/95/82, optionally
    # prefixed by 420 + 5-digit ZIP (Routing Application Code).
    ("usps", re.compile(r"^(420\d{5})?(94|93|92|95|82)\d{8,22}$"), "USPS"),

    # FedEx: 12-digit Express, 15-digit Ground/SmartPost, or 20-22 digit
    # codes starting with 96 (SSCC-style).
    ("fedex", re.compile(r"^(?:\d{12}|\d{15}|96\d{18,20})$"), "FedEx"),

    # DHL Express: 10 digits; DHL e-commerce often starts JD/JJD/JVGL.
    ("dhl", re.compile(r"^(?:\d{10}|JJD[A-Z0-9]+|JVGL[A-Z0-9]+|JD[A-Z0-9]+)$"),
     "DHL"),
]

UNKNOWN = "unknown"


def clean(number: str) -> str:
    """Normalize user input: strip spaces/dashes, uppercase letters."""
    return re.sub(r"[\s\-]+", "", number.strip()).upper()


def detect(number: str) -> tuple[str, str]:
    """Return (carrier_slug, human_name).  ("unknown", "") when unsure.

    Cleans the input itself so callers can pass raw user text.
    """
    number = clean(number)
    for slug, pattern, human in _RULES:
        if pattern.match(number):
            return slug, human
    return UNKNOWN, ""


def human_name(slug: str) -> str:
    """Pretty name for a carrier slug ("usps" -> "USPS")."""
    for carrier_slug, _, human in _RULES:
        if carrier_slug == slug:
            return human
    fixed = {
        "usps": "USPS", "ups": "UPS", "fedex": "FedEx", "dhl": "DHL",
        "usenberg": "USPS", "omniparcel": "Omniparcel",
    }
    return fixed.get(slug, slug.upper() if slug != UNKNOWN else "Unknown carrier")
