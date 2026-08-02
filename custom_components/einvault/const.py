"""Constants for the EinVault integration.

This module is deliberately free of ``homeassistant`` imports so that
:mod:`custom_components.einvault.api` — which must remain independently
testable — can import from it. Home Assistant specific constants such as
``PLATFORMS`` live in ``__init__.py`` instead.

Enum values mirror ``docs/openapi-reference.json`` (API document 1.0.0).
"""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "einvault"

CONF_URL: Final = "url"
CONF_API_TOKEN: Final = "api_token"

CONF_SCAN_INTERVAL: Final = "scan_interval"
CONF_ENABLE_MOOD_SENSOR: Final = "enable_mood_sensor"
CONF_INCLUDE_ARCHIVED: Final = "include_archived"
CONF_CALENDAR_FEED_URL: Final = "calendar_feed_url"

DEFAULT_SCAN_INTERVAL: Final = 300
"""Five minutes. The instance-wide pre-auth limit is 30 requests / 60s."""

MIN_SCAN_INTERVAL: Final = 60
"""Hard floor enforced by the options flow."""

SLOW_REFRESH_INTERVAL: Final = 3600
"""Cadence for slow-changing collections (companions, quick logs, users)."""

DEFAULT_ENABLE_MOOD_SENSOR: Final = False
DEFAULT_INCLUDE_ARCHIVED: Final = False

DEFAULT_TIMEOUT: Final = 30
"""Total per-request timeout in seconds."""

LIST_PAGE_SIZE: Final = 50
"""Server default and our page size. Server accepts 1-200."""

MAX_PAGE_SIZE: Final = 200
MAX_OFFSET: Final = 100_000

RATE_LIMIT_BACKOFF: Final = (60, 120, 300)
"""Escalating cooldown after a 429, in seconds.

The server exposes neither ``Retry-After`` nor ``RateLimit-*`` headers
(verified 2026-08-02), so the schedule is fixed rather than adaptive.
"""

# --- Enumerations mirrored from the OpenAPI document -----------------------

DAILY_EVENT_TYPES: Final = (
    "walk",
    "meal",
    "bathroom",
    "treat",
    "play",
    "grooming",
    "other",
)

HEALTH_EVENT_TYPES: Final = (
    "vet_visit",
    "vaccination",
    "medication",
    "procedure",
    "other",
)

REMINDER_TYPES: Final = ("vet", "medication", "vaccination", "grooming", "other")

REMINDER_OUTCOMES: Final = ("completed", "skipped")

JOURNAL_MOODS: Final = ("great", "good", "meh", "off", "sick")

WEIGHT_UNITS: Final = ("kg", "lbs")

COMPANION_SEXES: Final = ("male", "female", "unknown")

USER_ROLES: Final = ("admin", "member", "caretaker")

SUBTYPES_BY_TYPE: Final[dict[str, tuple[str, ...]]] = {
    "bathroom": ("pee", "poop"),
    "walk": ("leash", "offleash", "hike"),
    "meal": ("breakfast", "lunch", "dinner", "snack"),
    "play": ("fetch", "tug", "puzzle", "social"),
    "grooming": ("bath", "brush", "trim", "nails", "teeth", "ears"),
    "treat": ("chew", "dental", "training"),
    "other": (),
}
"""Verified against upstream ``src/lib/activitySubtypes.ts`` and confirmed live.

``POST /api/logs`` rejects an unrecognised subtype with ``400 invalidSubtype``
(verified 2026-08-02 — the code appears nowhere in the OpenAPI document).
Validating locally first turns a round-trip failure into an immediate, precise
error naming the offending value and the allowed set.
"""

MAX_SUBTYPES: Final = 10
MAX_NOTE_LENGTH: Final = 5000
MAX_JOURNAL_BODY_LENGTH: Final = 20_000
MAX_DURATION_MINUTES: Final = 480
MAX_COMPANION_IDS: Final = 50

# --- API error codes -------------------------------------------------------
# Observed live or declared in the OpenAPI document. Branch on these, never on
# ``message`` — messages are localized (the server sets an ``einvault_locale``
# cookie).

ERROR_INVALID_TOKEN: Final = "invalidToken"
ERROR_RATE_LIMITED: Final = "rateLimited"
ERROR_NOT_FOUND: Final = "notFound"
ERROR_NO_COMPANIONS: Final = "noCompanions"
ERROR_INVALID_PAGINATION: Final = "invalidPagination"
ERROR_INVALID_STATUS: Final = "invalidStatus"
ERROR_INVALID_DATE: Final = "invalidDate"
ERROR_INVALID_TYPE: Final = "invalidType"
ERROR_INVALID_SUBTYPE: Final = "invalidSubtype"
ERROR_INVALID_MOOD: Final = "invalidMood"
ERROR_INVALID_WEIGHT: Final = "invalidWeight"
ERROR_INVALID_UNIT: Final = "invalidUnit"
ERROR_INVALID_OCCURRED_AT: Final = "invalidOccurredAt"
ERROR_INVALID_RECORDED_AT: Final = "invalidRecordedAt"
ERROR_TITLE_REQUIRED: Final = "titleRequired"
ERROR_NOTE_TOO_LONG: Final = "noteTooLong"
ERROR_JOURNAL_TOO_LONG: Final = "journalTooLong"
ERROR_NO_ACTIVE_SHIFT: Final = "noActiveShift"
ERROR_NOT_ASSIGNED: Final = "notAssigned"
ERROR_WRITE_SCOPE_READ_ONLY: Final = "writeScopeReadOnly"
ERROR_FORBIDDEN: Final = "forbidden"
ERROR_NOT_RECURRING: Final = "notRecurring"
ERROR_ALREADY_COMPLETED: Final = "alreadyCompleted"
ERROR_IDEMPOTENCY_KEY_REUSED: Final = "idempotencyKeyReused"
