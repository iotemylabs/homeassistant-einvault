"""Tests for the EinVault sensor platform."""

from __future__ import annotations

from datetime import timedelta

from aioresponses import aioresponses
from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import STATE_UNAVAILABLE, UnitOfMass
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.einvault.const import DOMAIN

from .conftest import (
    CINDY_ID,
    LILLY_ID,
    endpoint,
    load_fixture_json,
    mock_full_refresh,
)
from .test_coordinator import _setup


async def test_entities_created_per_companion(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Six last_* sensors plus a weight sensor, for each companion."""
    await _setup(hass, mock_config_entry)
    registry = er.async_get(hass)

    entries = er.async_entries_for_config_entry(registry, mock_config_entry.entry_id)
    keys = {e.unique_id for e in entries}

    for suffix in (
        "last_walk",
        "last_meal",
        "last_bathroom",
        "last_play",
        "last_grooming",
        "last_treat",
        "latest_weight",
    ):
        assert f"{mock_config_entry.entry_id}_{CINDY_ID}_{suffix}" in keys
        assert f"{mock_config_entry.entry_id}_{LILLY_ID}_{suffix}" in keys

    for suffix in ("due_reminders", "next_reminder", "reminder_overdue"):
        assert f"{mock_config_entry.entry_id}_{CINDY_ID}_{suffix}" in keys
        assert f"{mock_config_entry.entry_id}_{LILLY_ID}_{suffix}" in keys

    # Instance-scoped entities.
    assert f"{mock_config_entry.entry_id}_api_calls_last_refresh" in keys
    assert f"{mock_config_entry.entry_id}_caretaker_on_shift" in keys

    # The mood sensor is opt-in and off by default.
    assert f"{mock_config_entry.entry_id}_{CINDY_ID}_today_mood" not in keys

    # No calendar feed URL configured, so no calendar entities.
    assert not any(e.domain == "calendar" for e in entries)

    # 2 companions x (6 last_* + weight + due + next + overdue) + 2 instance
    assert len(entries) == 22


async def test_device_model_and_via_device(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """One device per companion, linked to a service device for the instance."""
    await _setup(hass, mock_config_entry)
    devices = dr.async_get(hass)

    service = devices.async_get_device(identifiers={(DOMAIN, mock_config_entry.entry_id)})
    assert service is not None

    cindy = devices.async_get_device(
        identifiers={(DOMAIN, f"{mock_config_entry.entry_id}_{CINDY_ID}")}
    )
    assert cindy is not None
    assert cindy.name == "Cindy"
    assert cindy.manufacturer == "EinVault"
    assert cindy.model == "Companion"
    assert cindy.via_device_id == service.id


async def test_last_event_sensor_state_and_attributes(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    await _setup(hass, mock_config_entry)

    state = hass.states.get("sensor.cindy_last_walk")
    assert state is not None
    assert state.state == "2026-08-02T20:01:09+00:00"
    assert state.attributes["subtypes"] == ["leash"]
    assert state.attributes["duration_minutes"] == 15
    assert state.attributes["notes"] == "ha-einvault phase-1 probe"


async def test_last_event_sensor_unknown_when_never_logged(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """A companion with no grooming entry reports unknown, not unavailable."""
    await _setup(hass, mock_config_entry)

    state = hass.states.get("sensor.cindy_last_grooming")
    assert state is not None
    assert state.state == "unknown"


async def test_weight_sensor_uses_companion_unit(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Both companions are configured in lbs."""
    await _setup(hass, mock_config_entry)

    state = hass.states.get("sensor.cindy_weight")
    assert state is not None
    assert state.state == "46.0"
    assert state.attributes["unit_of_measurement"] == UnitOfMass.POUNDS
    assert state.attributes["device_class"] == "weight"


async def test_weight_sensor_falls_back_to_entry_unit(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """companion.weightUnit is nullable; the entry always carries a unit."""
    companions = load_fixture_json("companions.json")
    for companion in companions["companions"]:
        companion["weightUnit"] = None

    mock_config_entry.add_to_hass(hass)
    with aioresponses() as mocked:
        mocked.get(endpoint("/api/companions"), payload=companions, repeat=True)
        mock_full_refresh(mocked)
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    state = hass.states.get("sensor.cindy_weight")
    assert state is not None
    assert state.attributes["unit_of_measurement"] == UnitOfMass.POUNDS


async def test_weight_sensor_unknown_without_entries(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    with aioresponses() as mocked:
        mocked.get(
            endpoint("/api/weight"),
            payload=load_fixture_json("weight_empty.json"),
            repeat=True,
        )
        mock_full_refresh(mocked)
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    state = hass.states.get("sensor.cindy_weight")
    assert state is not None
    assert state.state == "unknown"


async def test_entities_go_unavailable_when_companion_disappears(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """A companion archived upstream should not freeze on a stale value."""
    await _setup(hass, mock_config_entry)
    assert hass.states.get("sensor.lilly_last_treat") is not None

    only_cindy = load_fixture_json("companions.json")
    only_cindy["companions"] = only_cindy["companions"][:1]

    with aioresponses() as mocked:
        mocked.get(endpoint("/api/companions"), payload=only_cindy, repeat=True)
        mock_full_refresh(mocked)
        # Past the hourly slow-refresh boundary so companions are re-read.
        freezer.tick(timedelta(seconds=3700))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    state = hass.states.get("sensor.lilly_last_treat")
    assert state is not None
    assert state.state == STATE_UNAVAILABLE


async def test_new_companion_gets_entities_without_reload(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """A pet added in EinVault should appear on the next slow refresh."""
    one = load_fixture_json("companions.json")
    both = load_fixture_json("companions.json")
    one["companions"] = one["companions"][:1]

    mock_config_entry.add_to_hass(hass)
    with aioresponses() as mocked:
        mocked.get(endpoint("/api/companions"), payload=one, repeat=True)
        mock_full_refresh(mocked)
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert hass.states.get("sensor.lilly_last_treat") is None

    with aioresponses() as mocked:
        mocked.get(endpoint("/api/companions"), payload=both, repeat=True)
        mock_full_refresh(mocked)
        freezer.tick(timedelta(seconds=3700))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    assert hass.states.get("sensor.lilly_last_treat") is not None


async def test_api_calls_diagnostic_sensor(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Disabled by default, but registered so the budget stays observable."""
    await _setup(hass, mock_config_entry)
    registry = er.async_get(hass)

    entity_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{mock_config_entry.entry_id}_api_calls_last_refresh"
    )
    assert entity_id is not None
    assert registry.async_get(entity_id).disabled_by is er.RegistryEntryDisabler.INTEGRATION
