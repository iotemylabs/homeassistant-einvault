"""The EinVault integration."""

from __future__ import annotations

from dataclasses import dataclass
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    EinVaultApiDisabledError,
    EinVaultAuthError,
    EinVaultClient,
    EinVaultConnectionError,
    EinVaultRateLimitError,
)
from .const import CONF_API_TOKEN, CONF_URL
from .coordinator import EinVaultDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


@dataclass
class EinVaultRuntimeData:
    """Objects shared across the entry's platforms."""

    client: EinVaultClient
    coordinator: EinVaultDataUpdateCoordinator


type EinVaultConfigEntry = ConfigEntry[EinVaultRuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: EinVaultConfigEntry) -> bool:
    """Set up EinVault from a config entry."""
    session = async_get_clientsession(hass)
    client = EinVaultClient(entry.data[CONF_URL], entry.data[CONF_API_TOKEN], session)
    coordinator = EinVaultDataUpdateCoordinator(hass, entry, client)

    # The first refresh doubles as the setup check: it proves the instance is
    # reachable, the bearer API is enabled, and the token is still valid.
    try:
        await coordinator.async_config_entry_first_refresh()
    except ConfigEntryAuthFailed:
        raise
    except EinVaultAuthError as err:
        raise ConfigEntryAuthFailed(
            "The EinVault API token was rejected. It may have been rotated or revoked."
        ) from err
    except EinVaultApiDisabledError as err:
        raise ConfigEntryNotReady(str(err)) from err
    except EinVaultRateLimitError as err:
        raise ConfigEntryNotReady(
            f"EinVault is rate limiting requests; retrying in {err.retry_after:.0f}s"
        ) from err
    except EinVaultConnectionError as err:
        raise ConfigEntryNotReady(str(err)) from err

    entry.runtime_data = EinVaultRuntimeData(client=client, coordinator=coordinator)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EinVaultConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(hass: HomeAssistant, entry: EinVaultConfigEntry) -> None:
    """Reload the entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)
