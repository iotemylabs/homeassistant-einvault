"""Snapshot coverage of the full entity and device registry.

Time is frozen deliberately. Several entities derive their state from "now" —
the overdue binary sensor, and the calendar's next-event attributes, which look
365 days ahead — so without a fixed clock these snapshots would quietly start
failing when a recurring reminder's next occurrence rolled over.

The fixture set here is the maximal one: two companions, reminders, quick logs
spanning one and both companions, a live shift, journals, and a calendar feed.
That way every platform is represented in a single registry snapshot.
"""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import patch

from aioresponses import aioresponses
from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, snapshot_platform
from syrupy.assertion import SnapshotAssertion

from custom_components.einvault.const import (
    CONF_CALENDAR_FEED_URL,
    CONF_ENABLE_MOOD_SENSOR,
)

from .conftest import BASE_URL, CINDY_ID, LILLY_ID, endpoint, load_fixture_json

FEED_URL = f"{BASE_URL}/api/calendar/snapshot-feed-token/feed.ics"

QUICK_LOGS = {
    "quickLogs": [
        {
            "id": "ql-evening-walk",
            "name": "Evening walk",
            "type": "walk",
            "durationMinutes": 30,
            "subtypes": ["leash"],
            "note": None,
            "companionIds": [CINDY_ID],
        },
        {
            "id": "ql-feed-both",
            "name": "Feed both",
            "type": "meal",
            "durationMinutes": None,
            "subtypes": ["dinner"],
            "note": "Evening meal",
            "companionIds": [CINDY_ID, LILLY_ID],
        },
    ]
}

SHIFTS = {
    "shifts": [
        {
            "id": "shift-snapshot",
            "userId": "4gy73h3nxgljlt7",
            "startAt": "2026-08-02T08:00:00.000Z",
            "endAt": "2026-08-02T20:00:00.000Z",
            "notes": "Weekend cover",
        }
    ],
    "hasMore": False,
}


def _mock_everything(mocked: aioresponses) -> None:
    """Register a fully populated instance."""
    mocked.get(
        re.compile(rf"^{re.escape(FEED_URL)}(\?.*)?$"),
        body=(
            __import__("pathlib").Path(__file__).parent / "fixtures" / "calendar_feed.ics"
        ).read_text(encoding="utf-8"),
        content_type="text/calendar",
        repeat=True,
    )
    mocked.get(endpoint("/api/quick-logs"), payload=QUICK_LOGS, repeat=True)
    mocked.get(endpoint("/api/shifts"), payload=SHIFTS, repeat=True)
    mocked.get(
        endpoint("/api/journal"),
        payload=load_fixture_json("journal_entry.json"),
        repeat=True,
    )
    mocked.get(
        endpoint("/api/companions"),
        payload=load_fixture_json("companions.json"),
        repeat=True,
    )
    mocked.get(endpoint("/api/users"), payload=load_fixture_json("users.json"), repeat=True)
    mocked.get(endpoint("/api/reminders"), payload=load_fixture_json("reminders.json"), repeat=True)
    mocked.get(endpoint("/api/logs"), payload=load_fixture_json("logs_cindy.json"), repeat=True)
    mocked.get(endpoint("/api/weight"), payload=load_fixture_json("weight_lilly.json"), repeat=True)


async def _setup(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    platforms: list[Platform] | None = None,
) -> MockConfigEntry:
    """Set up a fully populated entry at a fixed moment in time."""
    freezer.move_to("2026-08-02T12:00:00+00:00")

    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry,
        options={CONF_CALENDAR_FEED_URL: FEED_URL, CONF_ENABLE_MOOD_SENSOR: True},
    )

    with aioresponses() as mocked:
        _mock_everything(mocked)
        if platforms is None:
            assert await hass.config_entries.async_setup(entry.entry_id)
        else:
            with patch("custom_components.einvault.PLATFORMS", platforms):
                assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    return entry


@pytest.fixture
async def loaded_entry(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> MockConfigEntry:
    """Return a fully loaded entry with every platform set up."""
    return await _setup(hass, mock_config_entry, freezer)


@pytest.mark.parametrize(
    "platform",
    [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.BUTTON, Platform.CALENDAR],
)
@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_entity_registry_snapshot(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    freezer: FrozenDateTimeFactory,
    platform: Platform,
) -> None:
    """Every entity, its registry metadata, and its state.

    snapshot_platform insists on a single loaded platform, so each is set up on
    its own and snapshotted separately. It also requires every entity to be
    enabled, hence entity_registry_enabled_by_default — the diagnostic
    call-count sensor ships disabled, which test_sensor.py asserts separately.
    """
    entry = await _setup(hass, mock_config_entry, freezer, [platform])
    await snapshot_platform(hass, entity_registry, snapshot, entry.entry_id)


async def test_device_registry_snapshot(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
    snapshot: SnapshotAssertion,
) -> None:
    """The service device plus one device per companion, and their links."""
    devices = dr.async_entries_for_config_entry(device_registry, loaded_entry.entry_id)
    assert len(devices) == 3

    summary: list[dict[str, Any]] = sorted(
        (
            {
                "name": device.name,
                "model": device.model,
                "model_id": device.model_id,
                "manufacturer": device.manufacturer,
                "identifiers": sorted(
                    ident.replace(loaded_entry.entry_id, "<entry_id>")
                    for _, ident in device.identifiers
                ),
                "via_device": bool(device.via_device_id),
            }
            for device in devices
        ),
        key=lambda item: item["name"] or "",
    )
    assert summary == snapshot


async def test_all_platforms_are_represented(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, entity_registry: er.EntityRegistry
) -> None:
    """Guard the snapshots themselves.

    A platform that silently produced no entities would still yield a passing
    snapshot, so the expected counts are asserted explicitly.
    """
    entries = er.async_entries_for_config_entry(entity_registry, loaded_entry.entry_id)
    domains = {entry.domain for entry in entries}

    assert domains == {"sensor", "binary_sensor", "button", "calendar"}

    # 2 companions x (6 last_* + weight + due + next + mood) = 20 sensors,
    # plus the instance diagnostic sensor.
    assert sum(e.domain == "sensor" for e in entries) == 21
    # reminder_overdue per companion, plus caretaker_on_shift.
    assert sum(e.domain == "binary_sensor" for e in entries) == 3
    # One button per quick log.
    assert sum(e.domain == "button" for e in entries) == 2
    # One calendar per companion, plus the shift calendar.
    assert sum(e.domain == "calendar" for e in entries) == 3
