"""The engine: walks the provider chain and produces one clean outcome.

Responsibilities (and deliberately NOT the providers'):

* order providers: configured tier-0 official APIs first, then keyless,
  browser last;
* skip providers that are in per-number cooldown (rate-limit memory);
* honor each provider's ``supports()`` eligibility gate;
* interpret failures: NOT_FOUND is *soft* (keep going), RATE_LIMITED /
  BLOCKED set a cooldown then keep going, CONFIG / NETWORK just keep
  going;
* cache the first good result for ``ttl_seconds`` so agents polling in
  loops cost the free sources nothing;
* record a per-provider attempt log (shown with -v, embedded in
  verbose JSON).

A successful outcome is the first provider whose answer carries data.
A number every source calls unknown is kind="not_found" (exit code 1);
if every eligible source *errored*, kind="failed" (exit code 2).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from .cache import Cache
from .config import Config
from .models import (Attempt, ErrorKind, ProviderError, Shipment,
                     TrackOutcome)
from .providers import Provider, build_chain
from .status import best_status


@dataclass
class Engine:
    config: Config
    cache: Cache
    providers: list[Provider] = field(default_factory=list)
    #: only run this one provider (the --provider debug flag)
    only: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.providers:
            self.providers = build_chain(self.config)

    # ------------------------------------------------------------- helpers
    def _eligible(self, provider: Provider, number: str, carrier: str,
                  attempts: list[Attempt]) -> bool:
        if self.only and provider.name != self.only:
            return False
        if not provider.supports(number, carrier):
            return False
        if self.cache.in_cooldown(provider.name, number):
            attempts.append(Attempt(provider=provider.name, ok=False,
                                    note="skipped (cooling down after a "
                                         "rate-limit)"))
            return False
        return True

    def _record_cooldown(self, error: ProviderError, number: str) -> None:
        if error.cooldown_seconds > 0:
            self.cache.set_cooldown(error.provider, number,
                                    error.cooldown_seconds)

    # ---------------------------------------------------------------- main
    def track(self, number: str, refresh: bool = False) -> TrackOutcome:
        carrier, _ = self._detect(number)

        # 1. Fresh cache?  Nothing downstream needs to happen at all.
        #    (--refresh skips this: the user explicitly wants live data.)
        hit = None if refresh else self.cache.get(number)
        if hit is not None and not self.only:
            shipment, cached_at = hit
            return TrackOutcome(
                tracking_number=number, shipment=shipment, cached=True,
                cached_at=_iso(cached_at), kind="success",
                attempts=[Attempt(provider=shipment.provider, ok=True,
                                  note="cache hit")])

        attempts: list[Attempt] = []
        saw_not_found = False
        # First provider that answered with a well-formed but empty body
        # ("not picked up yet").  Kept as a last-resort success if every
        # richer source fails.
        fallback: Optional[Shipment] = None

        # 2. Walk the chain.
        for provider in self.providers:
            if not self._eligible(provider, number, carrier, attempts):
                continue
            started = time.monotonic()
            try:
                shipment = provider.fetch(number, carrier)
            except ProviderError as error:
                attempts.append(Attempt(
                    provider=provider.name, ok=False,
                    note=f"{error.kind.value}: {error}",
                    elapsed_ms=int((time.monotonic() - started) * 1000)))
                self._record_cooldown(error, number)
                if error.kind is ErrorKind.NOT_FOUND:
                    saw_not_found = True
                continue
            except Exception as unexpected:  # noqa: BLE001 -- providers must
                # never crash the chain; still, guard against bugs here.
                attempts.append(Attempt(
                    provider=provider.name, ok=False,
                    note=f"unexpected {type(unexpected).__name__}: "
                         f"{unexpected}",
                    elapsed_ms=int((time.monotonic() - started) * 1000)))
                continue

            attempts.append(Attempt(
                provider=provider.name, ok=True,
                note=f"{len(shipment.events)} events, "
                     f"status={shipment.status_human or 'n/a'}",
                elapsed_ms=int((time.monotonic() - started) * 1000)))

            # Normalize the lifecycle status once, centrally: the human
            # status line first, then the newest event's description.
            newest = shipment.events[-1].description if shipment.events \
                else None
            shipment.status = best_status(
                (shipment.status_human, None),
                (newest, None))

            if shipment.has_data:
                self.cache.put(shipment)
                return TrackOutcome(tracking_number=number,
                                    shipment=shipment, attempts=attempts,
                                    kind="success")
            if fallback is None:
                fallback = shipment

        # 3. Nothing hard-succeeded.  Use the first "answered, empty" body
        #    if we have one (some sources auto-create a record on first
        #    search, returning zero scans) -- but classify it as
        #    not_found: nothing authoritative was learned.  The thin
        #    shipment rides along so agents still see what was found.
        if fallback is not None:
            return TrackOutcome(tracking_number=number, shipment=fallback,
                                attempts=attempts, kind="not_found")
        if saw_not_found:
            return TrackOutcome(tracking_number=number, attempts=attempts,
                                kind="not_found")
        return TrackOutcome(tracking_number=number, attempts=attempts,
                            kind="failed")

    @staticmethod
    def _detect(number: str) -> tuple[str, str]:
        from . import carriers
        return carriers.detect(number)


def _iso(epoch: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, tz=timezone.utc
                                  ).isoformat(timespec="seconds")
