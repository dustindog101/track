"""CLI front door: verbs, not flags.  One command does the right thing.

Usage philosophy -- zero flags, zero configuration:

    track <number>            track a package (card in a TTY, JSON piped)
    track help [topic]        the manual
    track refresh <number>    force one live query past the cache
    track providers           show the data-source chain
    track config              print a documented config.json
    track version             print the version

Behavior matrix:

* terminal  -> pretty ANSI card
* piped/CI  -> JSON automatically
* re-run within TTL -> cached result, zero network
* rate-limited source -> engine remembers, next call skips it

Exit codes (stable contract for scripts and agents):

    0  tracked -- result below (any status incl. "label created")
    1  not found -- sources answered, number unknown to all of them
    2  failed -- every eligible source errored (network / blocking)
    3  usage error -- bad arguments

A handful of power flags survive for scripting and debugging; they are
documented in `track help advanced` and never required.
"""

from __future__ import annotations

import json
import os
import sys

from . import __version__
from .cache import Cache
from .carriers import clean
from .config import Config, example_config, load
from .engine import Engine
from .helpsys import (PROG, TOPICS, main_help, render_topic,  # noqa: F401
                      topics_page)
from .models import TrackOutcome
from .ui import render, render_failure

#: verbs -- dispatched when they appear as the FIRST argument
VERBS = ("help", "version", "refresh", "providers", "config")

# --------------------------------------------------------------- parsing
#: boolean flags -- power users only, see `track help advanced`
_BOOL_FLAGS = {
    "--json": "json", "--pretty": "pretty",
    "-v": "verbose", "--verbose": "verbose",
    "--no-cache": "no_cache", "--no-color": "no_color",
    "--no-browser": "no_browser", "--refresh": "refresh_flag",
}
#: flags that consume the following token
_VALUE_FLAGS = {
    "-p": "provider", "--provider": "provider",
    "-t": "timeout", "--timeout": "timeout",
}
#: global aliases -- honored anywhere, before verb dispatch
_ALIASES = {"-h": "help", "--help": "help", "--version": "version",
            "--example-config": "config"}


class _Args:
    """Parsed invocation (a step above a bare dict for readability)."""

    def __init__(self) -> None:
        self.verb: str | None = None       # first positional, if a verb
        self.topic: str | None = None      # `track help <topic>`
        self.numbers: list[str] = []
        self.force: str | None = None      # -h/--help/--version anywhere
        self.json = False
        self.pretty = False
        self.verbose = False
        self.no_cache = False
        self.no_color = False
        self.no_browser = False
        self.refresh_flag = False
        self.provider: str | None = None
        self.timeout: int | None = None

    @property
    def refresh(self) -> bool:
        """True when the invocation asked for a live query."""
        return self.verb == "refresh" or self.refresh_flag


class _UsageError(Exception):
    """Anything that should exit 3 with a friendly one-liner."""


def _parse(argv: list[str]) -> _Args:
    args = _Args()
    positional: list[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in _ALIASES:
            # `track <anything> --help` must never be read as a number.
            if args.force is None:
                args.force = _ALIASES[tok]
            i += 1
            continue
        if tok in _BOOL_FLAGS:
            setattr(args, _BOOL_FLAGS[tok], True)
            i += 1
            continue
        if tok in _VALUE_FLAGS:
            if i + 1 >= len(argv):
                raise _UsageError(f"option {tok} needs a value")
            setattr(args, _VALUE_FLAGS[tok], argv[i + 1])
            i += 2
            continue
        if tok.startswith("-") and len(tok) > 1:
            # support --provider=NAME / --timeout=30 spellings
            if "=" in tok:
                key, _, val = tok.partition("=")
                if key in _VALUE_FLAGS:
                    setattr(args, _VALUE_FLAGS[key], val)
                    i += 1
                    continue
                if key in _BOOL_FLAGS and val in ("", "true"):
                    setattr(args, _BOOL_FLAGS[key], True)
                    i += 1
                    continue
            raise _UsageError(f"unknown option {tok}")
        positional.append(tok)
        i += 1

    # A global alias wins over everything (`track <num> --help` = help).
    if args.force is not None:
        if args.force == "help":
            # Only a real topic slug narrows the page; anything else
            # (e.g. `track 123 --help`) just means "show the manual".
            args.verb = "help"
            if positional and (positional[0] in TOPICS
                               or positional[0] == "topics"):
                args.topic = positional[0]
        else:
            args.verb = args.force
        return args

    if not positional:
        raise _UsageError("missing a tracking number")

    first = positional[0]
    if first in VERBS:
        args.verb = first
        rest = positional[1:]
        if first == "help":
            args.topic = rest[0] if rest else None
        elif first == "refresh":
            args.numbers = rest
        # version / providers / config ignore extra words
    else:
        _typo_hint(first)
        args.numbers = positional
    return args


def _typo_hint(word: str) -> None:
    """A tracking number is never one edit away from a verb -- if it is,
    the user probably mistyped one.  Surface the guess; the caller then
    exits 3 instead of burning a live query on a typo."""
    def one_edit(a: str, b: str) -> bool:
        if abs(len(a) - len(b)) > 1:
            return False
        if len(a) == len(b):
            diffs = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
            if len(diffs) == 1:
                return True
            # a transposition ("hepl" -> "help") is one clumsy keystroke
            return (len(diffs) == 2 and diffs[1] == diffs[0] + 1
                    and a[diffs[0]] == b[diffs[1]]
                    and a[diffs[1]] == b[diffs[0]])
        short, long = (a, b) if len(a) < len(b) else (b, a)
        return any(long[:i] + long[i + 1:] == short
                   for i in range(len(long)))

    for verb in VERBS + ("topics",):
        if one_edit(word.lower(), verb):
            raise _UsageError(f"unknown command '{word}' -- did you mean "
                              f"`{PROG} {verb}`?")
    # Not a near-miss: fall through, treat as a tracking number.


# --------------------------------------------------------------- output
def _wants_json(args: _Args) -> bool:
    if args.pretty:
        return False
    if args.json:
        return True
    return not sys.stdout.isatty()


def _emit_json(outcomes: list[TrackOutcome], verbose: bool) -> None:
    payload = [o.to_dict(verbose=verbose) for o in outcomes]
    if len(payload) == 1:
        payload = payload[0]
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def _emit_pretty(outcomes: list[TrackOutcome], args: _Args,
                 color: bool) -> None:
    for outcome in outcomes:
        if outcome.kind == "success" and outcome.shipment is not None:
            print(render(outcome.shipment, color=color,
                         cached=outcome.cached,
                         cached_at=outcome.cached_at))
        else:
            print(render_failure(outcome.tracking_number, outcome.kind,
                                 outcome.attempts, color=color,
                                 shipment=outcome.shipment))
        if args.verbose:
            for attempt in outcome.attempts:
                mark = "✓" if attempt.ok else "✗"
                print(f"   {mark} {attempt.provider}: {attempt.note} "
                      f"({attempt.elapsed_ms} ms)")
        print()


def _exit_code(outcomes: list[TrackOutcome]) -> int:
    kinds = [o.kind for o in outcomes]
    if "success" in kinds:
        return 0          # partial success: deliver what we got
    if "not_found" in kinds:
        return 1
    return 2


def _color_ok(args: _Args) -> bool:
    return not args.no_color and not os.environ.get("NO_COLOR") \
        and sys.stdout.isatty()


# ----------------------------------------------------------- commands
def _cmd_help(args: _Args, color: bool) -> int:
    if args.topic is None:
        print(main_help(__version__, color))
        return 0
    if args.topic == "topics":
        print(topics_page(color))
        return 0
    page = render_topic(args.topic, color)
    if page is None:
        print(f"{PROG}: no help topic '{args.topic}'", file=sys.stderr)
        print(topics_page(False), file=sys.stderr)
        return 3
    print(page)
    return 0


def _cmd_version() -> int:
    print(f"{PROG} {__version__}")
    return 0


def _cmd_config() -> int:
    print(example_config())
    return 0


#: static blurbs for `track providers` (keys match Provider.name)
_PROVIDER_INFO = {
    "usps-api": ("USPS official APIs -- best data, real ETAs",
                 "free USERID (usps.com/web-tools)"),
    "dhl-api": ("DHL Express official API",
                "free key (developer.dhl.com)"),
    "aftership": ("keyless signed widget API -- 2,000+ carriers",
                  "nothing (built in)"),
    "packageradar": ("keyless checkpoint scraping -- very tolerant",
                     "nothing (built in)"),
    "browser": ("real Chromium on the carrier's own page -- last resort",
                "playwright (pipx install 'track[browser]')"),
}


def _provider_rows(config: Config) -> list[dict]:
    """Describe the chain as configured on THIS machine (no network)."""
    from .providers import build_chain
    try:
        import playwright  # noqa: F401
        has_playwright = True
    except ImportError:
        has_playwright = False

    rows = []
    for provider in build_chain(config):
        info = _PROVIDER_INFO.get(provider.name,
                                  ("", "see docs/PROVIDERS.md"))
        if provider.name == "usps-api":
            ready = config.has_usps_legacy or config.has_usps_oauth
        elif provider.name == "dhl-api":
            ready = config.has_dhl
        elif provider.name == "browser":
            ready = has_playwright and config.allow_browser
        else:
            ready = True
        rows.append({
            "name": provider.name,
            "tier": provider.tier,
            "description": info[0],
            "requires": info[1],
            "ready": ready,
        })
    return rows


def _cmd_providers(config: Config, args: _Args) -> int:
    rows = _provider_rows(config)
    if _wants_json(args):
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    for row in rows:
        if row["ready"]:
            state = "ready"
        elif row["name"] == "browser" and config.allow_browser:
            state = "missing dep"
        else:
            state = "not configured"
        print(f"  tier {row['tier']}  {row['name']:<14} {state:<15} "
              f"{row['description']}")
        print(f"       {'':<14} {'':<15} needs: {row['requires']}")
    print()
    print("  the engine walks these top-down; first real answer wins.")
    print("  `track help providers` explains each one and rate limits.")
    return 0


# ----------------------------------------------------------- tracking
def _drop_cached_result(cache: Cache, number: str) -> None:
    """Remove a result written moments ago by a --no-cache run."""
    try:
        cache._result_path(number).unlink()
    except OSError:
        pass


def _cmd_track(args: _Args) -> int:
    if not args.numbers:
        raise _UsageError(f"`{args.verb or PROG}` needs at least one "
                          "tracking number")
    config = load()
    if args.timeout is not None:
        try:
            config.timeout = int(args.timeout)
        except ValueError:
            raise _UsageError(f"--timeout wants seconds, got "
                              f"'{args.timeout}'")
    if args.no_browser:
        config.allow_browser = False

    if args.provider:
        known = {p.name for p in _provider_rows(config)}
        if args.provider not in known:
            raise _UsageError(f"unknown provider '{args.provider}' "
                              f"-- known: {', '.join(sorted(known))}")

    cache = Cache(ttl_seconds=config.ttl_seconds)
    engine = Engine(config=config, cache=cache, only=args.provider)

    outcomes: list[TrackOutcome] = []
    for raw in args.numbers:
        number = clean(raw)
        if not number:
            raise _UsageError("empty tracking number")
        if args.refresh or args.no_cache:
            # A refresh must also defeat the cooldown memory: the user
            # explicitly asking for a live check gets one.
            cache.clear_cooldowns_for(number)
        outcome = engine.track(number, refresh=args.refresh)
        if args.no_cache:
            # --no-cache means: live query, and don't store the result.
            _drop_cached_result(cache, number)
        outcomes.append(outcome)

    if _wants_json(args):
        _emit_json(outcomes, verbose=args.verbose)
    else:
        _emit_pretty(outcomes, args, _color_ok(args))
    return _exit_code(outcomes)


# --------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    try:
        args = _parse(list(sys.argv[1:] if argv is None else argv))
        color = _color_ok(args)

        if args.verb == "help":
            return _cmd_help(args, color)
        if args.verb == "version":
            return _cmd_version()
        if args.verb == "config":
            return _cmd_config()
        if args.verb == "providers":
            return _cmd_providers(load(), args)
        return _cmd_track(args)
    except _UsageError as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        print(f"  try `{PROG} help`", file=sys.stderr)
        return 3


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
