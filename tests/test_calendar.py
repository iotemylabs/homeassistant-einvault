"""Tests for the ICS-backed calendar platform.

The fixture mirrors the exact output of upstream ``src/lib/server/calendarIcs.ts``
— UID, DTSTAMP, DTSTART/DTEND, optional RRULE, ``SUMMARY: [Companion] Title``
and ``CATEGORIES: <kind>,<companionName>`` — using event data captured from the
live instance.
"""

from __future__ import annotations

import datetime
import pathlib

from aioresponses import aioresponses
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.einvault.api import parse_calendar_feed_url
from custom_components.einvault.const import CONF_CALENDAR_FEED_URL

from .conftest import BASE_URL, mock_full_refresh

FEED_TOKEN = "cft_test_feed_token"
FEED_URL = f"{BASE_URL}/api/calendar/{FEED_TOKEN}/feed.ics"

ICS = (pathlib.Path(__file__).parent / "fixtures" / "calendar_feed.ics").read_text(encoding="utf-8")


def _mock_feed(mocked: aioresponses, *, body: str = ICS, status: int = 200) -> None:
    """Register the ICS feed, ignoring any query string."""
    import re

    pattern = re.compile(rf"^{re.escape(FEED_URL)}(\?.*)?$")
    mocked.get(
        pattern,
        status=status,
        body=body,
        content_type="text/calendar",
        repeat=True,
    )


async def _setup_with_feed(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Set the entry up with a calendar feed configured."""
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, options={CONF_CALENDAR_FEED_URL: FEED_URL})
    with aioresponses() as mocked:
        _mock_feed(mocked)
        mock_full_refresh(mocked)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


# --- feed URL parsing ------------------------------------------------------


def test_parse_calendar_feed_url() -> None:
    base, token = parse_calendar_feed_url(
        "https://einvault.example.com/api/calendar/abc123/feed.ics"
    )
    assert base == "https://einvault.example.com"
    assert token == "abc123"


@pytest.mark.parametrize(
    "bad",
    [
        "https://einvault.example.com/api/calendar/abc123/feed.ical",
        "https://einvault.example.com/settings",
        "not-a-url",
        "ftp://einvault.example.com/api/calendar/abc/feed.ics",
    ],
)
def test_parse_calendar_feed_url_rejects(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_calendar_feed_url(bad)


# --- entities --------------------------------------------------------------


async def test_calendars_created_per_companion(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    await _setup_with_feed(hass, mock_config_entry)
    registry = er.async_get(hass)
    entries = [
        e
        for e in er.async_entries_for_config_entry(registry, mock_config_entry.entry_id)
        if e.domain == "calendar"
    ]
    # One per companion plus the instance-level shift calendar.
    assert len(entries) == 3

    assert hass.states.get("calendar.cindy_calendar") is not None
    assert hass.states.get("calendar.lilly_calendar") is not None


async def test_no_calendars_without_feed_url(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """The calendar is opt-in; nothing is created without a feed URL."""
    mock_config_entry.add_to_hass(hass)
    with aioresponses() as mocked:
        mock_full_refresh(mocked)
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert hass.states.get("calendar.cindy_calendar") is None


async def test_events_are_split_by_companion(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """CATEGORIES carries the companion name; each calendar keeps only its own."""
    await _setup_with_feed(hass, mock_config_entry)

    start = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    end = datetime.datetime(2027, 6, 1, tzinfo=datetime.UTC)

    cindy = await hass.services.async_call(
        "calendar",
        "get_events",
        {
            "entity_id": "calendar.cindy_calendar",
            "start_date_time": start.isoformat(),
            "end_date_time": end.isoformat(),
        },
        blocking=True,
        return_response=True,
    )
    summaries = [e["summary"] for e in cindy["calendar.cindy_calendar"]["events"]]
    assert "[Cindy] Get Blood Work" in summaries
    assert "[Cindy] Change Flea Collar" in summaries
    assert not any("Lilly" in s for s in summaries)


async def test_recurring_reminder_expands(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """The whole point of using ICS: RRULE gives every future occurrence.

    The REST API exposes only the current occurrence of a recurring reminder.
    """
    await _setup_with_feed(hass, mock_config_entry)

    result = await hass.services.async_call(
        "calendar",
        "get_events",
        {
            "entity_id": "calendar.cindy_calendar",
            "start_date_time": datetime.datetime(2027, 1, 1, tzinfo=datetime.UTC).isoformat(),
            "end_date_time": datetime.datetime(2030, 1, 1, tzinfo=datetime.UTC).isoformat(),
        },
        blocking=True,
        return_response=True,
    )
    flea = [
        e
        for e in result["calendar.cindy_calendar"]["events"]
        if e["summary"] == "[Cindy] Change Flea Collar"
    ]
    # 2027, 2028, 2029 — three yearly occurrences from one stored reminder.
    assert len(flea) == 3


async def test_shift_calendar_keeps_only_shifts(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    await _setup_with_feed(hass, mock_config_entry)

    entity_id = "calendar.192_168_1_246_7387_caretaker_shifts"
    result = await hass.services.async_call(
        "calendar",
        "get_events",
        {
            "entity_id": entity_id,
            "start_date_time": datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC).isoformat(),
            "end_date_time": datetime.datetime(2027, 1, 1, tzinfo=datetime.UTC).isoformat(),
        },
        blocking=True,
        return_response=True,
    )
    events = result[entity_id]["events"]
    assert len(events) == 1
    assert events[0]["summary"] == "Caretaker shift"


async def test_zero_length_event_is_given_duration(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """A point-in-time reminder would otherwise render as nothing."""
    await _setup_with_feed(hass, mock_config_entry)

    result = await hass.services.async_call(
        "calendar",
        "get_events",
        {
            "entity_id": "calendar.cindy_calendar",
            "start_date_time": datetime.datetime(2027, 1, 1, tzinfo=datetime.UTC).isoformat(),
            "end_date_time": datetime.datetime(2027, 3, 1, tzinfo=datetime.UTC).isoformat(),
        },
        blocking=True,
        return_response=True,
    )
    flea = result["calendar.cindy_calendar"]["events"][0]
    assert flea["start"] != flea["end"]


async def test_bad_feed_token_does_not_break_the_integration(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """The feed token is separate from the API token.

    A rejected feed must degrade the calendar only — it must not trigger the
    API reauth flow, which would prompt for the wrong credential entirely.
    """
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, options={CONF_CALENDAR_FEED_URL: FEED_URL}
    )
    with aioresponses() as mocked:
        _mock_feed(mocked, body="Not found", status=404)
        mock_full_refresh(mocked)
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    # Sensors still work.
    assert hass.states.get("sensor.cindy_last_walk") is not None
    # No reauth flow was started for the API token.
    assert not [
        flow
        for flow in hass.config_entries.flow.async_progress()
        if flow["context"]["source"] == "reauth"
    ]


async def test_malformed_feed_is_handled(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, options={CONF_CALENDAR_FEED_URL: FEED_URL}
    )
    with aioresponses() as mocked:
        _mock_feed(mocked, body="this is not an ics document")
        mock_full_refresh(mocked)
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert hass.states.get("sensor.cindy_last_walk") is not None
