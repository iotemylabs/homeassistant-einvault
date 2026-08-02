"""Tests for the EinVault config, reauth, and options flows."""

from __future__ import annotations

from typing import Any

from aioresponses import aioresponses
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.einvault.const import (
    CONF_API_TOKEN,
    CONF_ENABLE_MOOD_SENSOR,
    CONF_INCLUDE_ARCHIVED,
    CONF_SCAN_INTERVAL,
    CONF_URL,
    DOMAIN,
)

from .conftest import BASE_URL, TOKEN, load_fixture_json

USER_INPUT = {CONF_URL: BASE_URL, CONF_API_TOKEN: TOKEN}


def _mock_health(mocked: aioresponses, **kwargs: Any) -> None:
    """Register the unauthenticated health pre-check."""
    if kwargs:
        mocked.get(f"{BASE_URL}/api/health", **kwargs)
    else:
        mocked.get(f"{BASE_URL}/api/health", payload=load_fixture_json("health.json"))


async def test_user_flow_success(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    with aioresponses() as mocked:
        _mock_health(mocked)
        mocked.get(f"{BASE_URL}/api/companions", payload=load_fixture_json("companions.json"))
        result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "192.168.1.246:7387"
    assert result["data"] == {CONF_URL: BASE_URL, CONF_API_TOKEN: TOKEN}
    assert result["result"].unique_id == BASE_URL


async def test_user_flow_cannot_connect(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})

    with aioresponses() as mocked:
        _mock_health(mocked, exception=TimeoutError())
        result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_invalid_auth(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})

    with aioresponses() as mocked:
        _mock_health(mocked)
        mocked.get(
            f"{BASE_URL}/api/companions",
            status=401,
            payload=load_fixture_json("error_invalid_token.json"),
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)

    assert result["errors"] == {"base": "invalid_auth"}


async def test_user_flow_api_disabled(hass: HomeAssistant) -> None:
    """API_TOKENS_ENABLED=false serves the SPA HTML, not a JSON 404."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})

    with aioresponses() as mocked:
        _mock_health(mocked)
        mocked.get(
            f"{BASE_URL}/api/companions",
            status=404,
            body="<!doctype html><html></html>",
            content_type="text/html",
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)

    assert result["errors"] == {"base": "api_disabled"}


async def test_user_flow_rate_limited(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})

    with aioresponses() as mocked:
        _mock_health(mocked)
        mocked.get(
            f"{BASE_URL}/api/companions",
            status=429,
            payload=load_fixture_json("error_rate_limited.json"),
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)

    assert result["errors"] == {"base": "rate_limited"}


async def test_user_flow_write_only_token_is_refused(hass: HomeAssistant) -> None:
    """A write-only token 403s on every read endpoint, so setup is pointless."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})

    with aioresponses() as mocked:
        _mock_health(mocked)
        mocked.get(
            f"{BASE_URL}/api/companions",
            payload=load_fixture_json("companions_write_only.json"),
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "write_only_token"}


async def test_user_flow_invalid_url(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: "ftp://nope", CONF_API_TOKEN: TOKEN}
    )

    assert result["errors"] == {"base": "invalid_url"}


async def test_duplicate_entry_aborts(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """A trailing slash must still collide with the existing entry."""
    mock_config_entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})

    with aioresponses() as mocked:
        _mock_health(mocked)
        mocked.get(f"{BASE_URL}/api/companions", payload=load_fixture_json("companions.json"))
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_URL: f"{BASE_URL}/", CONF_API_TOKEN: TOKEN},
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# --- Reauth ----------------------------------------------------------------


async def _start_reauth(hass: HomeAssistant, entry: MockConfigEntry) -> Any:
    return await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": SOURCE_REAUTH,
            "entry_id": entry.entry_id,
            "unique_id": entry.unique_id,
        },
        data=entry.data,
    )


async def test_reauth_success_updates_token(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """EinVault's Rotate feature revokes the old token, so this path matters."""
    mock_config_entry.add_to_hass(hass)
    result = await _start_reauth(hass, mock_config_entry)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    with aioresponses() as mocked:
        _mock_health(mocked)
        mocked.get(f"{BASE_URL}/api/companions", payload=load_fixture_json("companions.json"))
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_TOKEN: "evk_rotated_token"}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data[CONF_API_TOKEN] == "evk_rotated_token"


async def test_reauth_failure_shows_error(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    result = await _start_reauth(hass, mock_config_entry)

    with aioresponses() as mocked:
        _mock_health(mocked)
        mocked.get(
            f"{BASE_URL}/api/companions",
            status=401,
            payload=load_fixture_json("error_invalid_token.json"),
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_TOKEN: "still_wrong"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    assert mock_config_entry.data[CONF_API_TOKEN] == TOKEN


# --- Options ---------------------------------------------------------------


async def test_options_flow_saves(hass: HomeAssistant, mock_config_entry: MockConfigEntry) -> None:
    mock_config_entry.add_to_hass(hass)

    with aioresponses() as mocked:
        mocked.get(
            f"{BASE_URL}/api/companions",
            payload=load_fixture_json("companions.json"),
            repeat=True,
        )
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
        assert result["type"] is FlowResultType.FORM

        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_SCAN_INTERVAL: 600,
                CONF_ENABLE_MOOD_SENSOR: True,
                CONF_INCLUDE_ARCHIVED: False,
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_config_entry.options[CONF_SCAN_INTERVAL] == 600
    assert mock_config_entry.options[CONF_ENABLE_MOOD_SENSOR] is True


@pytest.mark.parametrize("bad_interval", [0, 30, 59])
async def test_options_flow_enforces_minimum_interval(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, bad_interval: int
) -> None:
    """The 60s floor protects the shared 30-request/60s budget."""
    mock_config_entry.add_to_hass(hass)

    with aioresponses() as mocked:
        mocked.get(
            f"{BASE_URL}/api/companions",
            payload=load_fixture_json("companions.json"),
            repeat=True,
        )
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
        with pytest.raises(Exception):  # noqa: B017 - voluptuous rejects the value
            await hass.config_entries.options.async_configure(
                result["flow_id"],
                {
                    CONF_SCAN_INTERVAL: bad_interval,
                    CONF_ENABLE_MOOD_SENSOR: False,
                    CONF_INCLUDE_ARCHIVED: False,
                },
            )
