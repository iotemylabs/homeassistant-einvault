# EinVault API — ground truth

Source of record: [`openapi-reference.json`](openapi-reference.json), fetched from
`GET https://einvault.example.com/api/openapi.json` on **2026-08-02**.

- `openapi`: 3.1.0
- `info.version`: **1.0.0** (this is the *API document* version, not the EinVault app version —
  the app version is not exposed by any reachable endpoint; see wishlist item W-1)
- sha256: `a739fdca48e49adb532210b6b1dc5f10863a8b493fa185c498f6950f58f57503`
- 13 documented paths, 33 component schemas, one security scheme (`bearerAuth`)

Everything below was verified against the live instance, not inferred from the spec alone.

---

## Where the spec differs from the build brief

These are the points where the brief and the authoritative spec disagree. **The spec wins**;
the integration is built to the right-hand column.

| # | Brief said | Spec / live instance says | Impact |
|---|---|---|---|
| D-1 | `/api/health` is a documented endpoint | **Not in the spec at all.** It exists and returns `200 {"status":"ok","timestamp":…,"woof":…}` unauthenticated | Still usable as a connectivity pre-check, but it is undocumented surface. Treat as best-effort: a failure means "cannot connect", a success does not prove the Bearer API is enabled. |
| D-2 | `species` is `enum: ['dog']` | Typed `string \| null`, nullable | Same conclusion (never branch on it) but the client must tolerate `null`, not just `"dog"`. Live data confirms the hazard: the one companion is a cat, `breed: "American Longhair"`, `bio: "Cat"`, and `species` reads `"dog"`. |
| D-3 | Write-only tokens "return only id, name, species, isActive" from `/api/companions` | True for `/api/companions`, but **every other GET returns `403 writeScopeReadOnly`** — logs, weight, reminders, shifts, users, journal, health-events | Decisive. A write-only token cannot feed a single sensor. Config flow aborts; see ADR-1. |
| D-4 | `notes` on quick logs | The `QuickLog` field is **`note`** (singular), not `notes` | Naming only, but it is exactly the kind of thing that would have silently read `None`. |
| D-5 | `POST /api/logs` requires a companion | Only **`type`** is required. `companionId` *and* `companionIds` are both optional; `companionIds` takes precedence when both are sent | Omitting both yields `400 noCompanions`. The service must always send exactly one of them. |
| D-6 | Reminder outcome enum `completed \| skipped` | Confirmed, plus `null` is a valid third state | Model field is `Literal["completed","skipped"] \| None`. |
| D-7 | `GET /api/weight` accepts `date` | It does **not**. Only `companionId`, `limit`, `offset`. `/api/logs` and `/api/health-events` do accept `date` | Client method signatures differ per endpoint; no shared "list with date" helper. |
| D-8 | Error codes listed as `invalidToken, rateLimited, notFound, invalidBody, noActiveShift, notAssigned, noTargets, noteTooLong, journalTooLong, invalidPagination` | Live/spec codes include several **not** in that list: `writeScopeReadOnly`, `noCompanions`, `invalidStatus`, `invalidDate`, `invalidType`, `invalidMood`, `invalidWeight`, `invalidUnit`, `titleRequired`, `notRecurring`, `alreadyCompleted`, `idempotencyKeyReused`, `invalidOccurredAt`, `invalidRecordedAt`, `forbidden`. `noTargets` and `invalidBody` were **not** observed — the real "no companion" code is `noCompanions` | Error mapping is built from the spec's actual code list, not the brief's. |
| D-9 | `Idempotency-Key` is accepted on all write endpoints | **Correct in behaviour, absent from the document.** The header is declared nowhere in the spec, and `POST /api/logs` declares no `409` — but upstream source shows `POST /api/logs` *does* wrap its handler in `withIdempotency()`, same as every other write. The missing 409 is a documentation gap only | Header is sent on every write, and retrying `POST /api/logs` with the same key **is** safe. See W-2 (downgraded to a docs bug). |

## Confirmed exactly as briefed

`GET /api/reminders` takes `companionId` as **optional** (one call covers all companions);
`/api/reminders/{id}/skip` exists despite being absent from the README; all request bodies are
`additionalProperties: false`; `notes` caps at 5000; journal `body` has no maxLength in the
schema but `journalTooLong` exists; `durationMinutes` is `exclusiveMinimum: 0, maximum: 480`;
`companionIds` is `maxItems: 50`; `subtypes` is `maxItems: 10` (a cap the brief did not mention).

---

## Verified response and error shapes

Captured live. Fixtures are derived from these, not invented.

```
GET  /api/health                    200  {"status":"ok","timestamp":"…Z","woof":"…"}      (no auth)
GET  /api/companions                200  {"companions":[ Companion, … ]}
GET  /api/companions/{id}           200  {"companion": Companion }
GET  /api/logs?companionId=…        200  {"events":[],"hasMore":false}
GET  /api/weight?companionId=…      200  {"entries":[],"hasMore":false}
GET  /api/health-events?companionId 200  {"events":[],"hasMore":false}
GET  /api/journal?companionId=…     200  {"entry":null}          <-- null, not {} or 404
GET  /api/reminders?status=all      200  {"reminders":[],"hasMore":false}
GET  /api/shifts                    200  {"shifts":[],"hasMore":false}
GET  /api/quick-logs                200  {"quickLogs":[]}
GET  /api/users                     200  {"users":[ User, … ],"hasMore":false}
```

Errors are `{"message": "...", "code": "..."}` — note `message` serializes **first**, which is
harmless but a reminder that key order is not contractual. Branch on `code` only; `message` is
localized (the instance sets an `einvault_locale` cookie).

```
401 {"message":"Invalid or revoked API token.","code":"invalidToken"}      bad token AND missing header
404 {"message":"Not found","code":"notFound"}                             unknown companion id
400 {"message":"limit must be 1-200 and offset must be 0 or more.","code":"invalidPagination"}
400 {"message":"status must be \"due\" or \"all\".","code":"invalidStatus"}
400 {"message":"Select at least one companion.","code":"noCompanions"}     GET /api/logs with no companionId
```

### The `api_disabled` discriminator (important)

Both "Bearer API turned off" and "unknown companion" surface as HTTP 404. They are
distinguishable by **body content type**:

- `API_TOKENS_ENABLED=false` → SvelteKit serves the SPA fallback: **`text/html`**, a `<!doctype html>`
  document. Verified by requesting `/api/bogus-route`, which is the same code path.
- Genuine not-found → **`application/json`** with `code: "notFound"`.

So: `404` + unparseable/non-JSON body ⇒ `api_disabled`; `404` + JSON `code` ⇒ map the code.
A client that blindly calls `response.json()` on the disabled case raises
`ContentTypeError`, which must not be reported to the user as "cannot connect".

---

## Rate limiting — measured

Probed directly against the LAN address: the **31st** request inside the window was refused.

```
HTTP/1.1 429 Too Many Requests
content-type: application/json

{"message":"Too many requests. Try again shortly.","code":"rateLimited"}
```

**No `Retry-After`. No `RateLimit-*`.** Not on the 429, not on 200s. A client cannot observe its
remaining budget and cannot be told when to come back, so backoff is necessarily blind: a fixed
60s → 120s → 300s schedule, reset on the next success. The client also refuses to issue further
requests locally while a cooldown is open, so a rate-limited integration stops consuming the
shared budget instead of hammering it.

## Write behaviour — verified live

All confirmed by writing to a real companion and reading the result back.

**Subtype validation is enforced, contrary to both the brief and a first reading of upstream.**
`src/lib/activitySubtypes.ts` exposes `parseSubtypes()`, which drops unrecognised values
silently — but the `POST /api/logs` route rejects them first:

```
POST /api/logs  {"type":"walk","subtypes":["leash","bogus-subtype"], …}
400 {"message":"Invalid subtype for this activity type","code":"invalidSubtype"}
```

`invalidSubtype` appears in **neither** the OpenAPI document nor the brief. The integration
still validates locally, because a local error can name the offending value and list the allowed
set, whereas the server's message cannot.

**Idempotency behaves exactly as the source implies.** Using key `K` on `POST /api/logs`:

| Sequence | Result |
|---|---|
| First call, valid body | `201 {"ids":["vqd5oz4qcpiiv6j"],"eventGroupId":null}` |
| Same key, **identical** body | `201` with the **same id** — a true replay, nothing created |
| Same key, **different** body | `409 {"code":"idempotencyKeyReused", …}` |
| Key used on a request that **failed validation** | Key stays free; a later request may reuse it |

That last row matters: a failed request stores no idempotency record, so a client that
generates one key per logical action and retries on failure behaves correctly.

**Journal upsert preserves omitted fields.** Posting `{companionId, date, mood:"great"}` to an
entry whose body was `"Integration test entry."` left the body intact and changed only the
mood. Omitting a key and sending an explicit null are therefore *not* equivalent — the client
never sends nulls.

**Error key order is not stable.** Most errors serialize `{"message":…,"code":…}`, but the 409
came back `{"code":…,"message":…}`. Harmless, and a reminder to parse rather than pattern-match.

### Idempotency semantics (verified in upstream source, `src/lib/server/api-idempotency.ts`)

Not discoverable from the spec, so recorded here:

- Header read is `idempotency-key`, case-insensitive, and **optional** — absent means the handler
  runs with no dedupe.
- The stored key is `(tokenId, endpoint, idempotencyKey)`, alongside a SHA-256 of the request body.
- Replay with an **identical body** returns the original stored response, including its original
  status code — so a replayed create still reads `201`.
- Same key with a **different body** → `409 idempotencyKeyReused`.
- Entries are pruned after **7 days**, opportunistically, scoped to the token.

Consequence for the integration: a uuid4 key per logical user action, reused across retries of
that same action, is exactly the right pattern, and it is safe on *every* write endpoint
including `POST /api/logs`.

### Proxy chain and the client-address problem

Deployment here is **Cloudflare Zero Trust tunnel → NPMPlus → EinVault container**, plus direct
LAN access on `http://192.168.1.246:7387`.

`requireApiToken` (`src/lib/server/auth/api-request.ts`, confirmed upstream) checks
`checkRateLimit('api-ip:' + ip, 30, 60_000)` **before** resolving the token, and wraps
`getClientAddress()` in a try/catch that falls back to the literal string `'unknown'`.

adapter-node's `getClientAddress()` (`packages/adapter-node/src/handler.js`):

```js
if (address_header) {
  if (!(address_header in req.headers)) {
    throw new Error(`Address header was specified with ADDRESS_HEADER=... but is absent from request`);
  }
  if (address_header === 'x-forwarded-for') {
    if (xff_depth > addresses.length) throw new Error(...);
  }
}
return req.connection.remoteAddress;   // default when ADDRESS_HEADER is unset
```

Two things follow, and they point the opposite way from the brief's assumption:

1. **Traffic arriving over the tunnel** has NPMPlus as the TCP peer, so with `ADDRESS_HEADER`
   unset every external client shares one 30-req/60s bucket keyed on the NPMPlus address.
2. **Traffic arriving directly on the LAN** has the real client as the TCP peer. Home Assistant
   polling `192.168.1.246:7387` therefore **already gets its own dedicated bucket** and shares
   nothing.

So pointing Home Assistant at the LAN address solves the rate-limit collision outright, and
`ADDRESS_HEADER` is not required for the integration to behave.

Worse, setting `ADDRESS_HEADER=x-forwarded-for` *without* also fixing direct-LAN access is a
regression: a direct request carries no `X-Forwarded-For`, `getClientAddress()` throws,
EinVault catches it, and the caller lands in the single global `'unknown'` bucket shared by
every other direct-LAN client. `ADDRESS_HEADER` is also absent from `.env.example` upstream, so
it is undocumented adapter surface rather than a supported EinVault setting (wishlist W-4).

### This instance is behind Cloudflare

Response headers show `Server: cloudflare`, `CF-RAY`, `cf-cache-status`. That matters more
than a generic reverse proxy:

- `getClientAddress()` will see the Cloudflare/tunnel edge address unless `ADDRESS_HEADER`
  is set, so **every** consumer collapses into one shared 30-req/60s bucket.
- Traffic from Home Assistant to a machine on the same network would otherwise leave the
  LAN, cross the public internet, and come back.

Recommended in the README: point Home Assistant at the **internal** address
(`http://<unraid-host>:<port>` or the Docker network name), not `einvault.example.com`.

---

## Token scope and role detection

Verified with the live token: **full scope, `admin` role**.

- Scope is detected from `GET /api/companions`: presence of profile keys (`breed`, `dob`,
  `weightUnit`, `vetName`, …) ⇒ full; only `id`/`name`/`species`/`isActive` ⇒ write-only.
- `GET /api/companions` declares no 403, so it is reachable at any scope — it is the only safe
  probe for scope detection.
- `GET /api/users` returns `username` only for admin-scoped tokens (the spec notes it is
  "omitted for member-scoped tokens"), so `displayName` is the field to rely on for the
  shift-attribution attribute.

## Live instance snapshot (2026-08-02)

One companion, everything else empty:

| Collection | Count |
|---|---|
| companions | 1 (`Cindy`, a cat reporting `species: "dog"`) |
| quick-logs | 0 |
| reminders | 0 |
| shifts | 0 |
| logs / weight / health-events / journal | 0 / 0 / 0 / none |

Insufficient for realistic fixtures. Populated captures still needed before the coordinator
and entity tests can be built from real data — see the request in the session notes.
