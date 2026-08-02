# Upstream wishlist — `davefatkin/EinVault`

Every place `ha-einvault` had to work around a missing or awkward API capability. Each item is
written to be liftable into a GitHub issue with minimal editing.

Verified against API document `1.0.0`, instance `einvault.example.com`, 2026-08-02.

---

## W-1 — No way to read the EinVault application version

**Impact:** medium. The integration cannot report the server version in the device registry
(`sw_version`), cannot warn when an instance is older than the minimum it supports, and cannot
adapt if a future release changes behaviour.

`GET /api/health` returns `{status, timestamp, woof}` — no version. `info.version` in the
OpenAPI document is `1.0.0` and describes the API contract, not the app; it will not move when
EinVault ships 1.4.0.

**Ask:** add `version` (and ideally `apiVersion`) to the `/api/health` payload, and document
`/api/health` in the OpenAPI document — it currently exists but is absent from `paths`.

**Workaround:** `sw_version` is left unset on the service device.

---

## W-2 — `Idempotency-Key` is completely undocumented

**Impact:** medium — documentation only. The feature itself is well built.

The header is not declared as a parameter on any operation in the OpenAPI document, so a client
author working from the spec (which is the stated contract) has no way to know it exists. It is
inferable only by noticing `409 idempotencyKeyReused` in some responses and then reading the
server source.

`POST /api/logs` declares **no 409**, which reads as "this endpoint is not idempotent" — the
opposite of the truth. `src/routes/api/logs/+server.ts` wraps its handler in `withIdempotency()`
exactly like every other write endpoint. This is the single most retry-prone endpoint in the
API (it backs smart buttons), so the misleading documentation is most costly precisely where
it matters most: an author who trusts the spec will disable retries there to avoid duplicate
entries, losing reliability they actually had.

**Ask:** (a) declare `Idempotency-Key` as an optional header parameter on **every** write
operation, documenting the semantics from `src/lib/server/api-idempotency.ts` — key scoped to
`(tokenId, endpoint, key)`, body hashed with SHA-256, identical body replays the original
response *with its original status code*, different body yields `409`, entries pruned after 7
days; (b) add the missing `409` response to `POST /api/logs`.

**Workaround:** none needed — behaviour was verified in source, and the integration sends the
header on every write and retries safely, including `POST /api/logs`.

---

## W-3 — Rate limit is invisible to clients

**Impact:** high. The 30-req/60s IP bucket is the binding constraint on the whole integration,
and a client has no way to observe its own budget.

No `RateLimit-Limit` / `RateLimit-Remaining` / `RateLimit-Reset` (RFC 9331) headers are
returned, so a well-behaved client cannot pace itself — it can only guess a scan interval and
react after being refused.

Measured directly: the 31st request in a window is refused, and the `429` carries **no
`Retry-After`** either. So a client is told neither how much budget remains nor when it may
resume — it can only guess a backoff and hope. That is the difference between a client that
recovers in exactly one retry and one that either hammers a closed door or sleeps far longer
than necessary.

**Ask:** emit `RateLimit-*` headers on every `/api/*` response, and `Retry-After` on 429. The
latter is a two-line change and removes all guesswork.

**Workaround:** hardcoded 5-minute default poll, a strict sequential request queue, a
minimum-interval floor in the options flow, and a blind fixed backoff on 429.

---

## W-4 — `getClientAddress()` collapses all consumers into one bucket behind a proxy

**Impact:** high for the common self-hosted deployment. This instance sits behind Cloudflare;
many sit behind NPM, Traefik, or Caddy.

Because the IP limit is checked *before* the token is resolved, and `getClientAddress()`
returns the TCP peer when `ADDRESS_HEADER` is unset, every client arriving through the proxy
shares a single 30-req/60s allowance. The failure is silent and confusing: each client looks
correctly configured and they intermittently 429 each other.

`ADDRESS_HEADER` and `XFF_DEPTH` are adapter-node variables that EinVault inherits but never
documents — neither appears in `.env.example`. That leaves operators with no supported way to
fix the collapse, and the obvious guess is actively harmful:

- adapter-node **throws** if `ADDRESS_HEADER` is set and the header is absent from a request,
  and again if `XFF_DEPTH` exceeds the number of addresses present.
- `requireApiToken` catches that throw and falls back to the literal string `'unknown'`.
- So setting `ADDRESS_HEADER=x-forwarded-for` silently moves every **direct**, non-proxied
  caller (the recommended deployment for a LAN Home Assistant) into one shared `'unknown'`
  bucket — strictly worse than the default, with no error surfaced to anyone.

**Ask:** (a) document `ADDRESS_HEADER` / `XFF_DEPTH` in `.env.example` with an explicit warning
that enabling them breaks direct LAN access unless all traffic is proxied; (b) better, key the
pre-auth limit on a hash of the bearer token when an `Authorization` header is present, falling
back to IP only for anonymous requests — this removes the shared-bucket problem entirely and is
strictly more correct, since the limit's purpose is per-consumer fairness; (c) treat the
`'unknown'` fallback as a hard error rather than a shared bucket, so misconfiguration is loud.

**Workaround:** README tells users to point Home Assistant at the container's LAN or Docker
address, where the TCP peer *is* the client and the bucket is naturally per-consumer.
`ADDRESS_HEADER` is explicitly **not** recommended.

---

## W-5 — No read endpoint for a companion's *latest* event per type

**Impact:** medium — this is the single largest driver of the integration's request budget.

The `last_walk` / `last_meal` / `last_bathroom` / `last_play` / `last_grooming` / `last_treat`
sensors need one thing: the newest event of each type. The only way to get it is
`GET /api/logs?companionId=X&limit=50` per companion, transferring up to 50 events per
companion per poll and bucketing client-side — and it is still not *correct*, because if a
companion has 50+ events since the last grooming, the newest grooming event falls outside the
window and the sensor silently goes stale.

**Ask:** either `GET /api/logs?companionId=X&latestPerType=true` returning at most one event per
type, or accept a repeatable `type` filter so a caller can ask for what it needs.

**Workaround:** `limit=50` and client-side bucketing, accepting the staleness window.

---

## W-6 — Journal has no multi-companion or "latest" read

**Impact:** medium. `GET /api/journal` is strictly one companion for one day, so the
`today_mood` sensor costs one request per companion per poll — on a 30/min shared bucket that is
material, which is why the sensor ships **disabled by default**.

**Ask:** accept `companionIds` on `GET /api/journal` (as `POST /api/logs` already does for
writes) and return an array.

**Workaround:** the mood sensor is opt-in via the options flow and documented as costing N extra
calls per refresh.

---

## W-7 — `species` is a dead field that actively misinforms

**Impact:** low functionally, high in confusion.

The field is typed `string | null` and reads `"dog"` for every companion. On this instance the
sole companion is a cat — `breed: "American Longhair"`, `bio: "Cat"`, `species: "dog"`. Any
integration author's first instinct is to use it for icons or device classes, and it will be
wrong for every non-dog companion.

**Ask:** either populate it properly (widen the enum and expose it in the companion editor), or
drop it from the API response and mark it deprecated in the OpenAPI document.

**Workaround:** never read. Icons are species-agnostic.

---

## W-8 — `Shift` carries no companion association and no user display name

**Impact:** low-medium. `Shift` is `{id, userId, startAt, endAt, notes}`. Resolving "who is on
shift" to a human-readable name requires a second `GET /api/users` call and a client-side join,
and there is no way to tell which companions a shift covers.

**Ask:** embed `userDisplayName` on `Shift`, and expose the shift↔companion assignment that the
server already uses to enforce `notAssigned`.

**Workaround:** `/api/users` is fetched on the slow hourly timer and joined client-side; the
resolved name is exposed as an attribute on the `caretaker_on_shift` binary sensor.

---

## W-11 — No way for a token to identify its own user or role

**Impact:** medium. There is no `/api/me`, so a client cannot ask "who am I and what am I?"

This matters because role determines behaviour a client must warn about *before* the user hits
it: a caretaker token reaches only assigned companions and cannot write outside an active shift
(`noActiveShift`, `notAssigned`). The integration would like to say so during setup rather than
letting the first button press fail mysteriously.

The only available signal is indirect and unreliable — `GET /api/users` is scoped by role, and
`username` is "omitted for member-scoped tokens", so the presence of `username` on any row
implies an admin-scoped token. That distinguishes admin from not-admin, and cannot separate
member from caretaker at all.

Access *level* (full vs write-only) is detectable, but only via a side effect: `GET
/api/companions` returns a reduced object for write-only tokens. It is the one read endpoint
that does not answer `403 writeScopeReadOnly`, which makes it the sole viable probe — an
undocumented property the integration now depends on.

**Ask:** add `GET /api/me` returning `{id, displayName, role, accessLevel}` for the token's own
user.

**Workaround:** scope inferred from which keys are present on the companions payload; role
inference limited to an admin/non-admin heuristic, logged rather than enforced.

---

## W-12 — `invalidSubtype` is undocumented, and upstream contradicts itself

**Impact:** low, but it cost real debugging time.

`POST /api/logs` rejects an unrecognised subtype with `400 invalidSubtype`. That code appears
nowhere in the OpenAPI document — the `400` description lists
`invalidType, noCompanions, noteTooLong, …` and stops.

Worse, the codebase reads as though the opposite is true: `parseSubtypes()` in
`src/lib/activitySubtypes.ts` is documented as keeping only allowed values and dropping invalid
ones *silently*. An integration author reading that helper concludes typos vanish without
feedback and builds defensive client-side validation for the wrong reason; the route in fact
validates and rejects first.

**Ask:** add `invalidSubtype` to the documented `400` codes for `POST /api/logs`, and clarify in
`parseSubtypes()`'s docstring that it is the *display/UI* path, not the API validation path.

**Workaround:** the integration validates type/subtype pairs locally before sending, which also
yields a better message than the server's.

---

## W-13 — A quick log with no companions is silently invisible to the API

**Impact:** medium, and it costs the user real debugging time — it cost this project some.

`createQuickLog` attaches companions conditionally:

```js
if (companionIds.length > 0) {
  tx.insert(schema.quickLogCompanions).values(...)
}
```

so a quick log saved without selecting a companion is persisted successfully. But
`listQuickLogButtons` ends with `.filter((b) => b.companionIds.length > 0)`, so that quick log
never appears in `GET /api/quick-logs` — and the response is an empty array, indistinguishable
from "this user has configured no quick logs at all".

The user sees their quick log in Settings, sees an empty list over the API, and has no signal
explaining the difference. An integration author sees `{"quickLogs": []}` and cannot tell
whether to show "none configured" or "something is misconfigured".

**Ask:** either require at least one companion when saving a quick log (a validation error at
creation time is far kinder than silent invisibility later), or surface the quick log in the API
response with its empty `companionIds` so a client can explain why it is not actionable. Same
applies to `isEnabled: false` — currently indistinguishable from absent.

**Workaround:** the README tells users a quick log needs at least one companion attached *and*
must be enabled *and* must belong to the token's own user. The integration logs an informational
message pointing at Settings → Quick logs when the list is empty.

---

## W-14 — Shifts are creatable only for caretaker users, from one well-hidden form

**Impact:** low-medium. Discoverability, mostly.

`/api/shifts` exists, is documented, and returns `200 {"shifts":[],"hasMore":false}` on most
instances — because creating a shift requires a user whose role is exactly `caretaker`
(`addShift` returns `400 userNotCaretaker` otherwise), and the only place to create one is a
form on **Admin → Users**. There is no shift page anywhere in `src/routes/`, and nothing in the
UI hints that shifts are a caretaker-only concept.

An admin-only household therefore gets a permanently empty, permanently unexplained endpoint.

**Ask:** document the caretaker prerequisite next to the shift form, and consider surfacing
shift scheduling somewhere more discoverable than the user-administration page.

**Workaround:** the integration's `caretaker_on_shift` sensor simply reads `off`, and the README
explains that shifts require a caretaker-role user.

---

## W-9 — No webhooks or push of any kind

**Impact:** architectural. Polling is the only option, which is what forces every constraint
above. Recorded for completeness rather than as a near-term ask — a webhook surface is a large
feature, and the ICS feed at `/api/calendar/{token}/feed.ics` already covers the calendar case
without polling.

**Ask (long term):** outbound webhooks on log/reminder/shift events.

**Workaround:** none. Polling is accepted as a deliberate constraint.

---

## W-10 — Documents, photos, and search are session-cookie only

**Impact:** out of scope by design, recorded so it is not re-litigated.

Documents, the Paperless proxy and thumbnails, all Immich endpoints, photos, avatars, and
`/api/search` are unreachable with a bearer token. Any HA feature depending on them (a companion
photo on the device page, for instance) is blocked upstream.

**Ask:** extend bearer-token auth to at least read-only photo/avatar access, so companion
devices can carry an entity picture.

**Workaround:** none. No photo entities are created.
