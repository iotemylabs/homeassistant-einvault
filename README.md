# EinVault for Home Assistant

[![Validate](https://github.com/iotemylabs/homeassistant-einvault/actions/workflows/validate.yml/badge.svg)](https://github.com/iotemylabs/homeassistant-einvault/actions/workflows/validate.yml)
[![hacs](https://img.shields.io/badge/HACS-custom-41BDF5.svg)](https://hacs.xyz)

Home Assistant integration for a self-hosted [EinVault](https://github.com/davefatkin/EinVault)
instance — companion (pet) health and care tracking.

> **Status: phase 2 of 6.** Client, config flow, coordinator, companion devices, and the
> `last_*` / weight sensors are in place. Reminder and shift sensors (phase 3), quick-log
> buttons and actions (phase 4), and the calendar (phase 5) are still to come.

## Entities

One device per companion, plus a service device for the instance itself.

| Entity | Type | Notes |
|---|---|---|
| `last_walk`, `last_meal`, `last_bathroom`, `last_play`, `last_grooming`, `last_treat` | timestamp | Newest event of that type. Attributes: `subtypes`, `duration_minutes`, `notes`. |
| `latest_weight` | weight, measurement | Unit from the companion's `weightUnit`, falling back to the unit on the entry itself since the former is nullable. |
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

### Call budget

Once the coordinator lands in phase 2, a full refresh with *N* companions costs:

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

---

## What this integration cannot do

Documents, the Paperless proxy, all Immich endpoints, photos, avatars, and `/api/search` are
reachable only with a session cookie, not a bearer token. Nothing here scrapes HTML, simulates
a login, or reads the SQLite database, so those features are out of scope. See
[W-10](docs/upstream-wishlist.md).

EinVault emits no webhooks, so polling is the only option. That is a deliberate constraint of
the upstream API, not an oversight here.

### Calendar: use the built-in ICS feed

EinVault already publishes a personal, revocable ICS feed at
`/api/calendar/{token}/feed.ics` covering health events, reminders with recurrence, and shifts.
Home Assistant's built-in [`remote_calendar`](https://www.home-assistant.io/integrations/remote_calendar/)
consumes it with zero code, and it is the recommended path. A native calendar entity is planned
as a convenience only (phase 5).

---

## Development

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-test.txt
pytest
ruff check custom_components tests
mypy custom_components/einvault
```

The client in `custom_components/einvault/api.py` imports nothing from `homeassistant` and is
tested in isolation. Test fixtures under `tests/fixtures/` are **captured from a live instance**,
not hand-written, so they reflect real nullability — `subtypes` arrives as `null` rather than
`[]`, and an absent journal entry is `{"entry": null}` rather than a 404.

### Documentation

| File | Contents |
|---|---|
| [`docs/openapi-reference.json`](docs/openapi-reference.json) | The authoritative spec, fetched from a live instance |
| [`docs/api-ground-truth.md`](docs/api-ground-truth.md) | Verified behaviour, error catalogue, proxy analysis |
| [`docs/upstream-wishlist.md`](docs/upstream-wishlist.md) | Every workaround, written up for upstream |

### Known follow-up for Home Assistant Core

Core requires third-party API clients to live in a PyPI package rather than inside the
integration. `api.py` was written with no Home Assistant imports specifically so it can be
lifted out when that time comes. It stays inline for now because HACS does not require it and a
vendored client is easier to iterate on.

---

## Credits

EinVault is by [@davefatkin](https://github.com/davefatkin). This integration is not affiliated
with the EinVault project.
