"""Tests for the quick-log button platform."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from aioresponses import CallbackResult, aioresponses
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.einvault.const import DOMAIN

from .conftest import CINDY_ID, LILLY_ID, endpoint, mock_full_refresh
from .test_coordinator import _setup

SINGLE_COMPANION_QUICK_LOG = {
    "id": "ql-evening-walk",
    "name": "Evening walk",
    "type": "walk",
    "durationMinutes": 30,
    "subtypes": ["leash"],
    "note": None,
    "companionIds": [CINDY_ID],
}

MULTI_COMPANION_QUICK_LOG = {
    "id": "ql-feed-both",
    "name": "Feed both",
    "type": "meal",
    "durationMinutes": None,
    "subtypes": ["dinner"],
    "note": "Evening meal",
    "companionIds": [CINDY_ID, LILLY_ID],
}


def _quick_logs(*entries: dict[str, Any]) -> dict[str, Any]:
    return {"quickLogs": list(entries)}


async def _setup_with_quick_logs(
    hass: HomeAssistant, entry: MockConfigEntry, *entries: dict[str, Any]
) -> None:
    entry.add_to_hass(hass)
    with aioresponses() as mocked:
        mocked.get(endpoint("/api/quick-logs"), payload=_quick_logs(*entries), repeat=True)
        mock_full_refresh(mocked)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


async def test_no_buttons_when_no_quick_logs(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    """An empty list is normal, not an error — but say why, once."""
    await _setup(hass, mock_config_entry)

    registry = er.async_get(hass)
    buttons = [
        e
        for e in er.async_entries_for_config_entry(registry, mock_config_entry.entry_id)
        if e.domain == "button"
    ]
    assert buttons == []
    assert sum("no buttons were created" in r.message for r in caplog.records) == 1


async def test_single_companion_button_lands_on_companion_device(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """A quick log targeting one companion belongs to that companion."""
    await _setup_with_quick_logs(hass, mock_config_entry, SINGLE_COMPANION_QUICK_LOG)

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "button", DOMAIN, f"{mock_config_entry.entry_id}_{CINDY_ID}_quick_log_ql-evening-walk"
    )
    assert entity_id is not None

    devices = dr.async_get(hass)
    cindy = devices.async_get_device(
        identifiers={(DOMAIN, f"{mock_config_entry.entry_id}_{CINDY_ID}")}
    )
    assert registry.async_get(entity_id).device_id == cindy.id

    state = hass.states.get(entity_id)
    assert state.attributes["activity_type"] == "walk"
    assert state.attributes["subtypes"] == ["leash"]


async def test_multi_companion_button_lands_on_service_device(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """A quick log spanning companions has no single home."""
    await _setup_with_quick_logs(hass, mock_config_entry, MULTI_COMPANION_QUICK_LOG)

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "button", DOMAIN, f"{mock_config_entry.entry_id}_quick_log_ql-feed-both"
    )
    assert entity_id is not None

    devices = dr.async_get(hass)
    service = devices.async_get_device(identifiers={(DOMAIN, mock_config_entry.entry_id)})
    assert registry.async_get(entity_id).device_id == service.id
    assert hass.states.get(entity_id).attributes["companion_count"] == 2


async def test_press_sends_empty_body_and_idempotency_key(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """All configuration stays in EinVault, so the body must be empty."""
    await _setup_with_quick_logs(hass, mock_config_entry, SINGLE_COMPANION_QUICK_LOG)

    captured: dict[str, Any] = {}

    async def callback(url: Any, **kwargs: Any) -> CallbackResult:
        captured["json"] = kwargs.get("json")
        captured["headers"] = kwargs.get("headers") or {}
        return CallbackResult(status=201, payload={"ids": ["new-log"]})

    with aioresponses() as mocked:
        mocked.post(
            endpoint("/api/quick-logs/ql-evening-walk/execute"),
            callback=callback,
            repeat=True,
        )
        mocked.get(
            endpoint("/api/quick-logs"),
            payload=_quick_logs(SINGLE_COMPANION_QUICK_LOG),
            repeat=True,
        )
        mock_full_refresh(mocked)

        await hass.services.async_call(
            "button",
            "press",
            {"entity_id": "button.cindy_evening_walk"},
            blocking=True,
        )
        await hass.async_block_till_done()

    assert captured["json"] is None
    assert "Idempotency-Key" in captured["headers"]
    assert len(captured["headers"]["Idempotency-Key"]) > 10


async def test_press_retries_once_with_the_same_key(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """A lost response must replay, not duplicate.

    The server replays an identical body under the same key, so reusing it is
    what makes the retry safe.
    """
    await _setup_with_quick_logs(hass, mock_config_entry, SINGLE_COMPANION_QUICK_LOG)

    keys: list[str] = []

    async def callback(url: Any, **kwargs: Any) -> CallbackResult:
        keys.append((kwargs.get("headers") or {})["Idempotency-Key"])
        if len(keys) == 1:
            raise TimeoutError
        return CallbackResult(status=201, payload={"ids": ["new-log"]})

    with aioresponses() as mocked:
        mocked.post(
            endpoint("/api/quick-logs/ql-evening-walk/execute"),
            callback=callback,
            repeat=True,
        )
        mocked.get(
            endpoint("/api/quick-logs"),
            payload=_quick_logs(SINGLE_COMPANION_QUICK_LOG),
            repeat=True,
        )
        mock_full_refresh(mocked)

        await hass.services.async_call(
            "button", "press", {"entity_id": "button.cindy_evening_walk"}, blocking=True
        )
        await hass.async_block_till_done()

    assert len(keys) == 2
    assert keys[0] == keys[1]


async def test_press_surfaces_no_active_shift(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """A caretaker outside their shift gets an actionable message."""
    await _setup_with_quick_logs(hass, mock_config_entry, SINGLE_COMPANION_QUICK_LOG)

    with aioresponses() as mocked:
        mocked.post(
            endpoint("/api/quick-logs/ql-evening-walk/execute"),
            status=403,
            payload={"code": "noActiveShift", "message": "No active shift."},
            repeat=True,
        )
        mocked.get(
            endpoint("/api/quick-logs"),
            payload=_quick_logs(SINGLE_COMPANION_QUICK_LOG),
            repeat=True,
        )
        mock_full_refresh(mocked)

        with pytest.raises(HomeAssistantError) as err:
            await hass.services.async_call(
                "button", "press", {"entity_id": "button.cindy_evening_walk"}, blocking=True
            )

    assert err.value.translation_key == "no_active_shift"


async def test_buttons_appear_and_disappear_with_the_quick_log_set(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """Quick logs change between hourly refreshes; no reload should be needed."""
    await _setup_with_quick_logs(hass, mock_config_entry)
    assert hass.states.get("button.cindy_evening_walk") is None

    with aioresponses() as mocked:
        mocked.get(
            endpoint("/api/quick-logs"),
            payload=_quick_logs(SINGLE_COMPANION_QUICK_LOG),
            repeat=True,
        )
        mock_full_refresh(mocked)
        freezer.tick(timedelta(seconds=3700))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    assert hass.states.get("button.cindy_evening_walk") is not None
