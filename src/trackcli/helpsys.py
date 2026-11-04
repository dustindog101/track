"""The help system: ``track help [topic]`` -- a manual that ships with
the binary.

Help content lives here (not in argparse strings) so it can be kept
in sync with the real contract, rendered for both humans and pipes,
and extended one topic at a time.  Rules for every page:

* fits in 80 columns;
* plain text when piped (agents parse it), light ANSI on a TTY;
* every tracking number shown is synthetic -- never a real shipment.

Topic bodies use a two-marker mini-format:

* ``!! text``  -> a bold section header (marker stripped);
* ``~~ text``  -> a dim meta line (marker stripped).
"""

from __future__ import annotations

from typing import Optional

PROG = "track"

_BOLD, _DIM, _CYAN, _RESET = "\033[1m", "\033[2m", "\033[36m", "\033[0m"

#: synthetic example numbers -- safe to publish, obviously not real
EX_USPS = "9405500000000000000000"
EX_UPS = "1Z999AA10123456784"
EX_FEDEX = "999999999999"
EX_DHL = "1234567890"


class _P:
    """Tiny painter: returns text unchanged when color is off."""

    def __init__(self, color: bool) -> None:
        self.color = color

    def __call__(self, text: str, *codes: str) -> str:
        if not self.color or not codes:
            return text
        return "".join(codes) + text + _RESET


def _render(body: str, color: bool) -> str:
    """Strip markers and apply TTY-aware styling line by line."""
    p = _P(color)
    out: list[str] = []
    for raw in body.splitlines():
        if raw.startswith("!! "):
            out.append(p(raw[3:], _BOLD))
        elif raw.startswith("~~ "):
            out.append(p(raw[3:], _DIM))
        else:
            out.append(raw)
    return "\n".join(out)


# --------------------------------------------------------------------------
# topic pages -- newest content lives at the bottom of each string
# --------------------------------------------------------------------------
_MAIN = f"""\
!! track {{version}} -- free package tracking from the command line

!! usage
  track <number>            track a package -- that's it
  track help [topic]        the manual (topics below)
  track refresh <number>    force a live query, past the cache
  track providers           show the data sources and their status
  track config              print a documented ~/.config/track/config.json
  track version             print the version

!! how it works
  - the carrier is auto-detected (USPS, UPS, FedEx, DHL, more)
  - a fallback chain of free sources is queried until one answers
  - terminal -> color card; piped or CI -> JSON.  No flags needed
  - results are cached ~5 min and rate-limited sources are skipped
    automatically, so polling loops are safe

!! examples
  track {EX_USPS}              a USPS package
  track {EX_UPS}                  UPS -- carrier auto-detected
  track {EX_USPS} | jq .status    JSON, no flags, any pipe
  track refresh {EX_USPS}         re-check right now

!! exit codes   (details: track help exit-codes)
  0 tracked | 1 not found | 2 sources failed | 3 bad usage

!! topics   (track help <topic>)
  json         the exact JSON contract for agents and scripts
  exit-codes   every code and what to do about it
  providers    the fallback chain: who's who, rate limits
  cache        the 5-minute cache, cooldowns, refreshing
  carriers     which number shapes map to which carrier
  config       config.json + TRACK_* environment variables
  advanced     power flags (one source only, verbose, timeouts...)
  examples     copy-paste recipes

~~ more: README.md, docs/AGENTS.md (for bots), docs/PROVIDERS.md
"""

_JSON = f"""\
!! json output -- the machine contract

The one rule: when stdout is not a terminal, the result is JSON.

  track {EX_USPS} | jq .

One number prints one JSON object; several numbers print an array.

{{
  "tracking_number": "{EX_USPS}",
  "carrier": "usps",
  "status": "in_transit",
  "status_human": "The package is in transit for 3 days.",
  "estimated_delivery": null,
  "events": [
    {{
      "timestamp": "2026-11-03T18:21:00",
      "location": "Springfield Il Distribution Center",
      "description": "In transit - Departed waypoint",
      "tz_naive": true
    }}
  ],
  "provider": "packageradar",
  "provider_tier": 2,
  "fetched_at": "2026-11-04T17:54:00+00:00",
  "notes": ["timestamps are carrier-local (no reliable timezone)"],
  "kind": "success",
  "cached": false
}}

!! fields
  status                normalized enum -- match on this, never on
                        wording: unknown, pre_transit, in_transit,
                        out_for_delivery, delivered, exception,
                        expired, returned
  kind                  success | not_found | failed -- the
                        authoritative outcome.  A not_found may still
                        carry a thin object (status null, empty
                        events) when a source has a record but the
                        carrier has not scanned it yet
  estimated_delivery    ISO-8601, or null -- keyless sources rarely
                        publish an ETA
  events                oldest -> newest; tz_naive: true warns that
                        timestamps are carrier-local wall-clock times
  provider / tier       which source answered (0 official key,
                        1-2 keyless, 3 browser)
  cached / cached_at    true when served from the 5-minute cache
  notes                 data-quality caveats from the source
  attempts              only with -v: the per-source decision log

~~ full contract + polling recipes: docs/AGENTS.md
"""

_EXIT_CODES = """\
!! exit codes -- stable contract for scripts and agents

  0  success      a result came back (any status, even "label
                  created") -> parse the JSON, or read the card
  1  not_found    sources answered; the number is unknown to all of
                  them, or the label exists but no carrier has
                  scanned it yet -> re-check the number; retry later
                  if brand-new
  2  failed       every eligible source errored (network, blocking,
                  rate limits) -> retry with backoff, or
                  `track refresh <number>` later
  3  usage        bad arguments or unknown help topic -> fix the
                  invocation; `track help` is the manual

The JSON "kind" field mirrors this: success | not_found | failed.
Partial success (several numbers, one failed) still exits 0 and
delivers what it got -- each object's "kind" tells you which is which.
"""

_PROVIDERS = f"""\
!! providers -- the fallback chain

The engine walks sources top-down and returns the first answer that
carries real data.  A layer is skipped automatically when it is
cooling down from a rate limit, missing its dependency, or doesn't
support the detected carrier.

  tier 0  usps-api       USPS official APIs.  Best data, real ETAs.
                         Off until you add a free USERID
                         (usps.com/web-tools) -- see track help config
  tier 0  dhl-api        DHL Express official API.  Off until you add
                         a free key (developer.dhl.com)
  tier 1  aftership      AfterShip's signed widget API.  Keyless,
                         2,000+ carriers, clean JSON, ETAs.  Roughly
                         one fresh query per number/IP per window,
                         then HTTP 402 -> 15-minute cooldown
  tier 2  packageradar   PackageRadar.  Keyless, very scrape-tolerant,
                         carrier auto-detected server-side.  Gives
                         wall-clock checkpoints (tz_naive: true)
  tier 3  browser        Playwright + real Chromium loading the
                         carrier's own tracking page.  Last resort
                         only; needs the `browser` extra and works
                         best from a residential IP

!! rate-limit memory
A rate-limited source sets a per-number cooldown; the engine skips it
silently next call and falls through the chain.  `track refresh
<number>` bypasses the cache AND clears those cooldowns for one live
query.

`track providers` lists the chain as configured on this machine.
~~ research archive with endpoints + quirks: docs/PROVIDERS.md
"""

_CACHE = """\
!! cache -- five minutes of politeness

  where   $XDG_CACHE_HOME/track  (default ~/.cache/track)
            results/<sha1>.json   last good result per number
            cooldowns.json        per-source rate-limit memory

  - a fresh result is stored for ttl_seconds (default 300) and
    re-served instantly on re-runs -- zero network, ~0.1 s
  - sources that rate-limit you set a per-number cooldown (AfterShip's
    402 -> ~15 min); cooled-down sources are skipped automatically
  - track refresh <number>   one live query: bypasses the cache and
    clears cooldowns for that number
  - TRACK_TTL=60 track <number>   shrink the TTL via environment
  - start over:  rm -rf ~/.cache/track

Polling loops should just call `track <number>` and let the cache
decide.  `refresh` is for when you know something changed.
"""

_CARRIERS = f"""\
!! carriers -- auto-detection by number shape

  USPS    20-22 digits starting 94/93/92/95/82, optionally prefixed
          with 420 + 5-digit ZIP          {EX_USPS}
  UPS     1Z + 16 alphanumeric            {EX_UPS}
  FedEx   12 digits, 15 digits, or 96 + 18-20 digits   {EX_FEDEX}
  DHL     10 digits or JJD/JVGL/JD...     {EX_DHL}
  other   handed to the aggregators, which detect the carrier
          server-side -- unknown never blocks anything

Input is forgiving: spaces and dashes are stripped and lowercase is
upper-cased before detection.  Detection is deliberately conservative
-- "unknown" is preferred over a wrong guess.
"""

_CONFIG = f"""\
!! config -- everything optional

None of this is required: the keyless chain works out of the box.
Official keys only upgrade data quality, and are tried first.

  file    $XDG_CONFIG_HOME/track/config.json  (default ~/.config/track)
  starter track config  prints a documented example

  {{
    "ttl_seconds": 300,
    "allow_browser": true,
    "usps":  {{ "user_id": "YOUR-FREE-USPS-USERID" }},
    "dhl":   {{ "api_key": "YOUR-FREE-DHL-KEY" }},
    "fedex": {{ "api_key": "...", "api_secret": "..." }}
  }}

!! environment variables (override the file; handy for CI/agents)
  TRACK_TTL                  cache seconds (default 300)
  TRACK_NO_BROWSER           any value -> never invoke playwright
  TRACK_USPS_USERID          legacy XML API key (free, ~2 min signup)
  TRACK_USPS_CLIENT_ID       api.usps.com OAuth2 (new platform)
  TRACK_USPS_CLIENT_SECRET
  TRACK_DHL_API_KEY          developer.dhl.com free key
  TRACK_FEDEX_API_KEY        fedex developer account
  TRACK_FEDEX_API_SECRET

Highest-value upgrade: the free USPS USERID -- official data with real
ETAs for USPS numbers.  The engine prefers tier-0 sources when keys
are present, automatically.
~~ details: docs/PROVIDERS.md
"""

_ADVANCED = f"""\
!! advanced -- power flags (never required)

Daily use needs none of these; `track <number>` does the right thing.
They exist for scripts and debugging.

!! output
  --json            force JSON even in a terminal
  --pretty          force the card even when piped
  --no-color        plain text (the NO_COLOR env var is honored too)

!! diagnostics
  -v, --verbose     per-source attempt log (embedded in JSON too)
  -p, --provider N  query only this source: aftership, packageradar,
                    usps-api, dhl-api, browser
  -t, --timeout S   per-source network timeout (default 20 s)

!! cache control
  --refresh         alias of `track refresh <number>`
  --no-cache        live query AND don't store the fresh result

!! chain control
  --no-browser      never invoke playwright (TRACK_NO_BROWSER too)

!! muscle-memory aliases
  --help -> help    --version -> version    --example-config -> config

~~ the JSON side of every flag: track help json
"""

_EXAMPLES = f"""\
!! examples -- copy-paste recipes

  track {EX_USPS}
  track {EX_UPS} {EX_FEDEX}       two packages, one call
  track 9405 5000 0000 0000 0000 000         spaces are fine

!! scripts (JSON when piped -- no flags needed)
  track {EX_USPS} | jq .status
  track {EX_USPS} | jq -r '.events[-1].description'
  track {EX_USPS} {EX_UPS} | jq '.[] | select(.estimated_delivery != null)'

!! poll politely (cache + cooldowns make this safe)
  watch -n 60 -c "track {EX_USPS}"

!! status-change detection
  prev=$(track {EX_USPS} | jq -r .status)
  track refresh {EX_USPS} | jq -e '.status == "delivered"' \\
    && notify-send "Package delivered"

!! python
  import json, subprocess
  proc = subprocess.run(["track", "{EX_USPS}"],
                        capture_output=True, text=True)
  data = json.loads(proc.stdout)      # proc.returncode: 0/1/2/3

!! debug a suspicious answer (see what every source said)
  track -v {EX_USPS}
  # attempts: [ {{provider: aftership, ok: false, note: "rate_limited: ..."}} ]
"""

#: ordered registry -- slug -> page body ("{{version}}" templated later)
TOPICS: dict[str, str] = {
    "json": _JSON,
    "exit-codes": _EXIT_CODES,
    "providers": _PROVIDERS,
    "cache": _CACHE,
    "carriers": _CARRIERS,
    "config": _CONFIG,
    "advanced": _ADVANCED,
    "examples": _EXAMPLES,
}

#: one-line summaries used by the main page and the topics listing
TOPIC_BLURBS: dict[str, str] = {
    "json": "the exact JSON contract for agents and scripts",
    "exit-codes": "every code and what to do about it",
    "providers": "the fallback chain: who's who, rate limits",
    "cache": "the 5-minute cache, cooldowns, refreshing",
    "carriers": "which number shapes map to which carrier",
    "config": "config.json + TRACK_* environment variables",
    "advanced": "power flags (one source only, verbose, timeouts...)",
    "examples": "copy-paste recipes",
}


def main_help(version: str, color: bool) -> str:
    """The `track help` front page."""
    return _render(_MAIN.replace("{version}", version), color)


def topics_page(color: bool) -> str:
    """The `track help topics` listing."""
    lines = [f"!! {PROG} help topics"]
    lines += [f"  {slug:<12} {TOPIC_BLURBS[slug]}" for slug in TOPICS]
    lines.append("")
    lines.append(f"~~ open one: {PROG} help <topic>")
    return _render("\n".join(lines), color)


def render_topic(topic: str, color: bool) -> Optional[str]:
    """One topic page, or None when the slug is unknown."""
    body = TOPICS.get(topic)
    if body is None:
        return None
    return _render(body, color)
