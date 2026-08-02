# EinVault for Home Assistant

[![Validate](https://github.com/iotemylabs/homeassistant-einvault/actions/workflows/validate.yml/badge.svg)](https://github.com/iotemylabs/homeassistant-einvault/actions/workflows/validate.yml)
[![hacs](https://img.shields.io/badge/HACS-custom-41BDF5.svg)](https://hacs.xyz)

Home Assistant integration for a self-hosted [EinVault](https://github.com/davefatkin/EinVault)
instance — companion (pet) health and care tracking.

> **Status: phase 5 of 6.** All entities, actions, calendars, and diagnostics are in place.
> Remaining: snapshot tests and final hardening (phase 6). See
> [docs/release-checklist.md](docs/release-checklist.md) for the pre-release steps that live
> outside this repository.

## Entities

One device per companion, plus a service device for the instance itself.

| Entity | Type | Notes |
|---|---|---|
| `last_walk`, `last_meal`, `last_bathroom`, `last_play`, `last_grooming`, `last_treat` | timestamp | Newest event of that type. Attributes: `subtypes`, `duration_minutes`, `notes`. |
| `latest_weight` | weight, measurement | Unit from the companion's `weightUnit`, falling back to the unit on the entry itself since the former is nullable. |
| `due_reminders` | count | Open reminders for that companion. Attribute: how many are overdue. |
| `next_reminder` | timestamp | Soonest open reminder. Attributes: `title`, `type`, `is_recurring`. |
| `today_mood` | enum | **Opt-in.** Today's journal mood. Costs one extra API call per companion per refresh. |
| `reminder_overdue` | binary, problem | Anything past due. Attribute: the offending titles. |
| `caretaker_on_shift` | binary, occupancy | Instance-scoped. Attribute: caretaker names, resolved from the roster. |
| `Calendar` | calendar | **Opt-in.** One per companion, from the ICS feed. See below. |
| `Caretaker shifts` | calendar | **Opt-in.** Instance-scoped shift calendar. |
| Quick-log buttons | button | One per quick log. Attached to the companion's device when it targets exactly one, otherwise to the service device. |
| `api_calls_last_refresh` | diagnostic | Disabled by default. Enable it to watch the request budget. |

A companion added in EinVault gets entities on the next hourly refresh, with no reload. One
archived or removed upstream goes unavailable rather than freezing on a stale value.

---

## Read this before you install: point Home Assistant at the *local* address

EinVault rate limits **30 requests per 60 seconds keyed on client IP**, and that check runs
*before* your API token is even looked at. The address it sees is the TCP peer, so:

- **Through a reverse proxy** (Nginx Proxy Manager, Traefik, Caddy, a Cloudflare tunnel), every
  client collapses into a single shared bucket — Home Assistant, n8n, your phone, and every
  smart button competing for the same 30 requests a minute. They will intermittently rate-limit
  each other, and nothing in any UI will explain why.
- **Directly on your LAN or Docker network** (`http://192.168.1.10:7387`, or the container name
  if Home Assistant shares the network), Home Assistant *is* the peer and gets its own bucket.

**So use the local address.** It is faster, it keeps traffic off the public internet, and it
sidesteps the shared-budget problem entirely.

### Do not set `ADDRESS_HEADER`

It looks like the fix. It is not, unless *all* your traffic is proxied.

`ADDRESS_HEADER` and `XFF_DEPTH` are SvelteKit adapter-node variables that EinVault inherits but
never documents. adapter-node **throws** when the named header is absent from a request, and
EinVault catches that and falls back to the literal string `'unknown'`. A direct LAN request
carries no `X-Forwarded-For`, so setting `ADDRESS_HEADER=x-forwarded-for` silently moves Home
Assistant out of its own clean bucket into a global `'unknown'` bucket shared with every other
direct client — strictly worse, with no error surfaced anywhere.

Tracked upstream as [W-4](docs/upstream-wishlist.md).

---

## Requirements

- EinVault **1.3.0 or newer** with `API_TOKENS_ENABLED=true` (the default)
- An API token with **full** access, created under *Settings → API tokens*
- Home Assistant 2024.12 or newer

### Token access level and role

| | Result |
|---|---|
| **Full access** | Supported. Everything works. |
| **Write-only** | **Refused at setup.** Write-only tokens get `403 writeScopeReadOnly` on every read endpoint — logs, weight, reminders, shifts, users, journal — so not a single entity could be populated. The config flow fails with a clear message rather than creating a broken entry. |
| **Admin or member role** | Recommended. |
| **Caretaker role** | Limited. Reaches only assigned companions, and writes require an active shift (`noActiveShift`, `notAssigned`). |

Rotating a token in EinVault revokes the old one immediately. Home Assistant will raise a
reauth prompt; paste the new token there.

---

## Installation

### HACS (recommended)

1. HACS → Integrations → ⋮ → **Custom repositories**
2. Add `https://github.com/iotemylabs/homeassistant-einvault`, category **Integration**
3. Install **EinVault**, restart Home Assistant
4. Settings → Devices & Services → **Add Integration** → *EinVault*

### Manual

Copy `custom_components/einvault` into your Home Assistant `config/custom_components/`
directory and restart.

### Removal

Settings → Devices & Services → **EinVault** → ⋮ → **Delete**. That removes the config entry,
its devices, and its entities. If you installed through HACS, uninstall it there afterwards; for
a manual install, delete `config/custom_components/einvault` and restart.

Nothing is stored outside the config entry, and no data is written to your EinVault instance on
removal. The API token stays valid until you revoke it in EinVault under *Settings → API
tokens*.

---

## Configuration

| Field | Notes |
|---|---|
| **URL** | Base URL, no trailing path. Use the LAN address — see above. |
| **API token** | Stored in the config entry; never logged, and redacted from diagnostics. |

### Options

| Option | Default | Notes |
|---|---|---|
| Update interval | 300 s | Minimum 60 s. See the call-budget note below. |
| Daily mood sensor | off | Costs **one extra request per companion per refresh** — `GET /api/journal` reads one companion for one day and has no bulk form. Off by default for that reason. |
| Include archived companions | off | Archived companions 404 on direct lookup; their entities go unavailable rather than failing the refresh. |
| Calendar feed URL | empty | Optional. The ICS URL from *Settings → Calendar feed*. Creates calendar entities; see below. Costs nothing against the API budget. |

### Call budget

A full refresh with *N* companions costs:

| Calls | Endpoint |
|---|---|
| 1 | `/api/reminders` — no `companionId`, covers every companion at once |
| 1 | `/api/shifts` |
| *N* | `/api/logs` |
| *N* | `/api/weight` |
| *N* | `/api/journal` — **only** if the mood sensor is enabled |

`/api/companions`, `/api/quick-logs`, and `/api/users` refresh on a separate hourly timer.
With two companions that is **6 calls per refresh**, or 8 with the mood sensor on. At the
default 5-minute interval that is well inside the 30/minute allowance.

A diagnostic sensor exposes the observed per-refresh call count so you can verify this.

## Actions

| Action | Purpose |
|---|---|
| `einvault.log_activity` | Record a walk, meal, bathroom, treat, play, grooming, or other event |
| `einvault.log_weight` | Record a weight measurement |
| `einvault.log_health_event` | Record a vet visit, vaccination, medication, or procedure |
| `einvault.set_journal` | Create or update a day's journal entry |
| `einvault.complete_reminder` | Mark a reminder done (recurring ones spawn the next occurrence) |
| `einvault.skip_reminder` | Skip one occurrence of a recurring reminder |

All of them target a **companion device** and send an `Idempotency-Key`, so a retried call
replays rather than duplicating. Each refreshes the coordinator on success, so sensors update
without waiting for the next poll.

`log_activity` validates subtypes **before** sending. The server would answer a bare
`400 invalidSubtype`; a local check can say which value was wrong and what was allowed. Pairing
`leash` with `meal` fails immediately, with no HTTP request made at all.

Errors map to actionable messages rather than generic failures — `noActiveShift` explains that
a caretaker can only log during an assigned shift, `notRecurring` tells you to complete the
reminder instead of skipping it, and so on.

### Quick-log buttons

One button per quick log returned by `GET /api/quick-logs`, pressed with an **empty body** so
all configuration stays in EinVault. A press generates a fresh idempotency key; a network
failure retries **once with the same key**, so a lost response replays instead of creating a
duplicate entry.

If no buttons appear, the API is returning an empty list. A quick log is only returned when
**all three** hold:

1. it belongs to the same user whose API token you configured;
2. it is **enabled**;
3. it has **at least one companion attached**.

EinVault's *Settings → Quick logs* page lists quick logs matching only condition 1, so a quick
log can be visible there and still be invisible to the API. Its dashboard and companion pages
use the same query as the API, so those are the reliable place to check. Tracked upstream as
[W-13](docs/upstream-wishlist.md).

---

## What this integration cannot do

Documents, the Paperless proxy, all Immich endpoints, photos, avatars, and `/api/search` are
reachable only with a session cookie, not a bearer token. Nothing here scrapes HTML, simulates
a login, or reads the SQLite database, so those features are out of scope. See
[W-10](docs/upstream-wishlist.md).

EinVault emits no webhooks, so polling is the only option. That is a deliberate constraint of
the upstream API, not an oversight here.

### Calendar

Calendars are **opt-in**. Paste your personal feed URL into the integration's options:

> EinVault → Settings → Calendar feed → copy the URL ending in
> `/api/calendar/<token>/feed.ics`

That creates one calendar per companion, plus an instance-level caretaker-shift calendar.

The ICS feed is used rather than the REST API for three concrete reasons:

- **Recurrence actually works.** The feed carries `RRULE`, so a yearly reminder shows every
  future occurrence. `GET /api/reminders` returns only the current one.
- **It costs nothing against the rate limit.** The feed route authenticates on the token in its
  own path instead of going through `requireApiToken`, so it is exempt from the
  30-requests/60-seconds budget.
- **One fetch covers everything** — health events, reminders, and shifts, for all companions.

Events are attributed to companions using the feed's `CATEGORIES` property, which EinVault
emits as `<kind>,<companionName>`. Two companions sharing a name is the one case this cannot
separate; both calendars would then show both sets of events.

The feed token is **separate from your API token** and is stored in the config entry options,
redacted from diagnostics, and never logged. If the feed is rejected or malformed, the
calendars go unavailable and nothing else is affected — in particular it does not trigger a
reauth prompt, which would ask for the wrong credential.

Home Assistant's built-in
[`remote_calendar`](https://www.home-assistant.io/integrations/remote_calendar/) consumes the
same URL with zero configuration here, if you would rather not use these entities.

---

## Development

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-test.txt
pytest
ruff check custom_components tests
mypy custom_components/einvault
```

The client in `custom_components/einvault/api.py` imports nothing from `homeassistant` and is
tested in isolation. The one third-party requirement is
[`ical`](https://pypi.org/project/ical/) — the same library Home Assistant's own
`remote_calendar` integration uses — needed only to parse the calendar feed's recurrence rules.
It is not used anywhere outside `calendar.py`. Test fixtures under `tests/fixtures/` are **captured from a live instance**,
not hand-written, so they reflect real nullability — `subtypes` arrives as `null` rather than
`[]`, and an absent journal entry is `{"entry": null}` rather than a 404.

### Diagnostics

Settings → Devices & Services → EinVault → ⋮ → **Download diagnostics**.

Both credentials are redacted, along with every field that identifies a person or a place:
microchip number, vet name/phone/clinic, emergency contact name and phone, notes for the sitter,
the companion's bio and schedules, and the user roster's names. The call-budget counters,
reminder and event data, and coordinator state are kept, since that is what makes a diagnostics
download worth attaching to a bug report.

### Documentation

| File | Contents |
|---|---|
| [`docs/openapi-reference.json`](docs/openapi-reference.json) | The authoritative spec, fetched from a live instance |
| [`docs/api-ground-truth.md`](docs/api-ground-truth.md) | Verified behaviour, error catalogue, proxy analysis |
| [`docs/upstream-wishlist.md`](docs/upstream-wishlist.md) | Every workaround, written up for upstream |
| [`docs/release-checklist.md`](docs/release-checklist.md) | Steps that must happen outside this repo before release |
| [`custom_components/einvault/quality_scale.yaml`](custom_components/einvault/quality_scale.yaml) | Honest self-assessment against Home Assistant's quality scale |

### Known follow-up for Home Assistant Core

Core requires third-party API clients to live in a PyPI package rather than inside the
integration. `api.py` was written with no Home Assistant imports specifically so it can be
lifted out when that time comes. It stays inline for now because HACS does not require it and a
vendored client is easier to iterate on.

---

## Credits

EinVault is by [@davefatkin](https://github.com/davefatkin). This integration is not affiliated
with the EinVault project.
