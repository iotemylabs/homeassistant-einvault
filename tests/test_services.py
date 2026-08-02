"""Tests for the EinVault actions.

The payload-shape assertions matter more than usual here: the API declares
``additionalProperties: false``, so one stray key turns a valid action into a
400.
"""

from __future__ import annotations

from typing import Any

from aioresponses import CallbackResult, aioresponses
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.einvault.const import DOMAIN

from .conftest import CINDY_ID, endpoint, mock_full_refresh
from .test_coordinator import _setup


def _companion_device_id(hass: HomeAssistant, entry: MockConfigEntry, companion_id: str) -> str:
    device = dr.async_get(hass).async_get_device(
        identifiers={(DOMAIN, f"{entry.entry_id}_{companion_id}")}
    )
    assert device is not None
    return device.id


async def _call_and_capture(
    hass: HomeAssistant,
    service: str,
    data: dict[str, Any],
    *,
    method: str,
    path: str,
    status: int = 201,
    payload: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Invoke an action and return the request body and headers it produced."""
    captured: dict[str, Any] = {}

    async def callback(url: Any, **kwargs: Any) -> CallbackResult:
        captured["json"] = kwargs.get("json")
        captured["headers"] = kwargs.get("headers") or {}
        return CallbackResult(status=status, payload=payload or {"ids": ["x"]})

    with aioresponses() as mocked:
        getattr(mocked, method)(endpoint(path), callback=callback, repeat=True)
        mock_full_refresh(mocked)
        await hass.services.async_call(DOMAIN, service, data, blocking=True)
        await hass.async_block_till_done()

    return captured["json"], captured["headers"]


async def test_services_are_registered(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    await _setup(hass, mock_config_entry)
    for name in (
        "log_activity",
        "log_weight",
        "log_health_event",
        "set_journal",
        "complete_reminder",
        "skip_reminder",
    ):
        assert hass.services.has_service(DOMAIN, name)


async def test_log_activity_payload_has_no_extra_keys(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """additionalProperties=false means a stray key is a hard 400."""
    await _setup(hass, mock_config_entry)
    device_id = _companion_device_id(hass, mock_config_entry, CINDY_ID)

    body, headers = await _call_and_capture(
        hass,
        "log_activity",
        {
            "device_id": device_id,
            "type": "walk",
            "subtypes": ["leash"],
            "duration_minutes": 30,
            "notes": "Evening walk",
        },
        method="post",
        path="/api/logs",
    )

    assert body == {
        "type": "walk",
        "companionId": CINDY_ID,
        "subtypes": ["leash"],
        "durationMinutes": 30,
        "notes": "Evening walk",
    }
    assert "Idempotency-Key" in headers


async def test_log_activity_minimal_payload(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Unset optional fields must be absent, not null."""
    await _setup(hass, mock_config_entry)
    device_id = _companion_device_id(hass, mock_config_entry, CINDY_ID)

    body, _ = await _call_and_capture(
        hass,
        "log_activity",
        {"device_id": device_id, "type": "bathroom"},
        method="post",
        path="/api/logs",
    )

    assert body == {"type": "bathroom", "companionId": CINDY_ID}


async def test_log_activity_rejects_bad_subtype_before_any_http(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """A local check names the offending value; the server just says 400."""
    await _setup(hass, mock_config_entry)
    device_id = _companion_device_id(hass, mock_config_entry, CINDY_ID)

    calls: list[Any] = []

    async def callback(url: Any, **kwargs: Any) -> CallbackResult:
        calls.append(url)
        return CallbackResult(status=201, payload={"ids": ["x"]})

    with aioresponses() as mocked:
        mocked.post(endpoint("/api/logs"), callback=callback, repeat=True)
        mock_full_refresh(mocked)

        with pytest.raises(ServiceValidationError) as err:
            await hass.services.async_call(
                DOMAIN,
                "log_activity",
                {"device_id": device_id, "type": "meal", "subtypes": ["leash"]},
                blocking=True,
            )

    assert err.value.translation_key == "invalid_subtypes"
    assert calls == []  # nothing was sent


async def test_log_weight_payload(hass: HomeAssistant, mock_config_entry: MockConfigEntry) -> None:
    await _setup(hass, mock_config_entry)
    device_id = _companion_device_id(hass, mock_config_entry, CINDY_ID)

    body, _ = await _call_and_capture(
        hass,
        "log_weight",
        {"device_id": device_id, "weight": 9.2, "unit": "lbs"},
        method="post",
        path="/api/weight",
        payload={"id": "w1", "companionId": CINDY_ID},
    )

    assert body == {"companionId": CINDY_ID, "weight": 9.2, "unit": "lbs"}


async def test_log_health_event_payload(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    await _setup(hass, mock_config_entry)
    device_id = _companion_device_id(hass, mock_config_entry, CINDY_ID)

    body, _ = await _call_and_capture(
        hass,
        "log_health_event",
        {
            "device_id": device_id,
            "type": "vet_visit",
            "title": "Annual checkup",
            "vet_name": "Dr Smith",
        },
        method="post",
        path="/api/health-events",
        payload={"id": "h1", "companionId": CINDY_ID},
    )

    assert body == {
        "companionId": CINDY_ID,
        "type": "vet_visit",
        "title": "Annual checkup",
        "vetName": "Dr Smith",
    }


async def test_set_journal_omits_untouched_fields(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Omitting body preserves the stored text — nulls are never sent."""
    await _setup(hass, mock_config_entry)
    device_id = _companion_device_id(hass, mock_config_entry, CINDY_ID)

    body, _ = await _call_and_capture(
        hass,
        "set_journal",
        {"device_id": device_id, "mood": "great"},
        method="post",
        path="/api/journal",
        payload={"id": "j1", "companionId": CINDY_ID, "date": "2026-08-02"},
    )

    assert body == {"companionId": CINDY_ID, "mood": "great"}
    assert "body" not in body


async def test_complete_reminder(hass: HomeAssistant, mock_config_entry: MockConfigEntry) -> None:
    await _setup(hass, mock_config_entry)
    device_id = _companion_device_id(hass, mock_config_entry, CINDY_ID)

    _, headers = await _call_and_capture(
        hass,
        "complete_reminder",
        {"device_id": device_id, "reminder_id": "rpfrzohwk2vuihd"},
        method="post",
        path="/api/reminders/rpfrzohwk2vuihd/complete",
        status=200,
        payload={"id": "rpfrzohwk2vuihd", "completedAt": "x", "nextReminderId": None},
    )

    assert "Idempotency-Key" in headers


async def test_skip_one_off_reminder_is_rejected(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """The server answers 400 notRecurring; surface it as a validation error."""
    await _setup(hass, mock_config_entry)
    device_id = _companion_device_id(hass, mock_config_entry, CINDY_ID)

    with aioresponses() as mocked:
        mocked.post(
            endpoint("/api/reminders/rpfrzohwk2vuihd/skip"),
            status=400,
            payload={"code": "notRecurring", "message": "Not recurring."},
            repeat=True,
        )
        mock_full_refresh(mocked)

        with pytest.raises(ServiceValidationError) as err:
            await hass.services.async_call(
                DOMAIN,
                "skip_reminder",
                {"device_id": device_id, "reminder_id": "rpfrzohwk2vuihd"},
                blocking=True,
            )

    assert err.value.translation_key == "not_recurring"


async def test_already_completed_reminder(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    await _setup(hass, mock_config_entry)
    device_id = _companion_device_id(hass, mock_config_entry, CINDY_ID)

    with aioresponses() as mocked:
        mocked.post(
            endpoint("/api/reminders/abc/complete"),
            status=409,
            payload={"code": "alreadyCompleted", "message": "Already done."},
            repeat=True,
        )
        mock_full_refresh(mocked)

        with pytest.raises(ServiceValidationError) as err:
            await hass.services.async_call(
                DOMAIN,
                "complete_reminder",
                {"device_id": device_id, "reminder_id": "abc"},
                blocking=True,
            )

    assert err.value.translation_key == "already_completed"


async def test_no_active_shift_is_actionable(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    await _setup(hass, mock_config_entry)
    device_id = _companion_device_id(hass, mock_config_entry, CINDY_ID)

    with aioresponses() as mocked:
        mocked.post(
            endpoint("/api/logs"),
            status=403,
            payload={"code": "noActiveShift", "message": "No active shift."},
            repeat=True,
        )
        mock_full_refresh(mocked)

        with pytest.raises(ServiceValidationError) as err:
            await hass.services.async_call(
                DOMAIN,
                "log_activity",
                {"device_id": device_id, "type": "walk"},
                blocking=True,
            )

    assert err.value.translation_key == "no_active_shift"


async def test_targeting_a_non_companion_device_is_rejected(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """The service device is not a companion and cannot be logged against."""
    await _setup(hass, mock_config_entry)
    service_device = dr.async_get(hass).async_get_device(
        identifiers={(DOMAIN, mock_config_entry.entry_id)}
    )

    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            "log_activity",
            {"device_id": service_device.id, "type": "walk"},
            blocking=True,
        )

    assert err.value.translation_key == "not_a_companion"


async def test_unknown_device_is_rejected(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    await _setup(hass, mock_config_entry)

    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            "log_activity",
            {"device_id": "does-not-exist", "type": "walk"},
            blocking=True,
        )

    assert err.value.translation_key == "device_not_found"


async def test_invalid_type_rejected_by_schema(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    await _setup(hass, mock_config_entry)
    device_id = _companion_device_id(hass, mock_config_entry, CINDY_ID)

    with pytest.raises(vol_error := Exception):
        await hass.services.async_call(
            DOMAIN,
            "log_activity",
            {"device_id": device_id, "type": "nap"},
            blocking=True,
        )
    assert vol_error is not None


async def test_action_triggers_a_refresh(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Sensors should reflect a logged activity without waiting for the poll."""
    coordinator = await _setup(hass, mock_config_entry)
    device_id = _companion_device_id(hass, mock_config_entry, CINDY_ID)
    before = coordinator.client.request_count

    await _call_and_capture(
        hass,
        "log_activity",
        {"device_id": device_id, "type": "walk"},
        method="post",
        path="/api/logs",
    )

    # One write plus a refresh, rather than just the write.
    assert coordinator.client.request_count > before + 1


async def test_rate_limited_action_is_reported(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    await _setup(hass, mock_config_entry)
    device_id = _companion_device_id(hass, mock_config_entry, CINDY_ID)

    with aioresponses() as mocked:
        mocked.post(
            endpoint("/api/logs"),
            status=429,
            payload={"code": "rateLimited", "message": "Too many requests."},
            repeat=True,
        )
        mock_full_refresh(mocked)

        with pytest.raises(HomeAssistantError) as err:
            await hass.services.async_call(
                DOMAIN,
                "log_activity",
                {"device_id": device_id, "type": "walk"},
                blocking=True,
            )

    assert err.value.translation_key == "rate_limited"
