"""Typed models for EinVault API payloads.

Pure dataclasses with no Home Assistant or aiohttp dependency, so they can be
exercised in isolation. Every ``from_api`` constructor is tolerant of the
nullability the OpenAPI document declares — several fields that read as
"always present" in the prose are genuinely ``null`` on a live instance
(``subtypes`` being the notable one).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any, Generic, TypeVar

_T = TypeVar("_T")


def parse_timestamp(value: Any) -> datetime | None:
    """Parse an ISO 8601 timestamp into a timezone-aware datetime.

    The server serializes UTC with a trailing ``Z``. Anything unparseable
    yields ``None`` rather than raising — a single malformed timestamp should
    degrade one entity, not fail an entire refresh.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def parse_date(value: Any) -> date | None:
    """Parse a ``YYYY-MM-DD`` string, tolerating nulls and junk."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


class TokenScope(StrEnum):
    """Access level of the configured API token.

    ``WRITE_ONLY`` tokens can reach ``GET /api/companions`` but every other
    read endpoint answers ``403 writeScopeReadOnly``, so they cannot populate
    any entity. The config flow refuses them.
    """

    FULL = "full"
    WRITE_ONLY = "write_only"


@dataclass(frozen=True, slots=True)
class ListPage(Generic[_T]):
    """A paginated list response plus its ``hasMore`` flag."""

    items: list[_T]
    has_more: bool


@dataclass(frozen=True, slots=True)
class Companion:
    """A companion (pet).

    ``species`` is deliberately **not** exposed. Upstream declares it
    ``string | null`` and it reads ``"dog"`` for every companion regardless of
    what the animal actually is — this instance's cat included. It is never
    used for icons, device classes, naming, or filtering.
    """

    id: str
    name: str
    is_active: bool
    breed: str | None = None
    dob: date | None = None
    sex: str | None = None
    weight_unit: str | None = None
    microchip: str | None = None
    bio: str | None = None
    feeding_schedule: str | None = None
    walk_schedule: str | None = None
    medication_schedule: str | None = None
    emergency_contact_name: str | None = None
    emergency_contact_phone: str | None = None
    vet_name: str | None = None
    vet_phone: str | None = None
    vet_clinic: str | None = None
    notes_for_sitter: str | None = None
    archived_at: datetime | None = None
    archive_note: str | None = None
    created_at: datetime | None = None

    @property
    def is_archived(self) -> bool:
        """Whether the companion has been archived upstream."""
        return self.archived_at is not None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Companion:
        """Build from a ``Companion`` payload."""
        return cls(
            id=str(data["id"]),
            name=str(data["name"]),
            is_active=bool(data.get("isActive", True)),
            breed=data.get("breed"),
            dob=parse_date(data.get("dob")),
            sex=data.get("sex"),
            weight_unit=data.get("weightUnit"),
            microchip=data.get("microchip"),
            bio=data.get("bio"),
            feeding_schedule=data.get("feedingSchedule"),
            walk_schedule=data.get("walkSchedule"),
            medication_schedule=data.get("medicationSchedule"),
            emergency_contact_name=data.get("emergencyContactName"),
            emergency_contact_phone=data.get("emergencyContactPhone"),
            vet_name=data.get("vetName"),
            vet_phone=data.get("vetPhone"),
            vet_clinic=data.get("vetClinic"),
            notes_for_sitter=data.get("notesForSitter"),
            archived_at=parse_timestamp(data.get("archivedAt")),
            archive_note=data.get("archiveNote"),
            created_at=parse_timestamp(data.get("createdAt")),
        )


# Keys present only on a full-scope ``GET /api/companions`` response. A
# write-only token receives id/name/species/isActive and nothing else, so the
# presence of any of these is what distinguishes the two access levels.
FULL_SCOPE_MARKER_KEYS: frozenset[str] = frozenset(
    {
        "breed",
        "dob",
        "sex",
        "weightUnit",
        "microchip",
        "bio",
        "createdAt",
    }
)


@dataclass(frozen=True, slots=True)
class LogEvent:
    """A logged daily event."""

    id: str
    companion_id: str
    type: str
    logged_at: datetime | None
    notes: str | None = None
    duration_minutes: int | None = None
    subtypes: tuple[str, ...] = ()
    event_group_id: str | None = None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> LogEvent:
        """Build from a ``LoggedEvent`` payload.

        ``subtypes`` arrives as ``null`` (not ``[]``) when unset, which is why
        it is normalised to an empty tuple here.
        """
        raw_subtypes = data.get("subtypes") or []
        return cls(
            id=str(data["id"]),
            companion_id=str(data["companionId"]),
            type=str(data["type"]),
            logged_at=parse_timestamp(data.get("loggedAt")),
            notes=data.get("notes"),
            duration_minutes=data.get("durationMinutes"),
            subtypes=tuple(str(s) for s in raw_subtypes),
            event_group_id=data.get("eventGroupId"),
        )


@dataclass(frozen=True, slots=True)
class WeightEntry:
    """A weight measurement."""

    id: str
    companion_id: str
    weight: float
    unit: str
    recorded_at: datetime | None
    notes: str | None = None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> WeightEntry:
        """Build from a ``WeightEntry`` payload."""
        return cls(
            id=str(data["id"]),
            companion_id=str(data["companionId"]),
            weight=float(data["weight"]),
            unit=str(data["unit"]),
            recorded_at=parse_timestamp(data.get("recordedAt")),
            notes=data.get("notes"),
        )


@dataclass(frozen=True, slots=True)
class HealthEvent:
    """A health event."""

    id: str
    companion_id: str
    type: str
    title: str
    occurred_at: datetime | None
    notes: str | None = None
    vet_name: str | None = None
    vet_clinic: str | None = None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> HealthEvent:
        """Build from a ``HealthEvent`` payload."""
        return cls(
            id=str(data["id"]),
            companion_id=str(data["companionId"]),
            type=str(data["type"]),
            title=str(data["title"]),
            occurred_at=parse_timestamp(data.get("occurredAt")),
            notes=data.get("notes"),
            vet_name=data.get("vetName"),
            vet_clinic=data.get("vetClinic"),
        )


@dataclass(frozen=True, slots=True)
class Reminder:
    """A reminder.

    ``companion_id`` is always present and single-valued; there is no
    multi-companion reminder in the schema.
    """

    id: str
    companion_id: str
    title: str
    type: str
    due_at: datetime | None
    is_recurring: bool
    description: str | None = None
    completed_at: datetime | None = None
    outcome: str | None = None
    series_id: str | None = None

    @property
    def is_open(self) -> bool:
        """Whether the reminder is still outstanding."""
        return self.completed_at is None

    def is_overdue(self, now: datetime) -> bool:
        """Whether the reminder is open and past due."""
        return self.is_open and self.due_at is not None and self.due_at < now

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Reminder:
        """Build from a ``Reminder`` payload."""
        return cls(
            id=str(data["id"]),
            companion_id=str(data["companionId"]),
            title=str(data["title"]),
            type=str(data["type"]),
            due_at=parse_timestamp(data.get("dueAt")),
            is_recurring=bool(data.get("isRecurring", False)),
            description=data.get("description"),
            completed_at=parse_timestamp(data.get("completedAt")),
            outcome=data.get("outcome"),
            series_id=data.get("seriesId"),
        )


@dataclass(frozen=True, slots=True)
class Shift:
    """A caretaker shift.

    Carries no companion association and no display name — resolving the
    caretaker requires a client-side join against ``GET /api/users``.
    """

    id: str
    user_id: str
    start_at: datetime | None
    end_at: datetime | None
    notes: str | None = None

    def is_active(self, now: datetime) -> bool:
        """Whether ``now`` falls inside the shift window."""
        if self.start_at is None or self.end_at is None:
            return False
        return self.start_at <= now <= self.end_at

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Shift:
        """Build from a ``Shift`` payload."""
        return cls(
            id=str(data["id"]),
            user_id=str(data["userId"]),
            start_at=parse_timestamp(data.get("startAt")),
            end_at=parse_timestamp(data.get("endAt")),
            notes=data.get("notes"),
        )


@dataclass(frozen=True, slots=True)
class User:
    """A user in the roster.

    ``username`` is omitted for member-scoped tokens, so ``display_name`` is
    the only field safe to rely on for presentation.
    """

    id: str
    display_name: str
    role: str
    is_active: bool
    username: str | None = None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> User:
        """Build from a ``User`` payload."""
        return cls(
            id=str(data["id"]),
            display_name=str(data["displayName"]),
            role=str(data["role"]),
            is_active=bool(data.get("isActive", True)),
            username=data.get("username"),
        )


@dataclass(frozen=True, slots=True)
class QuickLog:
    """A user-configured quick log.

    Note the singular ``note`` field name upstream — not ``notes``.
    """

    id: str
    name: str
    type: str
    companion_ids: tuple[str, ...]
    subtypes: tuple[str, ...] = ()
    duration_minutes: int | None = None
    note: str | None = None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> QuickLog:
        """Build from a ``QuickLog`` payload."""
        return cls(
            id=str(data["id"]),
            name=str(data["name"]),
            type=str(data["type"]),
            companion_ids=tuple(str(c) for c in data.get("companionIds") or []),
            subtypes=tuple(str(s) for s in data.get("subtypes") or []),
            duration_minutes=data.get("durationMinutes"),
            note=data.get("note"),
        )


@dataclass(frozen=True, slots=True)
class JournalEntry:
    """A journal entry for one companion on one day."""

    id: str
    companion_id: str
    entry_date: date | None
    body: str | None = None
    mood: str | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> JournalEntry:
        """Build from a ``JournalEntry`` payload."""
        return cls(
            id=str(data["id"]),
            companion_id=str(data["companionId"]),
            entry_date=parse_date(data.get("date")),
            body=data.get("body"),
            mood=data.get("mood"),
            updated_at=parse_timestamp(data.get("updatedAt")),
        )


@dataclass(frozen=True, slots=True)
class InstanceHealth:
    """Response from the unauthenticated ``/api/health`` endpoint.

    Undocumented in the OpenAPI spec but present on every instance. A success
    proves reachability only — it does **not** prove the bearer API is enabled.
    """

    status: str
    timestamp: datetime | None = None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> InstanceHealth:
        """Build from a health payload."""
        return cls(
            status=str(data.get("status", "unknown")),
            timestamp=parse_timestamp(data.get("timestamp")),
        )


@dataclass(frozen=True, slots=True)
class WriteResult:
    """Identifiers returned by a write endpoint."""

    ids: tuple[str, ...] = ()
    event_group_id: str | None = None
    next_reminder_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
