"""Tests for the reminder, shift, and mood entities.

The reminder fixture is captured from a live instance and deliberately holds
one recurring reminder, one one-off, and one already overdue, so the overdue
and next-reminder logic is exercised against real payloads.
"""

from __future__ import annotations

from datetime import timedelta

from aioresponses import aioresponses
from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.einvault.const import CONF_ENABLE_MOOD_SENSOR

from .conftest import CINDY_ID, LILLY_ID, endpoint, load_fixture_json, mock_full_refresh
from .test_coordinator import _setup


async def test_due_reminders_count(hass: HomeAssistant, mock_config_entry: MockConfigEntry) -> None:
    """Cindy has two open reminders, Lilly one."""
    await _setup(hass, mock_config_entry)

    cindy = hass.states.get("sensor.cindy_due_reminders")
    assert cindy is not None
    assert cindy.state == "2"
    assert cindy.attributes["overdue"] == 1

    lilly = hass.states.get("sensor.lilly_due_reminders")
    assert lilly is not None
    assert lilly.state == "1"
    assert lilly.attributes["overdue"] == 0


async def test_next_reminder_picks_soonest(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """The overdue blood work is sooner than the 2027 flea collar."""
    await _setup(hass, mock_config_entry)

    state = hass.states.get("sensor.cindy_next_reminder")
    assert state is not None
    assert state.state == "2026-08-01T18:00:00+00:00"
    assert state.attributes["title"] == "Get Blood Work"
    assert state.attributes["type"] == "vet"
    assert state.attributes["is_recurring"] is False


async def test_reminder_overdue_binary_sensor(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Cindy's blood work is past due; Lilly's reminder is not."""
    await _setup(hass, mock_config_entry)

    cindy = hass.states.get("binary_sensor.cindy_reminder_overdue")
    assert cindy is not None
    assert cindy.state == STATE_ON
    assert cindy.attributes["titles"] == ["Get Blood Work"]
    assert cindy.attributes["device_class"] == "problem"

    lilly = hass.states.get("binary_sensor.lilly_reminder_overdue")
    assert lilly is not None
    assert lilly.state == STATE_OFF


async def test_completed_reminders_are_not_counted(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """A completed reminder is neither due nor overdue."""
    reminders = load_fixture_json("reminders.json")
    for reminder in reminders["reminders"]:
        reminder["completedAt"] = "2026-08-02T10:00:00.000Z"
        reminder["outcome"] = "completed"

    mock_config_entry.add_to_hass(hass)
    with aioresponses() as mocked:
        mocked.get(endpoint("/api/reminders"), payload=reminders, repeat=True)
        mock_full_refresh(mocked)
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert hass.states.get("sensor.cindy_due_reminders").state == "0"
    assert hass.states.get("binary_sensor.cindy_reminder_overdue").state == STATE_OFF
    assert hass.states.get("sensor.cindy_next_reminder").state == STATE_UNKNOWN


async def test_caretaker_on_shift_off_without_shifts(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """No caretaker users means no shifts, which is correct rather than broken."""
    await _setup(hass, mock_config_entry)

    state = hass.states.get("binary_sensor.192_168_1_246_7387_caretaker_on_shift")
    assert state is not None
    assert state.state == STATE_OFF
    assert state.attributes["caretakers"] == []


async def test_caretaker_on_shift_resolves_display_name(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Shift carries only a userId, so the name comes from a roster join."""
    shifts = {
        "shifts": [
            {
                "id": "shift-1",
                "userId": "4gy73h3nxgljlt7",
                "startAt": "2026-08-02T00:00:00.000Z",
                "endAt": "2026-08-03T23:59:00.000Z",
                "notes": "Weekend cover",
            }
        ],
        "hasMore": False,
    }

    mock_config_entry.add_to_hass(hass)
    with aioresponses() as mocked:
        mocked.get(endpoint("/api/shifts"), payload=shifts, repeat=True)
        mock_full_refresh(mocked)
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    state = hass.states.get("binary_sensor.192_168_1_246_7387_caretaker_on_shift")
    assert state is not None
    assert state.state == STATE_ON
    # users.json maps this id to the scrubbed display name.
    assert state.attributes["caretakers"] == ["Alex Doe"]


async def test_mood_sensor_absent_by_default(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Opt-in, because it costs one extra request per companion per refresh."""
    coordinator = await _setup(hass, mock_config_entry)

    assert hass.states.get("sensor.cindy_mood_today") is None
    assert coordinator.data.calls_last_refresh == 9  # initial refresh incl. slow set


async def test_mood_sensor_when_enabled(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Enabling it adds the entity and N journal calls per refresh."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, options={CONF_ENABLE_MOOD_SENSOR: True}
    )

    with aioresponses() as mocked:
        mocked.get(
            endpoint("/api/journal"),
            payload=load_fixture_json("journal_entry.json"),
            repeat=True,
        )
        mock_full_refresh(mocked)
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    state = hass.states.get("sensor.cindy_mood_today")
    assert state is not None
    assert state.state == "great"
    assert state.attributes["has_body"] is True


async def test_mood_sensor_costs_extra_calls(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """Budget goes from 6 to 8 with two companions when mood is enabled."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, options={CONF_ENABLE_MOOD_SENSOR: True}
    )

    with aioresponses() as mocked:
        mocked.get(
            endpoint("/api/journal"),
            payload=load_fixture_json("journal_absent.json"),
            repeat=True,
        )
        mock_full_refresh(mocked)
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        coordinator = mock_config_entry.runtime_data.coordinator
        before = coordinator.client.request_count
        freezer.tick(timedelta(seconds=300))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    assert coordinator.client.request_count - before == 8
    assert hass.states.get("sensor.cindy_mood_today").state == STATE_UNKNOWN


async def test_reminders_belong_to_the_right_companion(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Reminders come back for every companion in one call, so filtering matters."""
    coordinator = await _setup(hass, mock_config_entry)

    by_companion: dict[str, list[str]] = {}
    for reminder in coordinator.data.reminders:
        by_companion.setdefault(reminder.companion_id, []).append(reminder.title)

    assert sorted(by_companion[CINDY_ID]) == ["Change Flea Collar", "Get Blood Work"]
    assert by_companion[LILLY_ID] == ["File Property Taxes for Dog"]
