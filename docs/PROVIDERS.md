# Providers — the full research dossier

Every source we probed while building `track`, what worked, what
didn't, and the exact endpoint details the implementations rely on.
All findings verified with live requests (November 2026). Treat
keyless endpoints as a cat-and-mouse game: this file is the map for
the next round.

## Executive summary

| Source | Keyless? | Verdict | Role in `track` |
|---|---|---|---|
| AfterShip widget API | ✅ | **works** (≈1 query/number/IP/window) | tier 1 |
| PackageRadar | ✅ | **works** (captcha only under abuse) | tier 2 |
| USPS legacy XML API | free `USERID` | **works**, datacenter-friendly | tier 0 (optional key) |
| USPS api.usps.com (OAuth2) | free app creds | alive, documented | tier 0 (optional key) |
| DHL `api-eu.dhl.com` | free key | alive, clean JSON | tier 0 (optional key) |
| 17TRACK REST | ❌ | fingerprint-gated (`meta:-4 "notN"`) | — (browser passes it) |
| ParcelsApp | ❌ | paid-only now ($19+/mo) | — |
| UPS `GetStatus` | ❌ | Akamai block page (even with cookies) | — |
| FedEx `trackingCal/track` | ❌ | **removed** (404 behind Akamai) | — |
| USPS/UPS/FedEx consumer web | ❌ | Akamai / Radware JS interstitial | tier 3 (browser) |
| OrderTracker / TrackTry / TrackingMore / PackageTrackr | ❌ | Turnstile / reCAPTCHA / Cloudflare 403 | — |

## Tier 1 — AfterShip widget API (`providers/aftership.py`)

AfterShip's public tracking widget (`track.aftership.com/<num>`) talks
to an internal batch API guarded by two HMAC signatures whose keys ship
in its public JS bundle (`track-widgets.aftership.com/static/main.js`).
No account, no API key, 2,000+ carriers, carrier auto-detection,
timezone-correct ISO timestamps, ETA field.

**Flow** (must share ONE TLS connection — the app signature embeds the
server-visible client IP observed during the ticket call):

1. `GET https://track.aftership.com/api/v2/trackings-ticket-info`
   (gateway-signed too) → `{"timestamp": <ms>, "ip": "<client ip>"}`
2. `app_sig = HMAC-SHA256_hex(APP_KEY, "<num>-<ip>-<UTC date>-<APP_KEY>")`
3. compact JSON body `{"directTrackings":[{"tracking_number":N,
   "additionalFields":{}}],"slugs":[],"translateTo":"en"}`
4. gateway sig (Kong hmac-auth): canonical string
   `POST <path> track.aftership.com\nam-sign-time: <unix>\nam-sign-version: v1\ndigest: <b64(md5(body))>`
   → `b64(HMAC-SHA256(GW_KEY, canonical))` into the `authorization` header
5. `POST /api/v2/direct-trackings/batch` with both signatures

**Response:** `data.direct_trackings[0].tracking.{checkpoints[],
latest_status, estimated_delivery_date}`, `.slug` (detected carrier),
`.detected_slugs` (ranked guesses). Checkpoints newest-first.

**Quirks:**
* ≈one fresh query per (number, client IP) per window → HTTP 402
  `failed to validate recaptcha` afterwards. `track` maps 402 to a
  15-minute per-number cooldown. First-ever query of a number may
  return 201 with empty checkpoints while their crawler warms up.
* Compact JSON matters: the `digest` is MD5 over the exact body bytes
  their JS would send (`json.dumps(..., separators=(",", ":"))`).
* If the widget keys rotate, 40101 "Unauthorized" appears — update
  `_APP_KEY`/`_GW_KEY` in `providers/aftership.py` from a fresh bundle.

## Tier 2 — PackageRadar (`providers/packageradar.py`)

Server-rendered tracking for 120+ carriers. No keys, no CSRF tokens,
no cookies *required* (send a jar anyway), no fingerprinting — the most
scrape-friendly source we found.

**Flow:**
1. `POST https://packageradar.com/form` with
   `tracking_form[tracking_number]=<num>` → 302 to
   `/courier/<detected-carrier>/tracking/<num>` (free carrier detection!)
2. Landing HTML: `#fragment-checkpoints > ul.checkpoints > li` rows with
   `time[datetime]` (naive carrier-local clock stamped with a bogus
   +03:00 — strip it, mark `tz_naive`), `strong.checkpoint-status`,
   `em.text-muted` (location), `i.icon.*` status hints; `data-tid` attribute
3. Poll `GET /ajax/track/<tid>/check` with
   `X-Requested-With: XMLHttpRequest` → `{"result":"waiting"}` a few
   times, then `{"result":"ok","data":"<html fragment>"}`

**Quirks:**
* A bogus number's landing page has **no `data-tid` and no
  checkpoints** — that's the not-found signal. A captcha interstitial
  (only under abusive volume) looks identical except the word
  "captcha" → classified `RATE_LIMITED` with a 10-minute cooldown.
* No estimated-delivery field on most carriers.
* Without a cookie jar they occasionally serve a session-less
  interstitial that looks like "no record" — always send cookies.

## Tier 0 — official free-key APIs (`providers/usps_api.py`, `providers/dhl_api.py`)

These require a free registration but reward you with official data
(real ETAs, exact scan locations) and no bot walls.

### USPS legacy Web Tools (simplest — recommended)

* Register: <https://www.usps.com/business/web-tools-apis> → USERID by email.
* `GET https://secure.shippingapis.com/ShippingAPI.dll?API=TrackV2&XML=<TrackRequest USERID="..."><TrackID ID="<num>"/></TrackRequest>`
* XML: `TrackInfo[@Status, @ExpectedDeliveryDate]`, `TrackSummary`,
  `TrackDetail` (newest-first).
* Verified reachable from datacenter IPs (the API host is exempt from
  the Akamai wall that guards tools.usps.com). Note: USPS has been
  deprecating Web Tools in favor of the new platform — if your USERID
  stops working, use the OAuth2 route below.

### USPS new platform (developers.usps.com)

1. Create an app → `client_id` + `client_secret`.
2. `POST https://api.usps.com/oauth2/v3/token`
   `grant_type=client_credentials` with Basic auth → `access_token`.
3. `GET https://api.usps.com/tracking/v3/tracking/<num>` with Bearer →
   `status`, `expectedDeliveryDate`, `eventHistory[]`.

### DHL

* Register: <https://developer.dhl.com> (free, ~1 minute) → API key.
* `GET https://api-eu.dhl.com/track/shipments?trackingNumber=<num>`
  header `DHL-API-Key: <key>`.
* `shipments[0].status.status`, `estimatedTimeOfDelivery`, `events[]`
  (address locality + country). Clean JSON, no bot wall (keyless
  probes answer 401 JSON, not a challenge page).

### FedEx (not implemented — slot available)

`api.fedex.com/track/v3` exists behind the same OAuth2 app pattern;
the developer-portal approval flow is heavier and the aggregator chain
already covers FedEx well. To add it: copy the USPS OAuth2 shape in
`usps_api.py`, gate `supports()` on `carrier == "fedex"`, add config
fields `fedex.api_key`/`fedex.api_secret` (already stubbed in
`config.py`).

## Tier 3 — headless browser (`providers/browser.py`)

Only invoked when every API source failed. Opens the carrier's own
tracking page in headless Chromium (playwright, lazy-imported), waits
for XHRs, and extracts status/ETA/events via DOM-agnostic text
patterns plus table-row parsing. Non-USPS numbers without a dedicated
page fall back to 17TRACK's web UI, which a real browser passes thanks
to the fingerprint sign computed in-page.

**Reality check (verified):** from datacenter IPs, Akamai serves
"Access Denied" even to headless Chromium on usps.com/ups.com. From
residential connections this path works essentially always. Keep it
last in the chain and don't rely on it for CI.

## The ones that didn't make it (and why)

* **17TRACK REST** — the documented-by-libraries endpoint
  (`t.17track.net/restapi/track`, used by py17track/seventeentrack)
  now answers `{"meta":{"code":-4}}` to *everything* keyless: the
  gate is a validated browser-fingerprint `sign` field generated by
  obfuscated in-page JS. Both old and new endpoints, plus
  `/track/restapi`, behave identically. Libraries have been dead
  since ~2022 for exactly this reason.
* **ParcelsApp** — `POST /api/v3/shipments/tracking` requires an
  `apiKey`; no free tier anymore (plans from $19/mo). The web XHR
  (`/api/v2/parcels`) uses JS-obfuscated params → `{"error":"NO_TRACKER"}`.
* **UPS `GetStatus`** — returns HTTP 200 wrapping an Akamai "Access
  Denied" HTML block page (a tempting false positive; check the body).
  Cookie warmup just redirects to `error.page`.
* **FedEx `trackingCal/track`** — removed entirely (404 page behind
  Akamai); `/api/track/v3` is Akamai-blocked keyless.
* **m.usps.com** — alive but serves a Radware/Shape JS interstitial
  (`ISTL-RESPONSE-URL` markers), zero tracking content without JS.
* **OrderTracker** — Cloudflare Turnstile on the track action;
  **PackageTrackr** — full Cloudflare 403; **TrackTry/TrackingMore**
  — sister sites with captchas and no keyless JSON.

## When things break

| Symptom | Likely cause | Fix |
|---|---|---|
| AfterShip 40101 | widget keys rotated | re-extract `_APP_KEY`/`_GW_KEY` from the current `static/main.js` |
| AfterShip 402 everywhere | IP burned | wait, or run from a different network; cooldowns recover automatically |
| PackageRadar captcha page | IP throttled | automatic 10-min cooldown; slow your polling |
| PackageRadar parser misses | markup changed | update regexes against a fresh landing page; fixture lives in `tests/fixtures/` |
| USPS XML auth error | Web Tools retired your USERID | switch to the OAuth2 route in the same file |
| Browser "Access Denied" | datacenter IP | expected; only tier-3 — plug in an official key instead |
