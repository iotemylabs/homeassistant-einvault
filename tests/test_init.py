"""Tests for config entry setup and teardown."""

from __future__ import annotations

from aioresponses import aioresponses
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import BASE_URL, load_fixture_json


async def test_setup_and_unload(hass: HomeAssistant, mock_config_entry: MockConfigEntry) -> None:
    mock_config_entry.add_to_hass(hass)

    with aioresponses() as mocked:
        mocked.get(
            f"{BASE_URL}/api/companions",
            payload=load_fixture_json("companions.json"),
            repeat=True,
        )
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert mock_config_entry.runtime_data.client.base_url == BASE_URL

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED


async def test_setup_retries_when_unreachable(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)

    with aioresponses() as mocked:
        mocked.get(f"{BASE_URL}/api/companions", exception=TimeoutError(), repeat=True)
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_revoked_token_triggers_reauth(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """A rotated token must start reauth, not a silent retry loop."""
    mock_config_entry.add_to_hass(hass)

    with aioresponses() as mocked:
        mocked.get(
            f"{BASE_URL}/api/companions",
            status=401,
            payload=load_fixture_json("error_invalid_token.json"),
            repeat=True,
        )
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert any(flow["context"]["source"] == "reauth" for flow in flows)


async def test_api_disabled_retries(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)

    with aioresponses() as mocked:
        mocked.get(
            f"{BASE_URL}/api/companions",
            status=404,
            body="<!doctype html><html></html>",
            content_type="text/html",
            repeat=True,
        )
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
