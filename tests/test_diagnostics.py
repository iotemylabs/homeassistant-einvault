"""Tests for diagnostics redaction.

A diagnostics download is the thing users paste into public issue trackers, so
these assertions are about what must *not* appear in it.
"""

from __future__ import annotations

import re
from typing import Any

from aioresponses import aioresponses
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.einvault.const import CONF_CALENDAR_FEED_URL
from custom_components.einvault.diagnostics import async_get_config_entry_diagnostics

from .conftest import BASE_URL, CINDY_ID, TOKEN, endpoint, load_fixture_json, mock_full_refresh

SENSITIVE_COMPANION = {
    "microchip": "985141000123456",
    "vetPhone": "+1-555-0100",
    "vetName": "Dr Rhonda Witt",
    "vetClinic": "Dunbar Animal Hospital",
    "emergencyContactName": "Jane Doe",
    "emergencyContactPhone": "+1-555-0199",
    "notesForSitter": "Spare key under the mat",
    "bio": "Nervous around men in hats",
    "feedingSchedule": "7am and 6pm",
}


async def _diagnostics(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, Any]:
    companions = load_fixture_json("companions.json")
    companions["companions"][0].update(SENSITIVE_COMPANION)

    entry.add_to_hass(hass)
    with aioresponses() as mocked:
        mocked.get(endpoint("/api/companions"), payload=companions, repeat=True)
        mock_full_refresh(mocked)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    return await async_get_config_entry_diagnostics(hass, entry)


async def test_credentials_are_redacted(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Neither the API token nor the calendar feed URL may appear."""
    feed_url = f"{BASE_URL}/api/calendar/super-secret-feed-token/feed.ics"
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, options={CONF_CALENDAR_FEED_URL: feed_url}
    )

    with aioresponses() as mocked:
        mocked.get(
            re.compile(r"^.*/feed\.ics(\?.*)?$"),
            body="BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n",
            content_type="text/calendar",
            repeat=True,
        )
        mock_full_refresh(mocked)
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    result = await async_get_config_entry_diagnostics(hass, mock_config_entry)
    dumped = str(result)

    assert TOKEN not in dumped
    assert "super-secret-feed-token" not in dumped
    assert result["entry"]["data"]["api_token"] == "**REDACTED**"
    assert result["entry"]["options"][CONF_CALENDAR_FEED_URL] == "**REDACTED**"


@pytest.mark.parametrize(
    "secret",
    [
        "985141000123456",
        "+1-555-0100",
        "+1-555-0199",
        "Jane Doe",
        "Spare key under the mat",
        "Nervous around men in hats",
    ],
)
async def test_companion_personal_data_is_redacted(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    secret: str,
) -> None:
    """Microchip, vet, emergency contact, and sitter notes must not leak."""
    result = await _diagnostics(hass, mock_config_entry)
    assert secret not in str(result)


async def test_diagnostics_still_useful(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Redaction must not gut the parts that make diagnostics worth having."""
    result = await _diagnostics(hass, mock_config_entry)

    assert result["instance"]["base_url"] == BASE_URL
    assert result["coordinator"]["last_update_success"] is True
    assert result["coordinator"]["calls_last_refresh"] == 9
    assert result["coordinator"]["mood_sensor_enabled"] is False

    companions = result["data"]["companions"]
    assert any(c["name"] == "Cindy" for c in companions)
    assert any(c["id"] == CINDY_ID for c in companions)

    # Reminders and events survive intact — they carry no personal data.
    assert len(result["data"]["reminders"]) == 3
    assert result["data"]["latest_events"][CINDY_ID]["walk"]["subtypes"] == ["leash"]


async def test_user_names_are_redacted(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """The roster is other people's names."""
    result = await _diagnostics(hass, mock_config_entry)
    assert "Alex Doe" not in str(result)
    assert result["data"]["users"][0]["role"] == "admin"
