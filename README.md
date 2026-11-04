# track

A free, fast, zero-configuration package tracking CLI. Point it at a
tracking number; it auto-detects the carrier, walks a fallback chain of
free data sources, and prints a clean terminal card — or machine-readable
JSON when an agent (or a pipe) is on the other end. No flags, no keys,
no setup:

```bash
track 9405500000000000000000
```

```
 USPS · 9405500000000000000000
────────────────────────────────────────────────────────────────────
 Status   ● In transit  —  The package is in transit for 3 days.
 ETA      not published by source
 Source   source: packageradar (cached Nov 04, 2026 · 5:54 pm)
────────────────────────────────────────────────────────────────────
 Timeline (3 events, newest first)
  │ Nov 03, 2026 · 6:21 pm
  ● In transit - Departed waypoint
  │ Springfield Il Distribution Center
  │ Nov 03, 2026 · 5:06 pm
  ● Package accepted
  ...
```

## Why it exists

* **Free.** No accounts, no API keys, no paid tiers. The default chain
  uses two keyless sources discovered and validated against live
  endpoints (see [docs/PROVIDERS.md](docs/PROVIDERS.md) for the full
  research story). Optional *free* official keys simply upgrade data
  quality.
* **Fast and light.** Pure Python 3.10+ standard library — zero runtime
  dependencies. A cached answer renders in ~0.1 s; a live query is
  typically 1–4 s. No daemons, no background jobs.
* **Flag-free by design.** `track <number>` is the whole interface.
  Output adapts on its own (card on a TTY, JSON in a pipe), the cache
  just works, and the manual is one word away: `track help`. A few
  power flags exist for scripts — documented in `track help advanced`,
  never required.
* **Agent-friendly.** Stable, schema-documented JSON when piped;
  deterministic exit codes; a 5-minute result cache and per-source
  rate-limit cooldowns make polling loops safe. See
  [docs/AGENTS.md](docs/AGENTS.md).
* **Resilient by design.** Four layers of fallback — if one source
  rate-limits, blocks, or changes its HTML, the next one takes over
  automatically and the attempt log (`track -v <number>`) tells you
  exactly what happened.

## The fallback chain

```
 0. official APIs (optional, free keys)   usps-api, dhl-api
      │  best data quality, only when you configured a key
      ▼
 1. AfterShip signed widget API           keyless, 2,000+ carriers
      │  clean JSON, ETA, timezone-correct stamps
      ▼   (~1 fresh query per number/IP per window; then 402)
 2. PackageRadar                          keyless, very scrape-tolerant
      │  server-rendered checkpoints, carrier auto-detected
      ▼
 3. Playwright headless browser           last resort only
         real Chromium loads the carrier's own tracking page
```

Every layer is skipped automatically when it is cooling down from a
rate limit, when its dependency (a key, playwright) is missing, or when
it does not support the detected carrier. The winner's result is cached
for 5 minutes. `track providers` shows the chain as configured on your
machine.

| Carrier | Auto-detected by |
|---|---|
| USPS | `9400…`, `9205…`, `9303…`, `420+ZIP…` (IMpb 20–22 digit) |
| UPS | `1Z` + 16 alphanumeric |
| FedEx | 12-digit, 15-digit, `96…` 20–22 digit |
| DHL | 10-digit, `JJD…`/`JD…`/`JVGL…` |
| anything else | the aggregators detect it server-side |

## Install

Zero-install (recommended):

```bash
git clone https://github.com/dustindog101/track.git && cd track
./track 9405500000000000000000        # just works
sudo ln -sf "$(pwd)/track" /usr/local/bin/track   # or ~/bin, ~/.local/bin
```

Or as a proper package:

```bash
pipx install .            # installs the `track` entry point
# optional extras:
pipx install "track/[browser]" && playwright install chromium
```

## Usage

One command is the whole interface. `track help` is the manual — in
the program, always up to date — with deep-dive topics
(`track help json`, `track help providers`, …).

```bash
track 9405500000000000000000        # a USPS number
track 1Z999AA10123456784            # UPS — carrier auto-detected
track 9405… 1Z… 9622…               # multiple numbers at once

track help                          # the manual
track help json                     # the exact JSON contract
track refresh <number>              # ignore cache, live query
track providers                     # show the source chain + status
track config                        # print a documented config.json
track version                       # print the version

track <number> | jq .status         # JSON when piped — no flags needed
track -v <number>                   # per-source attempt log (power flag)
```

The tracking numbers in this README are synthetic examples — swap in
your own.

### Configuration (all optional)

`~/.config/track/config.json` (or `$XDG_CONFIG_HOME`), also readable
from environment variables — `track config` prints a documented
starter, `track help config` explains every field:

```json
{
  "ttl_seconds": 300,
  "allow_browser": true,
  "usps":  { "user_id": "YOUR-FREE-USPS-USERID" },
  "dhl":   { "api_key": "YOUR-FREE-DHL-KEY" }
}
```

With a USPS Web Tools `USERID` (free, ~2 minutes to register), tier 0
goes live and USPS numbers get official data with real ETAs — the single
highest-value upgrade. Same story for DHL via developer.dhl.com.

## Documentation

| File | Contents |
|---|---|
| [docs/AGENTS.md](docs/AGENTS.md) | JSON contract, exit codes, polling recipes for AI agents & scripts |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | engine design, provider contract, how to add a source |
| [docs/PROVIDERS.md](docs/PROVIDERS.md) | every source we probed: endpoints, quirks, rate limits, what worked and what didn't |
| [tests/run_tests.py](tests/run_tests.py) | dependency-free test suite (`python3 tests/run_tests.py`) |

…plus the built-in manual: `track help` and its topics.

## Troubleshooting

* **"rate_limited" in the attempt log** — free sources throttle
  aggressively per IP. The engine backs off automatically (per-number
  cooldowns). Wait, or plug in an official key for that carrier.
* **USPS shows nothing from a server** — USPS/UPS/FedEx consumer pages
  sit behind Akamai bot protection that blocks datacenter IPs outright
  (even headless browsers). The keyless chain does not depend on those
  pages; a residential IP fixes the tier-3 browser path.
* **A source suddenly returns parse errors** — their markup changed.
  See docs/PROVIDERS.md "when things break" for the one-file fix.

## License

MIT — see [LICENSE](LICENSE).
