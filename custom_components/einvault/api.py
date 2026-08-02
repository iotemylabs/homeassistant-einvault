"""Async HTTP client for the EinVault bearer API.

Deliberately free of any ``homeassistant`` import so it can be tested in
isolation and, eventually, extracted to a PyPI package for Core acceptance.
It takes an :class:`aiohttp.ClientSession` and owns no I/O lifecycle of its own.

Two behaviours here are non-obvious and load-bearing:

* **Requests are strictly sequential.** An ``asyncio.Semaphore(1)`` serialises
  every call. The server applies a 30-request/60-second limit keyed on client
  IP *before* the token is resolved, so a concurrent burst is the fastest way
  to lock the whole integration out.
* **A 429 sets a client-wide cooldown.** The server returns neither
  ``Retry-After`` nor ``RateLimit-*`` headers (verified against a live
  instance), so recovery uses a fixed escalating schedule and short-circuits
  further requests locally rather than spending more of the shared budget.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from http import HTTPStatus
import logging
import re
from types import MappingProxyType
from typing import Any, Final, Literal
from urllib.parse import urlsplit, urlunsplit

from aiohttp import ClientError, ClientResponse, ClientSession, ClientTimeout

from .const import (
    DEFAULT_TIMEOUT,
    ERROR_INVALID_TOKEN,
    ERROR_NOT_FOUND,
    ERROR_RATE_LIMITED,
    LIST_PAGE_SIZE,
    MAX_COMPANION_IDS,
    MAX_DURATION_MINUTES,
    MAX_JOURNAL_BODY_LENGTH,
    MAX_NOTE_LENGTH,
    MAX_SUBTYPES,
    RATE_LIMIT_BACKOFF,
    SUBTYPES_BY_TYPE,
)
from .models import (
    FULL_SCOPE_MARKER_KEYS,
    Companion,
    HealthEvent,
    InstanceHealth,
    JournalEntry,
    ListPage,
    LogEvent,
    QuickLog,
    Reminder,
    Shift,
    TokenScope,
    User,
    WeightEntry,
    WriteResult,
)

_LOGGER = logging.getLogger(__name__)

ReminderStatus = Literal["due", "all"]

# Error codes that mean "the token is no longer usable" and should trigger a
# reauth flow rather than a transient failure.
_AUTH_ERROR_CODES: Final = frozenset({ERROR_INVALID_TOKEN})


class EinVaultError(Exception):
    """Base class for every error raised by this client."""


class EinVaultConnectionError(EinVaultError):
    """The instance could not be reached, or the response was unreadable."""


class EinVaultApiDisabledError(EinVaultError):
    """The bearer API is turned off (``API_TOKENS_ENABLED=false``).

    Distinguished from a genuine 404 by the response body: a disabled API
    falls through to the SvelteKit SPA handler and returns ``text/html``,
    whereas a real not-found returns JSON carrying ``code``.
    """


class EinVaultAuthError(EinVaultError):
    """The token is missing, invalid, or revoked."""


class EinVaultRateLimitError(EinVaultError):
    """The rate limit was hit, or a local cooldown is still in effect."""

    def __init__(self, message: str, retry_after: float) -> None:
        """Record how long the caller should wait."""
        super().__init__(message)
        self.retry_after = retry_after


class EinVaultResponseError(EinVaultError):
    """A structured ``{code, message}`` error from the API.

    Callers branch on :attr:`code`. ``message`` is localized by the server and
    must never be used for control flow.
    """

    def __init__(self, status: int, code: str, message: str) -> None:
        """Capture the status and the machine-readable code."""
        super().__init__(f"{code}: {message}")
        self.status = status
        self.code = code
        self.api_message = message


class EinVaultNotFoundError(EinVaultResponseError):
    """The requested resource does not exist, or the token cannot see it."""


class EinVaultForbiddenError(EinVaultResponseError):
    """The token's role or scope forbids the operation."""


class EinVaultValidationError(EinVaultResponseError):
    """The server rejected the request body or query parameters."""


class EinVaultConflictError(EinVaultResponseError):
    """A 409 — reused idempotency key, or an already-completed reminder."""


def normalize_base_url(url: str) -> str:
    """Normalize a base URL for storage and unique-id derivation.

    Lowercases scheme and host, drops a default port, strips any path and
    trailing slash. Two config entries differing only in these respects refer
    to the same instance and must collide.
    """
    candidate = url.strip()
    if not candidate:
        raise ValueError("URL must not be empty")
    if "://" not in candidate:
        candidate = f"http://{candidate}"

    parts = urlsplit(candidate)
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"Unsupported URL scheme: {parts.scheme}")
    if not parts.hostname:
        raise ValueError("URL must include a host")

    host = parts.hostname.lower()
    port = parts.port
    default_port = 443 if parts.scheme == "https" else 80
    netloc = host if port in (None, default_port) else f"{host}:{port}"

    return urlunsplit((parts.scheme, netloc, "", "", "")).rstrip("/")


def validate_subtypes(event_type: str, subtypes: list[str] | None) -> list[str]:
    """Validate subtypes against an activity type before any HTTP call.

    The server answers ``400 invalidSubtype`` for a mismatch, but a local
    check produces a far better message — it can name the offending value and
    list what was allowed.

    Raises:
        ValueError: if the type is unknown, a subtype does not belong to it,
            or too many were supplied.
    """
    if event_type not in SUBTYPES_BY_TYPE:
        allowed = ", ".join(sorted(SUBTYPES_BY_TYPE))
        raise ValueError(f"Unknown activity type '{event_type}'. Expected one of: {allowed}")

    if not subtypes:
        return []

    if len(subtypes) > MAX_SUBTYPES:
        raise ValueError(f"At most {MAX_SUBTYPES} subtypes may be supplied, got {len(subtypes)}")

    allowed_subtypes = SUBTYPES_BY_TYPE[event_type]
    if not allowed_subtypes:
        raise ValueError(f"Activity type '{event_type}' accepts no subtypes")

    invalid = [s for s in subtypes if s not in allowed_subtypes]
    if invalid:
        raise ValueError(
            f"Invalid subtype(s) {', '.join(sorted(invalid))} for type '{event_type}'. "
            f"Allowed: {', '.join(allowed_subtypes)}"
        )

    # Dedupe while preserving the caller's order.
    return list(dict.fromkeys(subtypes))


class EinVaultClient:
    """Typed async client for the EinVault bearer API."""

    def __init__(
        self,
        base_url: str,
        api_token: str,
        session: ClientSession,
        *,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        """Create a client bound to an externally owned session."""
        self._base_url = normalize_base_url(base_url)
        self._api_token = api_token
        self._session = session
        self._timeout = ClientTimeout(total=timeout)

        # One in-flight request at a time. See module docstring.
        self._lock = asyncio.Semaphore(1)

        self._request_count = 0
        self._rate_limited_until: float = 0.0
        self._backoff_index = 0

    @property
    def base_url(self) -> str:
        """The normalized base URL."""
        return self._base_url

    @property
    def request_count(self) -> int:
        """Total requests attempted, for the call-budget diagnostic sensor."""
        return self._request_count

    def update_token(self, api_token: str) -> None:
        """Swap in a rotated token after a successful reauth."""
        self._api_token = api_token

    # -- request plumbing --------------------------------------------------

    def _auth_headers(self, idempotency_key: str | None = None) -> dict[str, str]:
        """Build request headers.

        The token goes in the ``Authorization`` header only — never a query
        string, where it would land in server access logs.
        """
        headers = {
            "Authorization": f"Bearer {self._api_token}",
            "Accept": "application/json",
        }
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        authenticated: bool = True,
    ) -> Any:
        """Issue one request, serialised against all others.

        Returns the decoded JSON body, or ``None`` for an empty response.
        """
        loop = asyncio.get_running_loop()
        if (remaining := self._rate_limited_until - loop.time()) > 0:
            raise EinVaultRateLimitError(
                f"Rate limited; retrying in {remaining:.0f}s", retry_after=remaining
            )

        url = f"{self._base_url}{path}"
        headers = (
            self._auth_headers(idempotency_key) if authenticated else {"Accept": "application/json"}
        )
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}

        async with self._lock:
            self._request_count += 1
            try:
                async with self._session.request(
                    method,
                    url,
                    params=clean_params or None,
                    json=json_body,
                    headers=headers,
                    timeout=self._timeout,
                ) as response:
                    return await self._handle_response(response, loop)
            except TimeoutError as err:
                raise EinVaultConnectionError(f"Timed out talking to {self._base_url}") from err
            except ClientError as err:
                raise EinVaultConnectionError(f"Cannot reach {self._base_url}: {err}") from err

    async def _handle_response(
        self, response: ClientResponse, loop: asyncio.AbstractEventLoop
    ) -> Any:
        """Translate an HTTP response into data or a typed exception."""
        if response.status == HTTPStatus.TOO_MANY_REQUESTS:
            raise EinVaultRateLimitError(
                "Rate limited by EinVault (30 requests / 60s per client IP)",
                retry_after=self._enter_cooldown(loop),
            )

        payload = await self._decode(response)

        if response.status < HTTPStatus.BAD_REQUEST:
            self._reset_cooldown()
            return payload

        # A 404 with a non-JSON body means the route does not exist at all,
        # which for /api/* means the bearer API is disabled.
        if response.status == HTTPStatus.NOT_FOUND and not isinstance(payload, dict):
            raise EinVaultApiDisabledError(
                "The EinVault bearer API is disabled on this instance. "
                "Set API_TOKENS_ENABLED=true and restart the container."
            )

        code = payload.get("code", "") if isinstance(payload, dict) else ""
        message = payload.get("message", "") if isinstance(payload, dict) else ""

        if response.status == HTTPStatus.UNAUTHORIZED or code in _AUTH_ERROR_CODES:
            raise EinVaultAuthError(message or "The API token is invalid or has been revoked")
        if code == ERROR_RATE_LIMITED:
            raise EinVaultRateLimitError(
                message or "Rate limited", retry_after=self._enter_cooldown(loop)
            )
        if response.status == HTTPStatus.NOT_FOUND or code == ERROR_NOT_FOUND:
            raise EinVaultNotFoundError(response.status, code or ERROR_NOT_FOUND, message)
        if response.status == HTTPStatus.FORBIDDEN:
            raise EinVaultForbiddenError(response.status, code or "forbidden", message)
        if response.status == HTTPStatus.CONFLICT:
            raise EinVaultConflictError(response.status, code or "conflict", message)
        if response.status == HTTPStatus.BAD_REQUEST:
            raise EinVaultValidationError(response.status, code or "invalidRequest", message)

        raise EinVaultResponseError(
            response.status, code or "unknown", message or "Unexpected error"
        )

    @staticmethod
    async def _decode(response: ClientResponse) -> Any:
        """Decode a JSON body, returning the raw text when it is not JSON.

        A disabled API serves an HTML page, so this must not raise.
        """
        try:
            return await response.json(content_type=None)
        except (ValueError, ClientError):
            try:
                return await response.text()
            except ClientError:  # pragma: no cover - defensive
                return None

    def _enter_cooldown(self, loop: asyncio.AbstractEventLoop) -> float:
        """Start (or escalate) the local cooldown, returning the delay applied.

        The delay is returned rather than re-read afterwards: the index has
        already advanced by then, so reading it back reports the *next*
        backoff step instead of the one now in force.
        """
        delay = RATE_LIMIT_BACKOFF[min(self._backoff_index, len(RATE_LIMIT_BACKOFF) - 1)]
        self._rate_limited_until = loop.time() + delay
        self._backoff_index = min(self._backoff_index + 1, len(RATE_LIMIT_BACKOFF) - 1)
        return float(delay)

    def _reset_cooldown(self) -> None:
        """Clear the cooldown after any successful response."""
        self._rate_limited_until = 0.0
        self._backoff_index = 0

    # -- reads -------------------------------------------------------------

    async def async_get_health(self) -> InstanceHealth:
        """Check reachability via the unauthenticated health endpoint.

        Success proves the instance is up, **not** that the bearer API is
        enabled — that requires an authenticated call.
        """
        payload = await self._request("GET", "/api/health", authenticated=False)
        if not isinstance(payload, dict):
            raise EinVaultConnectionError("Health endpoint returned an unexpected response")
        return InstanceHealth.from_api(payload)

    async def async_get_companions(self) -> list[Companion]:
        """List every companion the token may target."""
        payload = await self._request("GET", "/api/companions")
        return [Companion.from_api(item) for item in self._expect_list(payload, "companions")]

    async def async_get_companion(self, companion_id: str) -> Companion:
        """Fetch one companion. Archived or unreachable ids raise not-found."""
        payload = await self._request("GET", f"/api/companions/{companion_id}")
        if not isinstance(payload, dict) or "companion" not in payload:
            raise EinVaultConnectionError("Malformed companion response")
        return Companion.from_api(payload["companion"])

    async def async_detect_token_scope(self) -> tuple[TokenScope, list[Companion]]:
        """Determine the token's access level from a single companions call.

        A write-only token receives only ``id``/``name``/``species``/
        ``isActive``; a full token also carries profile fields. ``GET
        /api/companions`` is the only endpoint that answers at both access
        levels, which makes it the sole safe probe.
        """
        payload = await self._request("GET", "/api/companions")
        raw = self._expect_list(payload, "companions")
        companions = [Companion.from_api(item) for item in raw]

        if not raw:
            # No companions to inspect. Assume full and let later calls fail
            # loudly rather than blocking setup on an empty instance.
            return TokenScope.FULL, companions

        has_profile_keys = any(key in item for item in raw for key in FULL_SCOPE_MARKER_KEYS)
        scope = TokenScope.FULL if has_profile_keys else TokenScope.WRITE_ONLY
        return scope, companions

    async def async_get_logs(
        self,
        companion_id: str,
        *,
        on_date: date | None = None,
        limit: int = LIST_PAGE_SIZE,
        offset: int = 0,
    ) -> ListPage[LogEvent]:
        """Read logged daily events for a companion, newest first."""
        payload = await self._request(
            "GET",
            "/api/logs",
            params={
                "companionId": companion_id,
                "date": on_date.isoformat() if on_date else None,
                "limit": limit,
                "offset": offset,
            },
        )
        return self._page(payload, "events", LogEvent.from_api)

    async def async_get_weight(
        self,
        companion_id: str,
        *,
        limit: int = LIST_PAGE_SIZE,
        offset: int = 0,
    ) -> ListPage[WeightEntry]:
        """Read weight entries for a companion, newest first.

        Unlike logs and health events, this endpoint accepts no ``date``.
        """
        payload = await self._request(
            "GET",
            "/api/weight",
            params={"companionId": companion_id, "limit": limit, "offset": offset},
        )
        return self._page(payload, "entries", WeightEntry.from_api)

    async def async_get_health_events(
        self,
        companion_id: str,
        *,
        on_date: date | None = None,
        limit: int = LIST_PAGE_SIZE,
        offset: int = 0,
    ) -> ListPage[HealthEvent]:
        """Read health events for a companion."""
        payload = await self._request(
            "GET",
            "/api/health-events",
            params={
                "companionId": companion_id,
                "date": on_date.isoformat() if on_date else None,
                "limit": limit,
                "offset": offset,
            },
        )
        return self._page(payload, "events", HealthEvent.from_api)

    async def async_get_journal(
        self, companion_id: str, *, on_date: date | None = None
    ) -> JournalEntry | None:
        """Read one companion's journal entry for a day.

        Returns ``None`` when no entry exists — the server sends
        ``{"entry": null}`` rather than a 404.
        """
        payload = await self._request(
            "GET",
            "/api/journal",
            params={
                "companionId": companion_id,
                "date": on_date.isoformat() if on_date else None,
            },
        )
        if not isinstance(payload, dict):
            raise EinVaultConnectionError("Malformed journal response")
        entry = payload.get("entry")
        return JournalEntry.from_api(entry) if isinstance(entry, dict) else None

    async def async_get_reminders(
        self,
        *,
        companion_id: str | None = None,
        status: ReminderStatus | None = None,
        limit: int = LIST_PAGE_SIZE,
        offset: int = 0,
    ) -> ListPage[Reminder]:
        """List reminders.

        ``companion_id`` is optional; omitting it covers every companion in a
        single request, which is how the coordinator keeps its budget down.
        """
        payload = await self._request(
            "GET",
            "/api/reminders",
            params={
                "companionId": companion_id,
                "status": status,
                "limit": limit,
                "offset": offset,
            },
        )
        return self._page(payload, "reminders", Reminder.from_api)

    async def async_get_shifts(
        self,
        *,
        date_from: date | None = None,
        date_to: date | None = None,
        limit: int = LIST_PAGE_SIZE,
        offset: int = 0,
    ) -> ListPage[Shift]:
        """List caretaker shifts."""
        payload = await self._request(
            "GET",
            "/api/shifts",
            params={
                "from": date_from.isoformat() if date_from else None,
                "to": date_to.isoformat() if date_to else None,
                "limit": limit,
                "offset": offset,
            },
        )
        return self._page(payload, "shifts", Shift.from_api)

    async def async_get_users(
        self, *, limit: int = LIST_PAGE_SIZE, offset: int = 0
    ) -> ListPage[User]:
        """List the user roster, scoped to what the token may see."""
        payload = await self._request(
            "GET", "/api/users", params={"limit": limit, "offset": offset}
        )
        return self._page(payload, "users", User.from_api)

    async def async_get_quick_logs(self) -> list[QuickLog]:
        """List the token user's enabled quick logs.

        Quick logs are per-user: an empty list means the token's owner has
        configured none, not that the feature is unavailable.
        """
        payload = await self._request("GET", "/api/quick-logs")
        return [QuickLog.from_api(item) for item in self._expect_list(payload, "quickLogs")]

    # -- writes ------------------------------------------------------------

    async def async_execute_quick_log(
        self, quick_log_id: str, *, idempotency_key: str | None = None
    ) -> WriteResult:
        """Run a configured quick log.

        The body is intentionally empty so that every parameter — type,
        subtypes, duration, note, and target companions — stays configured in
        EinVault rather than being duplicated in Home Assistant.
        """
        payload = await self._request(
            "POST",
            f"/api/quick-logs/{quick_log_id}/execute",
            idempotency_key=idempotency_key,
        )
        return self._write_result(payload)

    async def async_log_activity(
        self,
        *,
        event_type: str,
        companion_id: str | None = None,
        companion_ids: list[str] | None = None,
        subtypes: list[str] | None = None,
        duration_minutes: int | None = None,
        notes: str | None = None,
        logged_at: datetime | None = None,
        idempotency_key: str | None = None,
    ) -> WriteResult:
        """Log a daily event.

        Exactly one of ``companion_id`` or ``companion_ids`` must be supplied.
        The API technically requires neither, but omitting both yields
        ``400 noCompanions``, so it is rejected locally instead.
        """
        checked_subtypes = validate_subtypes(event_type, subtypes)

        if companion_id and companion_ids:
            raise ValueError("Supply either companion_id or companion_ids, not both")
        if not companion_id and not companion_ids:
            raise ValueError("A target companion is required")
        if companion_ids and len(companion_ids) > MAX_COMPANION_IDS:
            raise ValueError(f"At most {MAX_COMPANION_IDS} companions may be targeted")
        if duration_minutes is not None and not 0 < duration_minutes <= MAX_DURATION_MINUTES:
            raise ValueError(f"duration_minutes must be between 1 and {MAX_DURATION_MINUTES}")
        if notes is not None and len(notes) > MAX_NOTE_LENGTH:
            raise ValueError(f"notes must be at most {MAX_NOTE_LENGTH} characters")

        # Built key by key: the schema is additionalProperties=false, so a
        # stray null would be rejected outright.
        body: dict[str, Any] = {"type": event_type}
        if companion_ids:
            body["companionIds"] = companion_ids
        elif companion_id:
            body["companionId"] = companion_id
        if checked_subtypes:
            body["subtypes"] = checked_subtypes
        if duration_minutes is not None:
            body["durationMinutes"] = duration_minutes
        if notes is not None:
            body["notes"] = notes
        if logged_at is not None:
            body["loggedAt"] = logged_at.isoformat()

        payload = await self._request(
            "POST", "/api/logs", json_body=body, idempotency_key=idempotency_key
        )
        return self._write_result(payload)

    async def async_log_weight(
        self,
        *,
        companion_id: str,
        weight: float,
        unit: str,
        notes: str | None = None,
        recorded_at: datetime | None = None,
        idempotency_key: str | None = None,
    ) -> WriteResult:
        """Record a weight entry."""
        if weight <= 0:
            raise ValueError("weight must be greater than zero")
        if notes is not None and len(notes) > MAX_NOTE_LENGTH:
            raise ValueError(f"notes must be at most {MAX_NOTE_LENGTH} characters")

        body: dict[str, Any] = {
            "companionId": companion_id,
            "weight": weight,
            "unit": unit,
        }
        if notes is not None:
            body["notes"] = notes
        if recorded_at is not None:
            body["recordedAt"] = recorded_at.isoformat()

        payload = await self._request(
            "POST", "/api/weight", json_body=body, idempotency_key=idempotency_key
        )
        return self._write_result(payload)

    async def async_log_health_event(
        self,
        *,
        companion_id: str,
        event_type: str,
        title: str,
        notes: str | None = None,
        occurred_at: datetime | None = None,
        vet_name: str | None = None,
        vet_clinic: str | None = None,
        idempotency_key: str | None = None,
    ) -> WriteResult:
        """Record a health event."""
        if not title.strip():
            raise ValueError("title is required")
        if notes is not None and len(notes) > MAX_NOTE_LENGTH:
            raise ValueError(f"notes must be at most {MAX_NOTE_LENGTH} characters")

        body: dict[str, Any] = {
            "companionId": companion_id,
            "type": event_type,
            "title": title,
        }
        if notes is not None:
            body["notes"] = notes
        if occurred_at is not None:
            body["occurredAt"] = occurred_at.isoformat()
        if vet_name is not None:
            body["vetName"] = vet_name
        if vet_clinic is not None:
            body["vetClinic"] = vet_clinic

        payload = await self._request(
            "POST", "/api/health-events", json_body=body, idempotency_key=idempotency_key
        )
        return self._write_result(payload)

    async def async_set_journal(
        self,
        *,
        companion_id: str,
        entry_date: date | None = None,
        body_text: str | None = None,
        mood: str | None = None,
        idempotency_key: str | None = None,
    ) -> WriteResult:
        """Upsert a journal entry.

        Omitting a field leaves the stored value untouched (verified against a
        live instance: posting only ``mood`` preserved the existing body).
        Nulls are therefore never sent — an omitted key and an explicit null
        mean different things here.
        """
        if body_text is not None and len(body_text) > MAX_JOURNAL_BODY_LENGTH:
            raise ValueError(f"body must be at most {MAX_JOURNAL_BODY_LENGTH} characters")

        body: dict[str, Any] = {"companionId": companion_id}
        if entry_date is not None:
            body["date"] = entry_date.isoformat()
        if body_text is not None:
            body["body"] = body_text
        if mood is not None:
            body["mood"] = mood

        payload = await self._request(
            "POST", "/api/journal", json_body=body, idempotency_key=idempotency_key
        )
        return self._write_result(payload)

    async def async_complete_reminder(
        self, reminder_id: str, *, idempotency_key: str | None = None
    ) -> WriteResult:
        """Mark a reminder complete.

        A recurring reminder spawns its next occurrence, whose id comes back
        as ``nextReminderId``.
        """
        payload = await self._request(
            "POST",
            f"/api/reminders/{reminder_id}/complete",
            idempotency_key=idempotency_key,
        )
        return self._write_result(payload)

    async def async_skip_reminder(
        self, reminder_id: str, *, idempotency_key: str | None = None
    ) -> WriteResult:
        """Skip a recurring reminder occurrence.

        One-off reminders answer ``400 notRecurring``.
        """
        payload = await self._request(
            "POST",
            f"/api/reminders/{reminder_id}/skip",
            idempotency_key=idempotency_key,
        )
        return self._write_result(payload)

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _expect_list(payload: Any, key: str) -> list[dict[str, Any]]:
        """Pull a list of objects out of a wrapped response."""
        if not isinstance(payload, dict) or not isinstance(payload.get(key), list):
            raise EinVaultConnectionError(f"Malformed response: expected a '{key}' list")
        return [item for item in payload[key] if isinstance(item, dict)]

    @classmethod
    def _page(cls, payload: Any, key: str, factory: Any) -> ListPage[Any]:
        """Build a :class:`ListPage` from a wrapped list response."""
        items = cls._expect_list(payload, key)
        has_more = bool(payload.get("hasMore", False)) if isinstance(payload, dict) else False
        return ListPage(items=[factory(item) for item in items], has_more=has_more)

    @staticmethod
    def _write_result(payload: Any) -> WriteResult:
        """Normalize the varying shapes returned by write endpoints."""
        if not isinstance(payload, dict):
            return WriteResult()
        ids: tuple[str, ...] = ()
        if isinstance(payload.get("ids"), list):
            ids = tuple(str(i) for i in payload["ids"])
        elif isinstance(payload.get("id"), str):
            ids = (payload["id"],)
        return WriteResult(
            ids=ids,
            event_group_id=payload.get("eventGroupId"),
            next_reminder_id=payload.get("nextReminderId"),
            raw=MappingProxyType(payload).copy(),
        )


CALENDAR_FEED_PATH_RE = re.compile(r"^/api/calendar/(?P<token>[^/]+)/feed\.ics$")


def parse_calendar_feed_url(url: str) -> tuple[str, str]:
    """Split a calendar feed URL into its base URL and feed token.

    The feed token is a path segment rather than a header, which is EinVault's
    design and not something a client can change. It is therefore treated as a
    credential everywhere it is handled: never logged, and redacted from
    diagnostics.

    Raises:
        ValueError: if the URL is not a calendar feed URL.
    """
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("Calendar feed URL must be a full http(s) URL")

    match = CALENDAR_FEED_PATH_RE.match(parts.path)
    if not match:
        raise ValueError(
            "Expected a URL ending in /api/calendar/<token>/feed.ics — copy it from "
            "EinVault under Settings, Calendar feed"
        )

    base = normalize_base_url(urlunsplit((parts.scheme, parts.netloc, "", "", "")))
    return base, match.group("token")


class EinVaultCalendarFeedClient:
    """Fetches the personal ICS calendar feed.

    Deliberately separate from :class:`EinVaultClient`. The feed route does not
    go through ``requireApiToken`` — it authenticates on the token in its path —
    so it is exempt from the 30-request/60-second budget, and counting it there
    would misreport the figure the diagnostic sensor exists to show.
    """

    def __init__(
        self,
        base_url: str,
        feed_token: str,
        session: ClientSession,
        *,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        """Create a feed client."""
        self._base_url = normalize_base_url(base_url)
        self._feed_token = feed_token
        self._session = session
        self._timeout = ClientTimeout(total=timeout)

    @property
    def feed_url(self) -> str:
        """The full feed URL. Contains a credential — never log this."""
        return f"{self._base_url}/api/calendar/{self._feed_token}/feed.ics"

    async def async_fetch_ics(
        self, *, companion_id: str | None = None, kinds: list[str] | None = None
    ) -> str:
        """Fetch the feed, optionally filtered server-side.

        Raises:
            EinVaultAuthError: the feed token is unknown or the feed is
                disabled (``CALENDAR_FEED_ENABLED=false``). Both answer 404
                with a plain-text body, and neither is retryable.
            EinVaultConnectionError: the instance was unreachable.
        """
        params: dict[str, str] = {}
        if companion_id:
            params["companion"] = companion_id
        if kinds:
            params["type"] = ",".join(kinds)

        try:
            async with self._session.get(
                self.feed_url, params=params or None, timeout=self._timeout
            ) as response:
                if response.status == HTTPStatus.NOT_FOUND:
                    raise EinVaultAuthError(
                        "The EinVault calendar feed rejected this token. It may have "
                        "been regenerated, or the feed may be disabled on the server."
                    )
                if response.status >= HTTPStatus.BAD_REQUEST:
                    raise EinVaultConnectionError(f"Calendar feed returned HTTP {response.status}")
                return await response.text()
        except TimeoutError as err:
            raise EinVaultConnectionError("Timed out fetching the calendar feed") from err
        except ClientError as err:
            # The URL carries the feed token, so it must not reach the message.
            raise EinVaultConnectionError(
                f"Cannot reach the calendar feed: {err.__class__.__name__}"
            ) from err
