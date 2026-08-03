"""Sensor platform for EinVault."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfMass
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import EinVaultConfigEntry
from .const import JOURNAL_MOODS
from .coordinator import EinVaultDataUpdateCoordinator
from .entity import EinVaultCompanionEntity, EinVaultInstanceEntity
from .models import Companion, LogEvent, Reminder

# The coordinator owns all polling and the client serialises every request
# through a semaphore, so Home Assistant does not need to throttle this
# platform on top of that.
PARALLEL_UPDATES = 0

# `other` is deliberately absent: a generic "something happened" timestamp is
# not a useful sensor, and it would collide with the more specific ones.
LAST_EVENT_TYPES: tuple[str, ...] = (
    "walk",
    "meal",
    "bathroom",
    "play",
    "grooming",
    "treat",
)

UNIT_MAP: dict[str, str] = {
    "kg": UnitOfMass.KILOGRAMS,
    "lbs": UnitOfMass.POUNDS,
}


@dataclass(frozen=True, kw_only=True)
class EinVaultLastEventDescription(SensorEntityDescription):
    """Describes a `last <activity>` timestamp sensor."""

    event_type: str


LAST_EVENT_DESCRIPTIONS: tuple[EinVaultLastEventDescription, ...] = tuple(
    EinVaultLastEventDescription(
        key=f"last_{event_type}",
        translation_key=f"last_{event_type}",
        device_class=SensorDeviceClass.TIMESTAMP,
        event_type=event_type,
    )
    for event_type in LAST_EVENT_TYPES
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EinVaultConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EinVault sensors."""
    coordinator = entry.runtime_data.coordinator
    known: set[str] = set()

    @callback
    def _async_add_companion_sensors() -> None:
        """Create sensors for companions we have not seen yet.

        Companions refresh on the hourly slow timer, so a pet added in
        EinVault should appear on its own rather than requiring a reload.
        """
        new = [
            companion
            for companion_id, companion in coordinator.data.companions.items()
            if companion_id not in known
        ]
        if not new:
            return

        known.update(companion.id for companion in new)

        entities: list[SensorEntity] = []
        for companion in new:
            entities.extend(
                EinVaultLastEventSensor(coordinator, companion, description)
                for description in LAST_EVENT_DESCRIPTIONS
            )
            entities.append(EinVaultWeightSensor(coordinator, companion))
            entities.append(EinVaultDueRemindersSensor(coordinator, companion))
            entities.append(EinVaultNextReminderSensor(coordinator, companion))
            if coordinator.mood_sensor_enabled:
                entities.append(EinVaultMoodSensor(coordinator, companion))
        async_add_entities(entities)

    async_add_entities([EinVaultApiCallsSensor(coordinator)])
    _async_add_companion_sensors()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_companion_sensors))


class EinVaultLastEventSensor(EinVaultCompanionEntity, SensorEntity):
    """Timestamp of a companion's most recent event of one type."""

    entity_description: EinVaultLastEventDescription

    def __init__(
        self,
        coordinator: EinVaultDataUpdateCoordinator,
        companion: Companion,
        description: EinVaultLastEventDescription,
    ) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator, companion, description.key)
        self.entity_description = description

    @property
    def _event(self) -> LogEvent | None:
        """The newest event of this sensor's type, if any."""
        return self.coordinator.data.latest_events.get(self._companion_id, {}).get(
            self.entity_description.event_type
        )

    @property
    def native_value(self) -> datetime | None:
        """Return when the event was logged."""
        event = self._event
        return event.logged_at if event else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return details of the event.

        Kept small on purpose — the whole event list would bloat the state
        machine and the recorder database for no benefit.
        """
        event = self._event
        if event is None:
            return None
        return {
            "subtypes": list(event.subtypes),
            "duration_minutes": event.duration_minutes,
            "notes": event.notes,
        }


class EinVaultWeightSensor(EinVaultCompanionEntity, SensorEntity):
    """A companion's most recent recorded weight."""

    _attr_device_class = SensorDeviceClass.WEIGHT
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: EinVaultDataUpdateCoordinator, companion: Companion) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator, companion, "latest_weight")

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return the unit.

        The companion's configured ``weightUnit`` is authoritative, but it is
        nullable — so fall back to the unit recorded on the entry itself,
        which every weight entry always carries.
        """
        companion = self.companion
        if companion is not None and companion.weight_unit in UNIT_MAP:
            return UNIT_MAP[companion.weight_unit]

        entry = self.coordinator.data.latest_weight.get(self._companion_id)
        if entry is not None and entry.unit in UNIT_MAP:
            return UNIT_MAP[entry.unit]

        return None

    @property
    def native_value(self) -> float | None:
        """Return the most recent weight."""
        entry = self.coordinator.data.latest_weight.get(self._companion_id)
        return entry.weight if entry else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return when the weight was recorded, and any note."""
        entry = self.coordinator.data.latest_weight.get(self._companion_id)
        if entry is None:
            return None
        return {
            "recorded_at": entry.recorded_at.isoformat() if entry.recorded_at else None,
            "notes": entry.notes,
        }


class EinVaultApiCallsSensor(EinVaultInstanceEntity, SensorEntity):
    """How many API calls the last refresh cost.

    Exists so the request budget is observable rather than assumed. With two
    companions a normal refresh should read 6; a jump means something is
    fetching more than it should, and the 30-per-minute ceiling is close.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "calls"
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: EinVaultDataUpdateCoordinator) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator, "api_calls_last_refresh")

    @property
    def native_value(self) -> int:
        """Return the call count from the most recent refresh."""
        return self.coordinator.data.calls_last_refresh


class EinVaultDueRemindersSensor(EinVaultCompanionEntity, SensorEntity):
    """How many of a companion's reminders are still outstanding."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "reminders"

    def __init__(self, coordinator: EinVaultDataUpdateCoordinator, companion: Companion) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator, companion, "due_reminders")

    def _open_reminders(self) -> list[Reminder]:
        """Outstanding reminders for this companion."""
        return [
            reminder
            for reminder in self.coordinator.data.reminders
            if reminder.companion_id == self._companion_id and reminder.is_open
        ]

    @property
    def native_value(self) -> int:
        """Return the count of open reminders."""
        return len(self._open_reminders())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return how many of those are already past due."""
        now = dt_util.utcnow()
        return {
            "overdue": sum(r.is_overdue(now) for r in self._open_reminders()),
        }


class EinVaultNextReminderSensor(EinVaultCompanionEntity, SensorEntity):
    """When a companion's soonest outstanding reminder falls due."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: EinVaultDataUpdateCoordinator, companion: Companion) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator, companion, "next_reminder")

    def _next(self) -> Reminder | None:
        """Return the soonest open reminder that has a due date."""
        candidates = [
            reminder
            for reminder in self.coordinator.data.reminders
            if reminder.companion_id == self._companion_id
            and reminder.is_open
            and reminder.due_at is not None
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda r: r.due_at)  # type: ignore[arg-type,return-value]

    @property
    def native_value(self) -> datetime | None:
        """Return the soonest due date."""
        reminder = self._next()
        return reminder.due_at if reminder else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return what the reminder actually is."""
        reminder = self._next()
        if reminder is None:
            return None
        return {
            "title": reminder.title,
            "type": reminder.type,
            "description": reminder.description,
            "is_recurring": reminder.is_recurring,
            "reminder_id": reminder.id,
        }


class EinVaultMoodSensor(EinVaultCompanionEntity, SensorEntity):
    """Today's journal mood for a companion.

    Opt-in: it costs one extra API request per companion per refresh, because
    ``GET /api/journal`` reads a single companion for a single day and has no
    bulk form.
    """

    _attr_device_class = SensorDeviceClass.ENUM
    # RUF012 wants ClassVar, but SensorEntity declares _attr_options as an
    # instance variable, so annotating it that way fails mypy instead.
    _attr_options = list(JOURNAL_MOODS)  # noqa: RUF012

    def __init__(self, coordinator: EinVaultDataUpdateCoordinator, companion: Companion) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator, companion, "today_mood")

    @property
    def native_value(self) -> str | None:
        """Return today's mood, if one was recorded."""
        entry = self.coordinator.data.journals.get(self._companion_id)
        if entry is None or entry.mood not in JOURNAL_MOODS:
            return None
        return entry.mood

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return whether a journal body exists, without copying it wholesale."""
        entry = self.coordinator.data.journals.get(self._companion_id)
        if entry is None:
            return None
        return {
            "has_body": bool(entry.body),
            "updated_at": entry.updated_at.isoformat() if entry.updated_at else None,
        }
