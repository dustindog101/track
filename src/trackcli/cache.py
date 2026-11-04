"""Disk cache + provider cooldown store.

Two small JSON stores live under ``$XDG_CACHE_HOME/track`` (default
``~/.cache/track``):

* ``results/<sha1(number)>.json``  -- last good result, honored for
  ``ttl_seconds`` (default 300 s) so agents polling in loops are gentle
  on the free sources;
* ``cooldowns.json``               -- maps ``provider:number`` to a unix
  timestamp until which the engine will not ask that provider about that
  number again (AfterShip answers HTTP 402 for ~15 min after one query).

Writes are atomic (tmp file + rename) so a Ctrl-C mid-write never
corrupts the cache.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Optional

from .models import Shipment


def cache_root() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(base) / "track"


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp_name, path)
    except OSError:
        # Best effort only -- a read-only cache dir must never break tracking.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


class Cache:
    def __init__(self, ttl_seconds: int = 300) -> None:
        self.ttl = ttl_seconds
        self.root = cache_root()
        self.results_dir = self.root / "results"
        self.cooldowns_path = self.root / "cooldowns.json"

    # ------------------------------------------------------------ results
    def _result_path(self, number: str) -> Path:
        digest = hashlib.sha1(number.encode()).hexdigest()[:20]
        return self.results_dir / f"{digest}.json"

    def get(self, number: str) -> Optional[tuple[Shipment, float]]:
        """Return (shipment, cached_at_epoch) when a fresh entry exists."""
        path = self._result_path(number)
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        age = time.time() - blob.get("cached_at", 0)
        if age > self.ttl:
            return None
        try:
            return self._rehydrate(blob["shipment"]), blob["cached_at"]
        except (KeyError, TypeError, ValueError):
            return None

    @staticmethod
    def _rehydrate(data: dict) -> Shipment:
        """Rebuild a live Shipment from its JSON form.

        `to_dict()` flattened the Status enum and Event dataclasses;
        putting those raw dicts into `Shipment(**...)` would yield a
        shipment with a *string* status and dict events, which crashes
        the renderers.  Rehydrate both explicitly.
        """
        from .models import Event, Status

        data = dict(data)
        data["status"] = Status(data.get("status", "unknown"))
        data["events"] = [
            Event(
                timestamp=e.get("timestamp"),
                location=e.get("location", ""),
                description=e.get("description", ""),
                tz_naive=bool(e.get("tz_naive", False)),
            )
            for e in data.get("events") or []
        ]
        return Shipment(**data)

    def put(self, shipment: Shipment) -> None:
        now = time.time()
        payload = json.dumps(
            {"cached_at": now, "shipment": shipment.to_dict()},
            ensure_ascii=False)
        _atomic_write(self._result_path(shipment.tracking_number), payload)

    # ---------------------------------------------------------- cooldowns
    def _read_cooldowns(self) -> dict[str, float]:
        try:
            data = json.loads(self.cooldowns_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def in_cooldown(self, provider: str, number: str) -> bool:
        until = self._read_cooldowns().get(f"{provider}:{number}", 0)
        return time.time() < until

    def set_cooldown(self, provider: str, number: str, seconds: int) -> None:
        if seconds <= 0:
            return
        store = self._read_cooldowns()
        # Garbage-collect expired entries on every write.
        now = time.time()
        store = {k: v for k, v in store.items() if v > now}
        store[f"{provider}:{number}"] = now + seconds
        _atomic_write(self.cooldowns_path,
                      json.dumps(store, indent=1, sort_keys=True))

    def clear_cooldowns(self) -> None:
        _atomic_write(self.cooldowns_path, "{}")

    def clear_cooldowns_for(self, number: str) -> None:
        """Forget cooldowns for one number (--refresh must get a live shot
        at every source, that is the whole point of the flag)."""
        store = self._read_cooldowns()
        suffix = f":{number}"
        keep = {k: v for k, v in store.items() if not k.endswith(suffix)}
        if len(keep) != len(store):
            _atomic_write(self.cooldowns_path,
                          json.dumps(keep, indent=1, sort_keys=True))
