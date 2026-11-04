#!/usr/bin/env python3
"""Self-contained test runner for trackcli -- no pytest required.

    python3 tests/run_tests.py

Covers the pure logic (detection, status normalization, cache
roundtrips, cooldowns, signatures, the PackageRadar parser against a
sanitized captured fragment), the engine's fallback semantics using
fake in-memory providers, and the CLI verb dispatcher -- no network
needed.
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

# Make the package importable from a repo checkout.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from trackcli import carriers, status                      # noqa: E402
from trackcli.cache import Cache                            # noqa: E402
from trackcli.config import Config                          # noqa: E402
from trackcli.engine import Engine                          # noqa: E402
from trackcli.models import (ErrorKind, Event, ProviderError,  # noqa: E402
                             Shipment, Status)
from trackcli.providers.aftership import AfterShipProvider  # noqa: E402
from trackcli.providers.base import Provider                # noqa: E402
from trackcli.providers.packageradar import (               # noqa: E402
    PackageRadarProvider, _parse_events)

FIXTURE = Path(__file__).parent / "fixtures" / "packageradar_checkpoints.html"


# ------------------------------------------------------------------ dummies
class OkProvider(Provider):
    name, tier = "fake-ok", 1

    def fetch(self, number, carrier):
        return Shipment(tracking_number=number, carrier=carrier,
                        status_human="Delivered",
                        events=[Event(timestamp="2026-01-01T00:00:00",
                                      location="X", description="Delivered")],
                        provider=self.name)


class RateLimitedProvider(Provider):
    name, tier = "fake-rl", 1

    def fetch(self, number, carrier):
        raise ProviderError(self.name, ErrorKind.RATE_LIMITED, "slow down",
                            cooldown_seconds=900)


class NotFoundProvider(Provider):
    name, tier = "fake-nf", 1

    def fetch(self, number, carrier):
        raise ProviderError(self.name, ErrorKind.NOT_FOUND, "never heard of it")


class BoomProvider(Provider):
    name, tier = "fake-boom", 1

    def fetch(self, number, carrier):
        raise RuntimeError("bug in provider")


class EmptyProvider(Provider):
    """Answers 'record exists, zero scans' — must NOT count as success."""
    name, tier = "fake-empty", 1

    def fetch(self, number, carrier):
        return Shipment(tracking_number=number, provider=self.name)


# ------------------------------------------------------------------- tests
def test_carrier_detection():
    assert carriers.detect("9405500000000000000000")[0] == "usps"
    assert carriers.detect("9400 111 222 333 444 55 66")[0] == "usps"   # cleaned
    assert carriers.detect("42012345940015090123456789")[0] == "usps"
    assert carriers.detect("1Z999AA10123456784")[0] == "ups"
    assert carriers.detect("999999999999")[0] == "fedex"
    assert carriers.detect("123456789012345")[0] == "fedex"
    assert carriers.detect("1234567890")[0] == "dhl"
    assert carriers.detect("JJD000390009012345678")[0] == "dhl"
    assert carriers.detect("hello-world")[0] == "unknown"
    assert carriers.clean(" 1z-999 aa 101234 56784 ") == "1Z999AA10123456784"
    print("  carrier detection ............. OK")


def test_status_normalization():
    assert status.normalize("Delivered") is Status.DELIVERED
    assert status.normalize("In transit - Departed waypoint") is Status.IN_TRANSIT
    assert status.normalize("In-Transit, Arriving On Time") is Status.IN_TRANSIT
    assert status.normalize("USPS in Possession of Item") is Status.IN_TRANSIT
    assert status.normalize("Out for Delivery") is Status.OUT_FOR_DELIVERY
    assert status.normalize("Shipping Label Created, USPS Awaiting Item") \
        is Status.PRE_TRANSIT
    assert status.normalize("Return to sender") is Status.RETURNED
    assert status.normalize("Held in customs") is Status.EXCEPTION
    assert status.normalize(None, machine_status="InTransit") is Status.IN_TRANSIT
    assert status.normalize(None, machine_status="Delivered") is Status.DELIVERED
    assert status.normalize(None, machine_status="InfoReceived") is Status.PRE_TRANSIT
    assert status.normalize("gibberish") is Status.UNKNOWN
    # Priority: "out for delivery" beats generic movement words.
    assert status.normalize("Out for delivery - departed facility") \
        is Status.OUT_FOR_DELIVERY
    print("  status normalization .......... OK")


def test_cache_roundtrip_and_cooldowns():
    with tempfile.TemporaryDirectory() as tmp:
        cache = Cache(ttl_seconds=300)
        cache.root = Path(tmp) / "track"
        cache.results_dir = cache.root / "results"
        cache.cooldowns_path = cache.root / "cooldowns.json"

        shipment = Shipment(
            tracking_number="9405500000000000000000", carrier="usps",
            status=Status.IN_TRANSIT, status_human="In transit",
            estimated_delivery="2026-11-07T21:00:00-07:00",
            events=[Event(timestamp="2026-11-03T18:21:00",
                          location="Springfield Il Distribution Center",
                          description="Departed waypoint", tz_naive=True)],
            provider="packageradar", provider_tier=2,
            fetched_at="2026-11-04T17:54:00+00:00")
        cache.put(shipment)

        got = cache.get("9405500000000000000000")
        assert got is not None
        back, cached_at = got
        # The rehydrated object must have live enum/dataclass types again.
        assert back.status is Status.IN_TRANSIT
        assert isinstance(back.events[0], Event)
        assert back.events[0].tz_naive is True
        assert back.estimated_delivery == shipment.estimated_delivery

        # Expiry: a TTL of 0 means nothing is served.
        cache.ttl = 0
        assert cache.get("9405500000000000000000") is None
        cache.ttl = 300

        # Cooldowns.
        cache.set_cooldown("aftership", "N1", 900)
        assert cache.in_cooldown("aftership", "N1")
        assert not cache.in_cooldown("aftership", "N2")
        assert not cache.in_cooldown("packageradar", "N1")
        cache.clear_cooldowns_for("N1")
        assert not cache.in_cooldown("aftership", "N1")
        print("  cache + cooldowns ............. OK")


def test_aftership_signature_shape():
    headers = AfterShipProvider._gateway_headers(
        "POST", "/api/v2/direct-trackings/batch", b"{}")
    assert headers["am-sign-version"] == "v1"
    assert headers["digest"].startswith("=") is False
    assert 'username="track.aftership.com"' in headers["authorization"]
    assert 'algorithm="hmac-sha256"' in headers["authorization"]
    assert 'headers="digest"' in headers["authorization"]
    # Digest must be the base64 MD5 of exactly the body given.
    import base64, hashlib
    assert headers["digest"] == base64.b64encode(
        hashlib.md5(b"{}").digest()).decode()
    print("  aftership signature shape ..... OK")


def test_packageradar_parser():
    events = _parse_events(FIXTURE.read_text(encoding="utf-8"))
    assert len(events) == 3, f"expected 3 events, got {len(events)}"
    first = events[0]  # fixture is listed newest-first
    assert first.description == "Package information received"
    assert first.location == "Springfield, IL"
    # The bogus +03:00 offset is dropped; wall time kept, tz_naive set.
    assert first.timestamp == "2026-11-02T11:48:00"
    assert first.tz_naive is True
    newest = events[-1]
    assert newest.description == "In transit - Departed waypoint"
    print("  packageradar parser ........... OK")


def _fresh_cache():
    cache = Cache(ttl_seconds=300)
    tmp = tempfile.mkdtemp()
    cache.root = Path(tmp) / "track"
    cache.results_dir = cache.root / "results"
    cache.cooldowns_path = cache.root / "cooldowns.json"
    return cache


def test_engine_fallback_order():
    # (1) rate-limited first -> engine records cooldown, falls through to OK.
    cache = _fresh_cache()
    engine = Engine(config=Config(), cache=cache,
                    providers=[RateLimitedProvider(Config()), OkProvider(Config())])
    outcome = engine.track("9405500000000000000000")
    assert outcome.kind == "success"
    assert outcome.shipment.provider == "fake-ok"
    assert cache.in_cooldown("fake-rl", "9405500000000000000000")
    print("  engine: cooldown + fallback ... OK")

    # (2) second run: the cooled-down provider is skipped, cache serves hit.
    engine2 = Engine(config=Config(), cache=cache,
                     providers=[RateLimitedProvider(Config()), OkProvider(Config())])
    outcome2 = engine2.track("9405500000000000000000")
    assert outcome2.cached is True
    assert [a.note for a in outcome2.attempts].count("cache hit") == 1
    print("  engine: cached second call .... OK")

    # (3) all sources say not found -> kind=not_found.
    cache3 = _fresh_cache()
    engine3 = Engine(config=Config(), cache=cache3,
                     providers=[NotFoundProvider(Config())])
    assert engine3.track("1Z999AA10123456784").kind == "not_found"
    print("  engine: not-found kind ........ OK")

    # (4) a provider crashing (non-ProviderError) must not kill the chain.
    engine4 = Engine(config=Config(), cache=_fresh_cache(),
                     providers=[BoomProvider(Config()), OkProvider(Config())])
    outcome4 = engine4.track("9405500000000000000000")
    assert outcome4.kind == "success"
    assert "unexpected RuntimeError" in outcome4.attempts[0].note
    print("  engine: crash containment ..... OK")

    # (5) an empty answer is not success: kind=not_found, thin shipment
    #     still attached for agents.
    cache5 = _fresh_cache()
    engine5 = Engine(config=Config(), cache=cache5,
                     providers=[EmptyProvider(Config())])
    outcome5 = engine5.track("1Z999AA10123456784")
    assert outcome5.kind == "not_found"
    assert outcome5.shipment is not None and not outcome5.shipment.has_data
    assert cache5.get("1Z999AA10123456784") is None   # thin answers skip cache
    print("  engine: thin not-found ........ OK")


def test_engine_refresh_bypasses_cache():
    cache = _fresh_cache()
    engine = Engine(config=Config(), cache=cache, providers=[OkProvider(Config())])
    assert engine.track("9405500000000000000000").cached is False
    assert engine.track("9405500000000000000000").cached is True
    assert engine.track("9405500000000000000000", refresh=True).cached is False
    print("  engine: refresh bypass ....... OK")


def test_cli_interface():
    """The verb dispatcher: help, version, config, providers, usage errors."""
    import contextlib
    import io

    from trackcli import cli

    def run(*argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    code, out, _ = run("help")
    assert code == 0 and "topics" in out and "track help <topic>" in out
    code, out, _ = run("help", "json")
    assert code == 0 and '"tracking_number"' in out and "kind" in out
    code, out, _ = run("help", "topics")
    assert code == 0 and "exit-codes" in out
    code, _, err = run("help", "no-such-topic")
    assert code == 3 and "no help topic" in err and "exit-codes" in err
    code, out, _ = run("--help", "json")          # alias anywhere
    assert code == 0 and '"tracking_number"' in out
    code, _, _ = run("123", "--help")             # number + alias = help
    assert code == 0

    code, out, _ = run("version")
    assert code == 0 and out.strip() == f"track {cli.__version__}"
    code, out, _ = run("config")
    assert code == 0 and "ttl_seconds" in out
    code, out, _ = run("providers")               # piped -> JSON rows
    import json as _json
    rows = _json.loads(out)
    assert {r["name"] for r in rows} >= {"aftership", "packageradar"}
    assert all("tier" in r and "ready" in r for r in rows)

    code, _, err = run()
    assert code == 3 and "try `track help`" in err
    code, _, err = run("hepl")                    # near-miss verb hint
    assert code == 3 and "did you mean" in err
    code, _, err = run("refresh")                 # verb without a number
    assert code == 3 and "needs at least one" in err
    code, _, err = run("--bogus")
    assert code == 3 and "unknown option" in err
    print("  cli verb dispatcher ........... OK")


if __name__ == "__main__":
    print("trackcli test suite")
    test_carrier_detection()
    test_status_normalization()
    test_cache_roundtrip_and_cooldowns()
    test_aftership_signature_shape()
    test_packageradar_parser()
    test_engine_fallback_order()
    test_engine_refresh_bypasses_cache()
    test_cli_interface()
    print("All tests passed.")
