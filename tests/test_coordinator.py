"""Tests for the EinVault data update coordinator.

The call-budget test is the load-bearing one here. The server allows 30
requests per 60 seconds per client IP, shared with every other consumer, so a
regression that adds one call per companion per refresh is a real outage risk
rather than a style problem.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from aioresponses import aioresponses
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.einvault.const import CONF_INCLUDE_ARCHIVED

from .conftest import CINDY_ID, LILLY_ID, endpoint, load_fixture_json, mock_full_refresh


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> Any:
    """Set the entry up against a fully mocked instance."""
    entry.add_to_hass(hass)
    with aioresponses() as mocked:
        mock_full_refresh(mocked)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry.runtime_data.coordinator


async def test_initial_refresh_populates_data(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    coordinator = await _setup(hass, mock_config_entry)
    data = coordinator.data

    assert set(data.companions) == {CINDY_ID, LILLY_ID}
    assert data.companions[CINDY_ID].name == "Cindy"
    # logs fixture is newest-first: walk, meal, bathroom
    assert set(data.latest_events[CINDY_ID]) == {"walk", "meal", "bathroom"}
    assert data.latest_events[CINDY_ID]["walk"].subtypes == ("leash",)
    assert data.latest_weight[CINDY_ID] is not None
    assert data.latest_weight[CINDY_ID].weight == 46.0


async def test_latest_events_picks_newest_per_type(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Bucketing must take the first occurrence, since logs are newest-first."""
    coordinator = await _setup(hass, mock_config_entry)
    walk = coordinator.data.latest_events[CINDY_ID]["walk"]
    assert walk.id == "vqd5oz4qcpiiv6j"
    assert walk.duration_minutes == 15


async def test_call_budget_is_six_for_two_companions(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """A steady-state refresh must cost exactly 6 calls with two companions.

    1 reminders + 1 shifts + 2 logs + 2 weight. Companions, quick logs, and
    users are on the hourly slow timer and must not appear here.
    """
    coordinator = await _setup(hass, mock_config_entry)

    before = coordinator.client.request_count
    with aioresponses() as mocked:
        mock_full_refresh(mocked)
        freezer.tick(timedelta(seconds=300))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    assert coordinator.client.request_count - before == 6
    assert coordinator.data.calls_last_refresh == 6


async def test_slow_collections_refresh_hourly(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """After an hour the slow set is re-read, costing 3 extra calls."""
    coordinator = await _setup(hass, mock_config_entry)

    before = coordinator.client.request_count
    with aioresponses() as mocked:
        mock_full_refresh(mocked)
        freezer.tick(timedelta(seconds=3700))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    # 6 fast calls plus companions, quick-logs and users.
    assert coordinator.client.request_count - before == 9


async def test_partial_failure_keeps_other_companions(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """One companion's logs failing must not blank out the rest."""
    coordinator = await _setup(hass, mock_config_entry)
    assert coordinator.data.latest_events[CINDY_ID]

    with aioresponses() as mocked:
        # Registered first: aioresponses uses the first matching pattern.
        # Fail every logs call this cycle; weight and the rest still succeed.
        mocked.get(endpoint("/api/logs"), status=500, payload={}, repeat=True)
        mock_full_refresh(mocked, repeat=True)
        freezer.tick(timedelta(seconds=300))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    assert coordinator.last_update_success is True
    # Previous values are retained rather than dropped.
    assert coordinator.data.latest_events[CINDY_ID] != {}
    assert coordinator.data.latest_weight[LILLY_ID] is not None


async def test_rate_limit_keeps_previous_data(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 429 must not blank every entity, and must warn once, not per entity."""
    coordinator = await _setup(hass, mock_config_entry)
    previous = coordinator.data

    caplog.clear()
    with aioresponses() as mocked:
        mocked.get(
            endpoint("/api/reminders"),
            status=429,
            payload=load_fixture_json("error_rate_limited.json"),
            repeat=True,
        )
        freezer.tick(timedelta(seconds=300))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    assert coordinator.last_update_success is True
    assert coordinator.data.companions == previous.companions
    assert sum("rate limiting" in r.message for r in caplog.records) == 1


async def test_revoked_token_mid_run_triggers_reauth(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """A token rotated while running must start reauth, not retry forever."""
    await _setup(hass, mock_config_entry)

    with aioresponses() as mocked:
        mocked.get(
            endpoint("/api/reminders"),
            status=401,
            payload=load_fixture_json("error_invalid_token.json"),
            repeat=True,
        )
        freezer.tick(timedelta(seconds=300))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    flows = hass.config_entries.flow.async_progress()
    assert any(flow["context"]["source"] == "reauth" for flow in flows)


async def test_archived_companions_excluded_by_default(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Archived companions must not get entities unless asked for."""
    payload = load_fixture_json("companions.json")
    payload["companions"][1]["archivedAt"] = "2026-07-01T00:00:00.000Z"

    mock_config_entry.add_to_hass(hass)
    with aioresponses() as mocked:
        mocked.get(endpoint("/api/companions"), payload=payload, repeat=True)
        mock_full_refresh(mocked)
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    coordinator = mock_config_entry.runtime_data.coordinator
    assert set(coordinator.data.companions) == {CINDY_ID}


async def test_archived_companions_included_when_enabled(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    payload = load_fixture_json("companions.json")
    payload["companions"][1]["archivedAt"] = "2026-07-01T00:00:00.000Z"

    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(mock_config_entry, options={CONF_INCLUDE_ARCHIVED: True})

    with aioresponses() as mocked:
        mocked.get(endpoint("/api/companions"), payload=payload, repeat=True)
        mock_full_refresh(mocked)
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    coordinator = mock_config_entry.runtime_data.coordinator
    assert set(coordinator.data.companions) == {CINDY_ID, LILLY_ID}


async def test_setup_fails_cleanly_when_unreachable(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    with aioresponses() as mocked:
        mocked.get(endpoint("/api/companions"), exception=TimeoutError(), repeat=True)
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
