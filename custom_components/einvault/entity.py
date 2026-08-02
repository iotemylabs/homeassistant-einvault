"""Base entities and the device model for EinVault.

Two device kinds:

* one **service device** per config entry, representing the EinVault instance
  itself, which holds instance-scoped entities (shift status, diagnostics,
  quick logs spanning multiple companions);
* one **companion device** per pet, linked back to the service device via
  ``via_device``.

Unique ids are derived from the config entry id and the companion id, never
from a name — companions can be renamed in EinVault and entity ids must not
move when they are.
"""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EinVaultDataUpdateCoordinator
from .models import Companion

MANUFACTURER = "EinVault"


class EinVaultEntity(CoordinatorEntity[EinVaultDataUpdateCoordinator]):
    """Base for every EinVault entity."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: EinVaultDataUpdateCoordinator, key: str) -> None:
        """Initialise shared entity state."""
        super().__init__(coordinator)
        self._key = key
        self._entry_id = coordinator.config_entry.entry_id


class EinVaultInstanceEntity(EinVaultEntity):
    """An entity describing the instance rather than a single companion."""

    def __init__(self, coordinator: EinVaultDataUpdateCoordinator, key: str) -> None:
        """Attach the entity to the service device."""
        super().__init__(coordinator, key)
        self._attr_unique_id = f"{self._entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._entry_id)},
            name=coordinator.config_entry.title,
            manufacturer=MANUFACTURER,
            model="Instance",
            configuration_url=coordinator.client.base_url,
        )


class EinVaultCompanionEntity(EinVaultEntity):
    """An entity belonging to one companion."""

    def __init__(
        self,
        coordinator: EinVaultDataUpdateCoordinator,
        companion: Companion,
        key: str,
    ) -> None:
        """Attach the entity to a companion device."""
        super().__init__(coordinator, key)
        self._companion_id = companion.id
        self._attr_unique_id = f"{self._entry_id}_{companion.id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{self._entry_id}_{companion.id}")},
            name=companion.name,
            manufacturer=MANUFACTURER,
            model="Companion",
            # `species` is never used here: it reads "dog" for every companion
            # regardless of the animal, so it would be actively misleading.
            model_id=companion.breed or None,
            via_device=(DOMAIN, self._entry_id),
        )

    @property
    def companion(self) -> Companion | None:
        """The companion this entity belongs to, if still present."""
        return self.coordinator.data.companions.get(self._companion_id)

    @property
    def available(self) -> bool:
        """Whether the entity has usable data.

        A companion archived or removed upstream disappears from the payload;
        its entities go unavailable rather than freezing on a stale value.
        """
        return super().available and self.companion is not None
