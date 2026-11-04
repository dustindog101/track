"""Provider registry -- the fallback chain, in engine-visit order.

The engine instantiates these lazily (some constructors are free; the
browser one must not import playwright until actually used) and walks
the list top-down.  Tier 0 providers self-disable via ``supports()``
when their key isn't configured, so the keyless chain below is what
ships working out of the box::

    AfterShip (tier 1, signed keyless JSON)
      -> PackageRadar (tier 2, HTML fragments)
        -> Browser (tier 3, playwright, last resort)

Official keyed APIs (tier 0) run *before* the keyless chain whenever
the user configured credentials -- better data quality, official source.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .aftership import AfterShipProvider
from .base import Provider
from .browser import BrowserProvider
from .dhl_api import DhlApiProvider
from .packageradar import PackageRadarProvider
from .usps_api import UspsApiProvider

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Config

#: canonical chain order -- index order == visit order
PROVIDER_CLASSES: tuple[type[Provider], ...] = (
    UspsApiProvider,        # tier 0 -- USPS official (free USERID)
    DhlApiProvider,         # tier 0 -- DHL official (free key)
    AfterShipProvider,      # tier 1 -- keyless, 2000+ carriers, best JSON
    PackageRadarProvider,   # tier 2 -- keyless, most scrape-tolerant
    BrowserProvider,        # tier 3 -- playwright, last resort
)


def build_chain(config: "Config") -> list[Provider]:
    """Instantiate the chain for a config, skipping the browser provider
    when disabled (``allow_browser: false`` / ``TRACK_NO_BROWSER``)."""
    providers: list[Provider] = []
    for cls in PROVIDER_CLASSES:
        if cls is BrowserProvider and not config.allow_browser:
            continue
        providers.append(cls(config))
    return providers


__all__ = [
    "Provider", "PROVIDER_CLASSES", "build_chain",
    "AfterShipProvider", "PackageRadarProvider",
    "UspsApiProvider", "DhlApiProvider", "BrowserProvider",
]
