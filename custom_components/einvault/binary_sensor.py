"""Binary sensor platform for EinVault."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import EinVaultConfigEntry
from .coordinator import EinVaultDataUpdateCoordinator
from .entity import EinVaultCompanionEntity, EinVaultInstanceEntity
from .models import Companion, Shift


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EinVaultConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EinVault binary sensors."""
    coordinator = entry.runtime_data.coordinator
    known: set[str] = set()

    @callback
    def _async_add_companion_binary_sensors() -> None:
        """Create per-companion binary sensors for newly seen companions."""
        new = [
            companion
            for companion_id, companion in coordinator.data.companions.items()
            if companion_id not in known
        ]
        if not new:
            return

        known.update(companion.id for companion in new)
        async_add_entities(
            EinVaultReminderOverdueSensor(coordinator, companion) for companion in new
        )

    async_add_entities([EinVaultCaretakerOnShiftSensor(coordinator)])
    _async_add_companion_binary_sensors()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_companion_binary_sensors))


class EinVaultReminderOverdueSensor(EinVaultCompanionEntity, BinarySensorEntity):
    """Whether a companion has any reminder past its due date."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: EinVaultDataUpdateCoordinator, companion: Companion) -> None:
        """Initialise the binary sensor."""
        super().__init__(coordinator, companion, "reminder_overdue")

    def _overdue(self) -> list[Any]:
        """Open reminders for this companion that are past due."""
        now = dt_util.utcnow()
        return [
            reminder
            for reminder in self.coordinator.data.reminders
            if reminder.companion_id == self._companion_id and reminder.is_overdue(now)
        ]

    @property
    def is_on(self) -> bool:
        """Return whether anything is overdue."""
        return bool(self._overdue())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Name what is overdue, so the state is actionable."""
        overdue = self._overdue()
        return {
            "overdue_count": len(overdue),
            "titles": [reminder.title for reminder in overdue],
        }


class EinVaultCaretakerOnShiftSensor(EinVaultInstanceEntity, BinarySensorEntity):
    """Whether a caretaker is currently on shift.

    Shifts exist only for users whose role is ``caretaker``, and can only be
    created from EinVault's Admin -> Users page. On an instance with no
    caretaker users this reads ``off`` permanently, which is correct rather
    than broken.
    """

    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY

    def __init__(self, coordinator: EinVaultDataUpdateCoordinator) -> None:
        """Initialise the binary sensor."""
        super().__init__(coordinator, "caretaker_on_shift")

    def _active_shifts(self) -> list[Shift]:
        """Shifts whose window contains the current moment."""
        now = dt_util.utcnow()
        return [shift for shift in self.coordinator.data.shifts if shift.is_active(now)]

    @property
    def is_on(self) -> bool:
        """Return whether anyone is on shift right now."""
        return bool(self._active_shifts())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Resolve who is on shift.

        ``Shift`` carries only a ``userId``, so the display name comes from a
        client-side join against the roster fetched on the slow timer.
        """
        active = self._active_shifts()
        names_by_id = {user.id: user.display_name for user in self.coordinator.data.users}

        return {
            "caretakers": [names_by_id.get(shift.user_id, shift.user_id) for shift in active],
            "shift_ends_at": min(
                (shift.end_at for shift in active if shift.end_at is not None),
                default=None,
            ),
        }
