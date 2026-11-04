# Architecture

## Module map

```
src/trackcli/
├── cli.py          argument parsing, output routing, exit codes
├── engine.py       the fallback-chain orchestrator (the brain)
├── models.py       Shipment / Event / ProviderError / TrackOutcome
├── status.py       provider wording -> normalized Status enum
├── carriers.py     tracking number -> carrier regex detection
├── cache.py        TTL result cache + per-source cooldown store
├── config.py       optional keys & tunables (env / config.json)
├── ui.py           pretty ANSI renderer (zero dependencies)
└── providers/
    ├── base.py     Provider ABC + shared HTTP helpers
    ├── aftership.py      tier 1 — signed keyless JSON API
    ├── packageradar.py   tier 2 — HTML-fragment scraper
    ├── usps_api.py       tier 0 — official USPS (XML + OAuth2)
    ├── dhl_api.py        tier 0 — official DHL
    └── browser.py        tier 3 — Playwright last resort
```

Import graph is a strict tree: `providers/*` and `engine.py` depend on
`models/cache/config/status/carriers`; `cli.py` depends on everything;
nothing depends on `cli.py`. Adding a source touches exactly one new
file plus one line in `providers/__init__.py`.

## Request lifecycle

```
track <number>
  └─ cli.main()
       ├─ carriers.clean + Engine.track(number, refresh)
       │    ├─ cache.get() ── fresh? ───────────────► return cached
       │    ├─ carriers.detect(number)
       │    └─ for provider in chain:
       │         ├─ supports(number, carrier)?     else skip
       │         ├─ in cooldown?                   else skip (logged)
       │         ├─ provider.fetch(...) -> Shipment
       │         │    └─ raise ProviderError(kind) on any failure
       │         ├─ normalize status (status.best_status)
       │         ├─ shipment.has_data? ── yes ────► cache.put, return
       │         └─ no ── keep as empty-fallback, continue
       └─ render: isatty? ui.render : JSON (to_dict)
```

Error routing is the heart of the design:

| ProviderError kind | Engine behavior |
|---|---|
| `NOT_FOUND` | soft — keep walking the chain; if *every* source says unknown → `kind="not_found"` (exit 1) |
| `RATE_LIMITED` | set per-number cooldown, keep walking |
| `BLOCKED` | short cooldown, keep walking |
| `CONFIG` | missing key/dependency — keep walking |
| `NETWORK` / `PARSE` | keep walking |
| unexpected exception | caught, logged as an attempt, chain survives |

The first source whose answer carries data wins (has_data = events or
ETA or a non-unknown status). A source that answers "nothing scanned
yet" is kept as a *fallback*: it becomes the result only if no richer
source succeeds, and is surfaced as a success with empty events —
honest, because "the label exists but the carrier hasn't scanned it"
is real information an agent can act on.

## The provider contract

```python
class Provider(ABC):
    name: str          # slug used in output + cooldown keys
    tier: int          # 0 official-key, 1-2 keyless, 3 browser

    def supports(self, number: str, carrier: str) -> bool: ...
    @abstractmethod
    def fetch(self, number: str, carrier: str) -> Shipment: ...
```

Rules a provider must follow:

1. **Communicate only via `Shipment` or `ProviderError`.** Never return
   `None`, never swallow errors — the engine routes by error *kind*.
2. **Events oldest → newest.** Most carriers render newest-first;
   reverse before returning (the UI reverses again for display).
3. **Timestamps ISO-8601** when the source provides a machine format;
   set `Event.tz_naive=True` when offsets are unreliable (see
   PackageRadar).
4. **Be a good citizen:** one logical query per `fetch`; let the cache
   and cooldowns absorb polling. Providers must not implement their own
   retries — the engine + user-driven `track refresh` own that.
5. **Timeouts from `self.config.timeout`** — one knob for everything.

`base.Provider` ships helpers so a new provider is mostly parsing code:
`_http()` (keep-alive capable), `_parse_json()`, `_browser_headers()`,
`_event()`, `_not_found()`.

### Adding a provider (checklist)

1. Create `providers/mysource.py`, subclass `Provider`.
2. Implement `fetch()`; raise `ProviderError(kind, ...)` on failure —
   pick the kind by *cause*, not by HTTP status.
3. If it needs a key, set `tier = 0`, gate `supports()` on
   `self.config.has_<x>`, and add the field to `config.py`.
4. Register it in `PROVIDER_CLASSES` (position = priority).
5. Add a test to `tests/run_tests.py` (parser tests use captured
   fixtures under `tests/fixtures/` — no network in CI).
6. Document the endpoint in `docs/PROVIDERS.md`.

## Design decisions worth knowing

* **AfterShip's two signatures share one TLS connection.** The
  app-level signature embeds the *server-visible client IP* from the
  ticket call; from behind rotating NAT, two separate connections can
  present different IPs and the batch call fails signature validation.
  Hence `http.client.HTTPSConnection` keep-alive inside one `fetch`.
* **Cooldowns are per (provider, number), not global.** A 402 from
  AfterShip for number A must not stop AfterShip from serving number B.
  Global cooldowns only exist implicitly via the short cache.
* **The cache stores the JSON dialect, not pickles**, and `Cache.get`
  rehydrates enums/dataclasses explicitly. Stale or corrupt cache files
  degrade to a miss, never an error.
* **Status normalization lives in the engine**, not in providers, so
  every source is graded on one ladder (`status.py`) and a provider
  cannot invent lifecycle states.
* **JSON is the default when piped.** Agents and `cron` jobs get stable
  machine output without flags; humans get the card. The advanced
  `--json`/`--pretty` flags override detection; `NO_COLOR` and
  `--no-color` strip ANSI.
* **The browser is tier 3 and lazy.** `playwright` is imported only
  inside `BrowserProvider.fetch`, so the 99% path stays dependency-free
  and starts in milliseconds.

## Testing

`python3 tests/run_tests.py` — a zero-dependency runner covering
detection, normalization, cache/cooldown roundtrips, signature shapes,
the PackageRadar parser against a captured real fragment, and the
engine's fallback semantics using in-memory fake providers. Networked
behavior is exercised manually against the live sources (the sandbox
and free endpoints make reliable CI network tests impossible; the
fixtures freeze the parsers instead).
