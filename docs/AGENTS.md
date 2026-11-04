# Using `track` from agents and scripts

`track` is designed so an AI agent (or a cron job, or any script) can
call it with zero flags and parse the result deterministically.

## The one rule

**When stdout is not a terminal, output is JSON.** In a terminal it's a
pretty card. No mode switching, no flags.

```bash
track 9405500000000000000000 | jq .
```

```json
{
  "tracking_number": "9405500000000000000000",
  "carrier": "usps",
  "status": "in_transit",
  "status_human": "The package is in transit for 3 days.",
  "estimated_delivery": null,
  "events": [
    {
      "timestamp": "2026-11-02T11:48:00",
      "location": "Springfield, IL",
      "description": "Package information received",
      "tz_naive": true
    },
    {
      "timestamp": "2026-11-03T17:06:00",
      "location": "Springfield, IL",
      "description": "Package accepted",
      "tz_naive": true
    },
    {
      "timestamp": "2026-11-03T18:21:00",
      "location": "Springfield Il Distribution Center",
      "description": "In transit - Departed waypoint",
      "tz_naive": true
    }
  ],
  "provider": "packageradar",
  "provider_tier": 2,
  "fetched_at": "2026-11-04T17:54:00+00:00",
  "notes": ["timestamps are carrier-local (no reliable timezone)"],
  "kind": "success",
  "cached": false
}
```

## Fields

| Field | Meaning |
|---|---|
| `status` | normalized lifecycle enum: `unknown`, `pre_transit`, `in_transit`, `out_for_delivery`, `delivered`, `exception`, `expired`, `returned` — match on this, never on wording |
| `status_human` | the source's own phrase (display only) |
| `estimated_delivery` | ISO-8601 when a source publishes it; otherwise `null` (many keyless sources don't) |
| `events` | oldest → newest; `tz_naive: true` warns that timestamps are carrier-local wall-clock times without a reliable offset |
| `provider` / `provider_tier` | which source answered (0=official key, 1–2=keyless, 3=browser) |
| `cached` / `cached_at` | true when served from the 5-minute disk cache |
| `kind` | `success` \| `not_found` \| `failed` — the authoritative outcome. A `not_found` may still carry a thin object (status `null`, empty events) when a source has a record but no carrier scans yet |
| `attempts` | only with `-v`/`--verbose`; per-source decision log |
| `notes` | data-quality caveats from the winning source |

Multiple numbers produce a JSON **array** of these objects.

## Interface surface (all of it)

| Command | Purpose |
|---|---|
| `track <number> [number…]` | track; JSON when piped, card on a TTY |
| `track refresh <number>…` | live query: bypasses the cache and clears per-number cooldowns |
| `track help [topic]` | the manual; topics: `json`, `exit-codes`, `providers`, `cache`, `carriers`, `config`, `advanced`, `examples` |
| `track providers` | source chain + configured status (JSON when piped) |
| `track config` | documented `config.json` starter |
| `track version` | version line |

## Exit codes (stable contract)

| Code | Meaning | Agent action |
|---|---|---|
| 0 | `success` — result present (even "label created") | parse JSON |
| 1 | `not_found` — no source returned tracking data (unknown number, or a label no carrier has scanned yet; a thin object may be present) | re-check the number; retry later if brand-new |
| 2 | `failed` — every eligible source errored (network/blocking/rate-limit) | retry with backoff, or `track refresh <number>` later |
| 3 | usage error (bad arguments, unknown help topic) | fix the invocation |

## Recipes

Poll politely (cache + cooldowns make this safe):

```bash
watch -n 60 -c "track 9405500000000000000000"
```

Status change detection:

```bash
prev=$(track 9405500000000000000000 | jq -r .status)
track refresh 9405500000000000000000 | jq -e '.status == "delivered"' \
  && notify-send "Package delivered"
```

Deliveries due today:

```bash
track 9405… 1Z… 9622… | jq '.[] | select(.estimated_delivery != null)'
```

Debug a suspicious answer (see what every source said):

```bash
track -v 9405500000000000000000
# attempts: [ {provider: aftership, ok: false, note: "rate_limited: …"}, … ]
```

Python integration (note: no `--json` needed — a pipe implies JSON):

```python
import json, subprocess

def track(number: str) -> dict:
    proc = subprocess.run(
        ["track", number], capture_output=True, text=True)
    return json.loads(proc.stdout)   # exit code: proc.returncode
```

## Rate-limit semantics (why polling is safe)

* Results are cached for 5 minutes (`ttl_seconds`, `TRACK_TTL`) —
  repeated calls within the TTL touch no network.
* Sources that rate-limit set a **per-number cooldown** (e.g. AfterShip
  402 → 15 min). The engine skips cooled-down sources automatically and
  falls through the chain; the attempt log (`-v`) shows the skip.
* `track refresh <number>` clears cooldowns for that number and
  bypasses the cache for one live shot. The advanced `--no-cache` flag
  additionally refuses to store the fresh result.
* For tight loops, prefer reading the cache (plain call) over
  `refresh`; the free sources tolerate roughly one fresh query per
  number per few minutes per IP.
* `TRACK_NO_BROWSER=1` (or the advanced `--no-browser` flag) guarantees
  playwright is never invoked — useful on headless CI boxes where it
  can't help anyway.

## When the answer is thin

Keyless sources vary in what they publish. If `estimated_delivery` is
`null`, the keyless tiers simply don't carry an ETA for that carrier —
configuring the free USPS `USERID` (see docs/PROVIDERS.md) upgrades
USPS numbers to official data with real ETAs, and `track` will
automatically prefer that source.
