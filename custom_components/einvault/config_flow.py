"""Config, reauth, and options flows for EinVault."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import voluptuous as vol

from .api import (
    EinVaultApiDisabledError,
    EinVaultAuthError,
    EinVaultClient,
    EinVaultConnectionError,
    EinVaultForbiddenError,
    EinVaultRateLimitError,
    normalize_base_url,
)
from .const import (
    CONF_API_TOKEN,
    CONF_ENABLE_MOOD_SENSOR,
    CONF_INCLUDE_ARCHIVED,
    CONF_SCAN_INTERVAL,
    CONF_URL,
    DEFAULT_ENABLE_MOOD_SENSOR,
    DEFAULT_INCLUDE_ARCHIVED,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MIN_SCAN_INTERVAL,
)
from .models import TokenScope

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_URL): TextSelector(TextSelectorConfig(type=TextSelectorType.URL)),
        vol.Required(CONF_API_TOKEN): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)

STEP_REAUTH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_API_TOKEN): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)


async def _async_validate(hass: Any, url: str, token: str) -> tuple[str, str]:
    """Validate a URL and token pair.

    Returns the normalized base URL and the instance title.

    Raises one of the flow-level exceptions below, which the caller maps to a
    form error key.
    """
    normalized = normalize_base_url(url)
    session = async_get_clientsession(hass)
    client = EinVaultClient(normalized, token, session)

    # 1. Reachability, unauthenticated. Distinguishes "wrong address" from
    #    "wrong token" before the token is ever sent.
    await client.async_get_health()

    # 2. Authenticated call, which also reveals the token's access level.
    #    GET /api/companions is the only endpoint a write-only token can read.
    scope, companions = await client.async_detect_token_scope()

    if scope is TokenScope.WRITE_ONLY:
        raise _WriteOnlyTokenError

    _LOGGER.debug(
        "Validated EinVault at %s: %d companion(s), scope=%s",
        normalized,
        len(companions),
        scope,
    )
    return normalized, urlsplit_host(normalized)


def urlsplit_host(url: str) -> str:
    """Derive a human-readable title from a normalized base URL."""
    return url.split("://", 1)[-1]


class _WriteOnlyTokenError(Exception):
    """The token has write-only access and cannot read any data."""


class EinVaultConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the EinVault config flow."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise flow state."""
        self._reauth_entry: ConfigEntry | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                normalized, title = await _async_validate(
                    self.hass, user_input[CONF_URL], user_input[CONF_API_TOKEN]
                )
            except ValueError:
                errors["base"] = "invalid_url"
            except EinVaultConnectionError:
                errors["base"] = "cannot_connect"
            except EinVaultApiDisabledError:
                errors["base"] = "api_disabled"
            except EinVaultAuthError:
                errors["base"] = "invalid_auth"
            except EinVaultRateLimitError:
                errors["base"] = "rate_limited"
            except _WriteOnlyTokenError:
                errors["base"] = "write_only_token"
            except EinVaultForbiddenError:
                errors["base"] = "insufficient_permissions"
            except Exception:
                _LOGGER.exception("Unexpected error validating EinVault")
                errors["base"] = "unknown"
            else:
                # There is no instance-id endpoint, so the normalized base URL
                # is the most stable identifier available.
                await self.async_set_unique_id(normalized)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=title,
                    data={CONF_URL: normalized, CONF_API_TOKEN: user_input[CONF_API_TOKEN]},
                )

        return self.async_show_form(step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Start reauth after a token is rotated or revoked."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect a replacement token."""
        errors: dict[str, str] = {}
        entry = self._reauth_entry
        assert entry is not None

        if user_input is not None:
            try:
                await _async_validate(self.hass, entry.data[CONF_URL], user_input[CONF_API_TOKEN])
            except ValueError:
                errors["base"] = "invalid_url"
            except EinVaultConnectionError:
                errors["base"] = "cannot_connect"
            except EinVaultApiDisabledError:
                errors["base"] = "api_disabled"
            except EinVaultAuthError:
                errors["base"] = "invalid_auth"
            except EinVaultRateLimitError:
                errors["base"] = "rate_limited"
            except _WriteOnlyTokenError:
                errors["base"] = "write_only_token"
            except Exception:
                _LOGGER.exception("Unexpected error during EinVault reauth")
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    data={**entry.data, CONF_API_TOKEN: user_input[CONF_API_TOKEN]},
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_SCHEMA,
            errors=errors,
            description_placeholders={"url": entry.data[CONF_URL]},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> EinVaultOptionsFlow:
        """Return the options flow."""
        return EinVaultOptionsFlow()


class EinVaultOptionsFlow(OptionsFlow):
    """Handle EinVault options."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SCAN_INTERVAL,
                    default=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_SCAN_INTERVAL,
                        max=86400,
                        step=30,
                        unit_of_measurement="s",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_ENABLE_MOOD_SENSOR,
                    default=options.get(CONF_ENABLE_MOOD_SENSOR, DEFAULT_ENABLE_MOOD_SENSOR),
                ): BooleanSelector(),
                vol.Required(
                    CONF_INCLUDE_ARCHIVED,
                    default=options.get(CONF_INCLUDE_ARCHIVED, DEFAULT_INCLUDE_ARCHIVED),
                ): BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
