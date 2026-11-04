"""Core data model: shipments, events and the provider error taxonomy.

The whole CLI speaks exactly two dialects of this model:

* internally -- `Shipment` / `Event` dataclasses flow between providers,
  the engine and the renderers;
* externally -- `Shipment.to_dict()` is the JSON contract for agents
  (documented in docs/AGENTS.md).

Providers raise `ProviderError` instead of returning error objects; the
engine decides what each error *kind* means for the fallback chain.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class Status(str, Enum):
    """Normalized, provider-agnostic shipment lifecycle states.

    Agents should match on these values -- raw provider wording varies,
    this enum never does.
    """

    UNKNOWN = "unknown"                  # provider answered but had nothing
    PRE_TRANSIT = "pre_transit"          # label created / info received
    IN_TRANSIT = "in_transit"            # moving through the network
    OUT_FOR_DELIVERY = "out_for_delivery"
    DELIVERED = "delivered"
    EXCEPTION = "exception"              # held / damaged / address issue ...
    EXPIRED = "expired"                  # carrier gave up / stale
    RETURNED = "returned"                # on its way back to the sender


class ErrorKind(str, Enum):
    """Why a provider failed.  The engine treats each kind differently."""

    NOT_FOUND = "not_found"        # source answered: no such shipment (soft)
    RATE_LIMITED = "rate_limited"  # backed off -- cooldown, try later
    BLOCKED = "blocked"            # anti-bot wall / signature rejected
    CONFIG = "config"              # missing key / dependency for this source
    NETWORK = "network"            # timeouts, DNS, TLS -- probably transient
    PARSE = "parse"                # responded but body unintelligible


@dataclass
class Event:
    """One scan event in the shipment timeline.

    `timestamp` is an ISO-8601 string when the source provides one.
    Events from PackageRadar carry naive carrier-local clock times, so
    `tz_naive` is set to warn consumers that offsets are unreliable there.
    """

    timestamp: Optional[str] = None
    location: str = ""
    description: str = ""
    tz_naive: bool = False

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "timestamp": self.timestamp,
            "location": self.location,
            "description": self.description,
        }
        if self.tz_naive:
            out["tz_naive"] = True
        return out


@dataclass
class Shipment:
    """Everything we know about one tracking number, from one source."""

    tracking_number: str
    carrier: str = "unknown"             # normalized slug: usps / ups / ...
    status: Status = Status.UNKNOWN
    status_human: str = ""               # best human-readable status line
    estimated_delivery: Optional[str] = None   # ISO-8601 when the source gives it
    events: list[Event] = field(default_factory=list)  # oldest -> newest
    provider: str = ""                   # source that produced this result
    provider_tier: int = -1              # 0=official key, 1-2=keyless, 3=browser
    fetched_at: str = ""                 # ISO timestamp of the query
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tracking_number": self.tracking_number,
            "carrier": self.carrier,
            "status": self.status.value,
            "status_human": self.status_human,
            "estimated_delivery": self.estimated_delivery,
            "events": [e.to_dict() for e in self.events],
            "provider": self.provider,
            "provider_tier": self.provider_tier,
            "fetched_at": self.fetched_at,
            "notes": self.notes,
        }

    @property
    def has_data(self) -> bool:
        """True when the source gave us anything worth showing an agent."""
        return bool(
            self.events
            or self.estimated_delivery
            or self.status is not Status.UNKNOWN
        )


class ProviderError(Exception):
    """Raised by providers; the engine maps each kind onto chain behavior."""

    def __init__(self, provider: str, kind: ErrorKind, message: str,
                 cooldown_seconds: int = 0) -> None:
        super().__init__(message)
        self.provider = provider
        self.kind = kind
        self.cooldown_seconds = cooldown_seconds


@dataclass
class Attempt:
    """One line of the engine's decision log (shown by -v / JSON verbose)."""

    provider: str
    ok: bool
    note: str = ""
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "ok": self.ok,
            "note": self.note,
            "elapsed_ms": self.elapsed_ms,
        }


@dataclass
class TrackOutcome:
    """Engine result: the shipment if any source produced one, plus the log."""

    tracking_number: str = ""
    shipment: Optional[Shipment] = None
    attempts: list[Attempt] = field(default_factory=list)
    cached: bool = False
    cached_at: Optional[str] = None
    # "success" | "not_found" | "failed" -- drives the CLI exit code.
    kind: str = "failed"

    def to_dict(self, verbose: bool = False) -> dict[str, Any]:
        if self.shipment is None:
            out: dict[str, Any] = {
                "tracking_number": self.tracking_number,
                "carrier": None,
                "status": None,
                "status_human": None,
                "estimated_delivery": None,
                "events": [],
                "provider": None,
            }
        else:
            out = self.shipment.to_dict()
        out["kind"] = self.kind
        out["cached"] = self.cached
        if self.cached_at:
            out["cached_at"] = self.cached_at
        if verbose:
            out["attempts"] = [a.to_dict() for a in self.attempts]
        return out
