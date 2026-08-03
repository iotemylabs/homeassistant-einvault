"""Button platform for EinVault quick logs.

Each button runs one of the token user's quick logs with an **empty body**, so
every parameter — activity type, subtypes, duration, note, and target
companions — stays configured in EinVault rather than being duplicated in Home
Assistant.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import EinVaultConfigEntry
from .api import (
    EinVaultConflictError,
    EinVaultConnectionError,
    EinVaultForbiddenError,
    EinVaultNotFoundError,
    EinVaultResponseError,
)
from .const import DOMAIN, ERROR_NO_ACTIVE_SHIFT, ERROR_NOT_ASSIGNED
from .coordinator import EinVaultDataUpdateCoordinator
from .entity import EinVaultCompanionEntity, EinVaultEntity, EinVaultInstanceEntity
from .models import QuickLog

_LOGGER = logging.getLogger(__name__)

# Presses go through the same serialising client as everything else, so
# concurrent presses cannot burst the server's 30-request/60-second budget.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EinVaultConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up a button per quick log, tracking changes to the set."""
    coordinator = entry.runtime_data.coordinator
    known: set[str] = set()
    warned_empty = False

    @callback
    def _async_sync_buttons() -> None:
        """Add buttons for new quick logs.

        Quick logs refresh on the hourly slow timer, so the set can change
        under us. Adding entities dynamically avoids requiring a reload.
        """
        nonlocal warned_empty

        current = {quick_log.id: quick_log for quick_log in coordinator.data.quick_logs}

        if not current and not warned_empty:
            warned_empty = True
            _LOGGER.info(
                "No EinVault quick logs are visible to this API token, so no buttons "
                "were created. A quick log is only returned by the API when it belongs "
                "to the token's own user, is enabled, and has at least one companion "
                "attached — check Settings, Quick logs in EinVault"
            )

        new = [
            quick_log for quick_log_id, quick_log in current.items() if quick_log_id not in known
        ]
        if not new:
            return

        known.update(quick_log.id for quick_log in new)
        async_add_entities(_build_button(coordinator, quick_log) for quick_log in new)

    _async_sync_buttons()
    entry.async_on_unload(coordinator.async_add_listener(_async_sync_buttons))


def _build_button(coordinator: EinVaultDataUpdateCoordinator, quick_log: QuickLog) -> ButtonEntity:
    """Attach a quick log to a companion device when it targets exactly one.

    A quick log spanning several companions has no single home, so it goes on
    the instance's service device instead.
    """
    companion_ids = [
        companion_id
        for companion_id in quick_log.companion_ids
        if companion_id in coordinator.data.companions
    ]
    if len(companion_ids) == 1:
        companion = coordinator.data.companions[companion_ids[0]]
        return EinVaultCompanionQuickLogButton(coordinator, companion, quick_log)
    return EinVaultInstanceQuickLogButton(coordinator, quick_log)


async def _async_press(coordinator: EinVaultDataUpdateCoordinator, quick_log: QuickLog) -> None:
    """Execute a quick log, retrying once on a network failure.

    A fresh key is generated per press, so two deliberate presses both count.
    The retry reuses that key, so a press that actually reached the server but
    whose response was lost is replayed rather than duplicated — verified
    against a live instance: an identical body returns the original response.
    """
    idempotency_key = str(uuid4())
    max_attempts = 2

    for attempt in range(1, max_attempts + 1):
        try:
            await coordinator.client.async_execute_quick_log(
                quick_log.id, idempotency_key=idempotency_key
            )
        except EinVaultConnectionError:
            if attempt == max_attempts:
                raise
            _LOGGER.debug(
                "Quick log %s failed to reach the server; retrying with the same key",
                quick_log.id,
            )
            continue
        else:
            return


class EinVaultQuickLogButtonMixin(EinVaultEntity, ButtonEntity):
    """Press handling shared by both button flavours."""

    _quick_log: QuickLog

    async def async_press(self) -> None:
        """Run the quick log and refresh so sensors reflect it."""
        try:
            await _async_press(self.coordinator, self._quick_log)
        except EinVaultForbiddenError as err:
            if err.code == ERROR_NO_ACTIVE_SHIFT:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="no_active_shift"
                ) from err
            if err.code == ERROR_NOT_ASSIGNED:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="not_assigned"
                ) from err
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="quick_log_failed",
                translation_placeholders={"error": err.api_message or err.code},
            ) from err
        except EinVaultNotFoundError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="quick_log_not_found"
            ) from err
        except (EinVaultConflictError, EinVaultResponseError, EinVaultConnectionError) as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="quick_log_failed",
                translation_placeholders={"error": str(err)},
            ) from err

        await self.coordinator.async_request_refresh()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose what the quick log will do, since it is configured remotely."""
        return {
            "quick_log_id": self._quick_log.id,
            "activity_type": self._quick_log.type,
            "subtypes": list(self._quick_log.subtypes),
            "duration_minutes": self._quick_log.duration_minutes,
            "companion_count": len(self._quick_log.companion_ids),
        }


class EinVaultCompanionQuickLogButton(
    EinVaultCompanionEntity, EinVaultQuickLogButtonMixin, ButtonEntity
):
    """A quick log that targets exactly one companion."""

    _attr_translation_key = None

    def __init__(
        self,
        coordinator: EinVaultDataUpdateCoordinator,
        companion: Any,
        quick_log: QuickLog,
    ) -> None:
        """Initialise the button."""
        super().__init__(coordinator, companion, f"quick_log_{quick_log.id}")
        self._quick_log = quick_log
        # Named by the user in EinVault, so use it verbatim rather than a
        # translation key.
        self._attr_translation_key = None
        self._attr_name = quick_log.name


class EinVaultInstanceQuickLogButton(
    EinVaultInstanceEntity, EinVaultQuickLogButtonMixin, ButtonEntity
):
    """A quick log spanning several companions, or none we can resolve."""

    def __init__(self, coordinator: EinVaultDataUpdateCoordinator, quick_log: QuickLog) -> None:
        """Initialise the button."""
        super().__init__(coordinator, f"quick_log_{quick_log.id}")
        self._quick_log = quick_log
        self._attr_translation_key = None
        self._attr_name = quick_log.name
