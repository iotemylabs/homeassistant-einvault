"""Tests for the EinVault HTTP client.

These exercise the client against mocked HTTP without any Home Assistant
machinery, which is the point of keeping ``api.py`` free of HA imports.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from typing import Any

from aiohttp import ClientSession, TCPConnector, ThreadedResolver
from aioresponses import CallbackResult, aioresponses
import pytest

from custom_components.einvault.api import (
    EinVaultApiDisabledError,
    EinVaultAuthError,
    EinVaultClient,
    EinVaultConflictError,
    EinVaultConnectionError,
    EinVaultForbiddenError,
    EinVaultNotFoundError,
    EinVaultRateLimitError,
    EinVaultValidationError,
    normalize_base_url,
    validate_subtypes,
)
from custom_components.einvault.models import TokenScope

from .conftest import BASE_URL, CINDY_ID, TOKEN, load_fixture_json


def _test_session() -> ClientSession:
    """Build a session safe to construct on any platform.

    aiohttp defaults to the aiodns resolver when aiodns is installed, and that
    resolver refuses to initialise on Windows' ProactorEventLoop. Every request
    here is intercepted by aioresponses, so DNS never actually runs — an
    explicit threaded resolver just keeps construction portable.
    """
    return ClientSession(connector=TCPConnector(resolver=ThreadedResolver()))


@pytest.fixture
async def client() -> Any:
    """Provide a client bound to a real session against mocked transport."""
    async with _test_session() as session:
        yield EinVaultClient(BASE_URL, TOKEN, session)


# --- URL normalization -----------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://192.168.1.246:7387", "http://192.168.1.246:7387"),
        ("http://192.168.1.246:7387/", "http://192.168.1.246:7387"),
        ("http://192.168.1.246:7387/api/", "http://192.168.1.246:7387"),
        ("HTTP://192.168.1.246:7387", "http://192.168.1.246:7387"),
        ("https://EinVault.Example.Com", "https://einvault.example.com"),
        ("https://einvault.example.com:443", "https://einvault.example.com"),
        ("http://einvault.local:80", "http://einvault.local"),
        ("192.168.1.246:7387", "http://192.168.1.246:7387"),
    ],
)
def test_normalize_base_url(raw: str, expected: str) -> None:
    assert normalize_base_url(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "ftp://host", "http://"])
def test_normalize_base_url_rejects(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_base_url(raw)


# --- Subtype validation ----------------------------------------------------


def test_validate_subtypes_accepts_valid_pair() -> None:
    assert validate_subtypes("bathroom", ["pee", "poop"]) == ["pee", "poop"]


def test_validate_subtypes_dedupes_preserving_order() -> None:
    assert validate_subtypes("walk", ["hike", "leash", "hike"]) == ["hike", "leash"]


def test_validate_subtypes_rejects_wrong_type_pairing() -> None:
    """A walk subtype on a meal must fail locally, before any HTTP call."""
    with pytest.raises(ValueError, match="leash"):
        validate_subtypes("meal", ["leash"])


def test_validate_subtypes_rejects_unknown_type() -> None:
    with pytest.raises(ValueError, match="Unknown activity type"):
        validate_subtypes("nap", ["leash"])


def test_validate_subtypes_rejects_subtypes_on_other() -> None:
    with pytest.raises(ValueError, match="accepts no subtypes"):
        validate_subtypes("other", ["pee"])


def test_validate_subtypes_rejects_too_many() -> None:
    with pytest.raises(ValueError, match="At most 10"):
        validate_subtypes("grooming", ["bath"] * 11)


def test_validate_subtypes_allows_empty() -> None:
    assert validate_subtypes("walk", None) == []
    assert validate_subtypes("other", []) == []


# --- Reads and parsing -----------------------------------------------------


async def test_get_companions_parses_real_payload(client: EinVaultClient) -> None:
    with aioresponses() as mocked:
        mocked.get(
            f"{BASE_URL}/api/companions",
            payload=load_fixture_json("companions.json"),
        )
        companions = await client.async_get_companions()

    assert [c.name for c in companions] == ["Cindy", "Lilly"]
    cindy = companions[0]
    assert cindy.id == CINDY_ID
    assert cindy.weight_unit == "lbs"
    assert cindy.dob == date(2010, 5, 1)
    assert cindy.is_archived is False
    # species is intentionally not modelled at all.
    assert not hasattr(cindy, "species")


async def test_logs_parse_null_subtypes(client: EinVaultClient) -> None:
    """Live data returns ``subtypes: null``, not ``[]``, when unset."""
    with aioresponses() as mocked:
        mocked.get(
            f"{BASE_URL}/api/logs?companionId={CINDY_ID}&limit=50&offset=0",
            payload=load_fixture_json("logs_cindy.json"),
        )
        page = await client.async_get_logs(CINDY_ID)

    walk, meal, bathroom = page.items
    assert page.has_more is False
    assert walk.type == "walk"
    assert walk.subtypes == ("leash",)
    assert walk.duration_minutes == 15
    assert meal.subtypes == ()
    assert bathroom.subtypes == ()
    assert walk.logged_at == datetime(2026, 8, 2, 20, 1, 9, tzinfo=UTC)


async def test_weight_newest_first(client: EinVaultClient) -> None:
    with aioresponses() as mocked:
        mocked.get(
            f"{BASE_URL}/api/weight?companionId=lilly&limit=50&offset=0",
            payload=load_fixture_json("weight_lilly.json"),
        )
        page = await client.async_get_weight("lilly")

    assert [e.weight for e in page.items] == [46.0, 32.4]
    assert page.items[0].unit == "lbs"


async def test_journal_absent_returns_none(client: EinVaultClient) -> None:
    """The server sends ``{"entry": null}`` rather than a 404."""
    with aioresponses() as mocked:
        mocked.get(
            f"{BASE_URL}/api/journal?companionId={CINDY_ID}",
            payload=load_fixture_json("journal_absent.json"),
        )
        assert await client.async_get_journal(CINDY_ID) is None


async def test_journal_present_parses(client: EinVaultClient) -> None:
    with aioresponses() as mocked:
        mocked.get(
            f"{BASE_URL}/api/journal?companionId={CINDY_ID}&date=2026-08-02",
            payload=load_fixture_json("journal_entry.json"),
        )
        entry = await client.async_get_journal(CINDY_ID, on_date=date(2026, 8, 2))

    assert entry is not None
    assert entry.mood == "great"
    assert entry.body == "Integration test entry."


async def test_detect_scope_full(client: EinVaultClient) -> None:
    with aioresponses() as mocked:
        mocked.get(f"{BASE_URL}/api/companions", payload=load_fixture_json("companions.json"))
        scope, companions = await client.async_detect_token_scope()

    assert scope is TokenScope.FULL
    assert len(companions) == 2


async def test_detect_scope_write_only(client: EinVaultClient) -> None:
    """A write-only token gets id/name/species/isActive and nothing more."""
    with aioresponses() as mocked:
        mocked.get(
            f"{BASE_URL}/api/companions",
            payload=load_fixture_json("companions_write_only.json"),
        )
        scope, _ = await client.async_detect_token_scope()

    assert scope is TokenScope.WRITE_ONLY


# --- Error mapping ---------------------------------------------------------


async def test_401_raises_auth_error(client: EinVaultClient) -> None:
    with aioresponses() as mocked:
        mocked.get(
            f"{BASE_URL}/api/companions",
            status=401,
            payload=load_fixture_json("error_invalid_token.json"),
        )
        with pytest.raises(EinVaultAuthError):
            await client.async_get_companions()


async def test_404_html_means_api_disabled(client: EinVaultClient) -> None:
    """A disabled bearer API falls through to the SPA and returns HTML."""
    with aioresponses() as mocked:
        mocked.get(
            f"{BASE_URL}/api/companions",
            status=404,
            body="<!doctype html><html><head></head><body></body></html>",
            content_type="text/html",
        )
        with pytest.raises(EinVaultApiDisabledError):
            await client.async_get_companions()


async def test_404_json_means_not_found(client: EinVaultClient) -> None:
    """A real not-found must not be mistaken for a disabled API."""
    with aioresponses() as mocked:
        mocked.get(
            f"{BASE_URL}/api/companions/nope",
            status=404,
            payload=load_fixture_json("error_not_found.json"),
        )
        with pytest.raises(EinVaultNotFoundError) as err:
            await client.async_get_companion("nope")

    assert err.value.code == "notFound"


async def test_400_carries_code(client: EinVaultClient) -> None:
    with aioresponses() as mocked:
        mocked.post(
            f"{BASE_URL}/api/logs",
            status=400,
            payload=load_fixture_json("error_invalid_subtype.json"),
        )
        with pytest.raises(EinVaultValidationError) as err:
            await client.async_log_activity(event_type="walk", companion_id=CINDY_ID)

    assert err.value.code == "invalidSubtype"


async def test_403_write_scope_is_forbidden(client: EinVaultClient) -> None:
    with aioresponses() as mocked:
        mocked.get(
            f"{BASE_URL}/api/shifts?limit=50&offset=0",
            status=403,
            payload=load_fixture_json("error_write_scope.json"),
        )
        with pytest.raises(EinVaultForbiddenError) as err:
            await client.async_get_shifts()

    assert err.value.code == "writeScopeReadOnly"


async def test_409_is_conflict(client: EinVaultClient) -> None:
    with aioresponses() as mocked:
        mocked.post(
            f"{BASE_URL}/api/weight",
            status=409,
            payload=load_fixture_json("error_idempotency_reused.json"),
        )
        with pytest.raises(EinVaultConflictError) as err:
            await client.async_log_weight(
                companion_id=CINDY_ID, weight=9.2, unit="lbs", idempotency_key="k"
            )

    assert err.value.code == "idempotencyKeyReused"


async def test_connection_error(client: EinVaultClient) -> None:
    with aioresponses() as mocked:
        mocked.get(f"{BASE_URL}/api/health", exception=TimeoutError())
        with pytest.raises(EinVaultConnectionError):
            await client.async_get_health()


# --- Rate limiting ---------------------------------------------------------


async def test_429_raises_and_opens_local_cooldown(client: EinVaultClient) -> None:
    """After a 429 the client refuses to spend more of the shared budget."""
    with aioresponses() as mocked:
        mocked.get(
            f"{BASE_URL}/api/companions",
            status=429,
            payload=load_fixture_json("error_rate_limited.json"),
        )
        with pytest.raises(EinVaultRateLimitError) as first:
            await client.async_get_companions()

        # A second call must short-circuit locally rather than hit the wire.
        with pytest.raises(EinVaultRateLimitError):
            await client.async_get_companions()

    assert first.value.retry_after == 60
    assert client.request_count == 1


async def test_success_resets_backoff(client: EinVaultClient) -> None:
    with aioresponses() as mocked:
        mocked.get(f"{BASE_URL}/api/companions", payload={"companions": []})
        await client.async_get_companions()

    assert client.request_count == 1


# --- Sequencing ------------------------------------------------------------


async def test_requests_are_strictly_sequential() -> None:
    """Concurrent callers must still produce one in-flight request at a time."""
    in_flight = 0
    peak = 0

    async def slow_callback(url: Any, **kwargs: Any) -> CallbackResult:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return CallbackResult(status=200, payload={"companions": []})

    async with _test_session() as session:
        client = EinVaultClient(BASE_URL, TOKEN, session)
        with aioresponses() as mocked:
            mocked.get(f"{BASE_URL}/api/companions", callback=slow_callback, repeat=True)
            await asyncio.gather(*(client.async_get_companions() for _ in range(5)))

    assert peak == 1
    assert client.request_count == 5


# --- Write payload shape ---------------------------------------------------


async def _capture_request(
    client_call: Any, method: str, url: str
) -> tuple[dict[str, Any], dict[str, str]]:
    """Run a client call and return the JSON body and headers it sent."""
    captured: dict[str, Any] = {}

    async def callback(request_url: Any, **kwargs: Any) -> CallbackResult:
        captured["json"] = kwargs.get("json")
        captured["headers"] = kwargs.get("headers") or {}
        captured["url"] = str(request_url)
        return CallbackResult(status=201, payload={"ids": ["new-id"], "eventGroupId": None})

    with aioresponses() as mocked:
        getattr(mocked, method)(url, callback=callback)
        await client_call()

    return captured["json"], captured["headers"]


async def test_log_activity_sends_exact_keys(client: EinVaultClient) -> None:
    """The API is additionalProperties=false — a stray key is a 400."""
    body, headers = await _capture_request(
        lambda: client.async_log_activity(
            event_type="walk",
            companion_id=CINDY_ID,
            subtypes=["leash"],
            duration_minutes=15,
            notes="hello",
            idempotency_key="key-1",
        ),
        "post",
        f"{BASE_URL}/api/logs",
    )

    assert body == {
        "type": "walk",
        "companionId": CINDY_ID,
        "subtypes": ["leash"],
        "durationMinutes": 15,
        "notes": "hello",
    }
    assert headers["Idempotency-Key"] == "key-1"


async def test_log_activity_omits_unset_keys(client: EinVaultClient) -> None:
    """Unset optional fields must be absent, never sent as null."""
    body, _ = await _capture_request(
        lambda: client.async_log_activity(event_type="bathroom", companion_id=CINDY_ID),
        "post",
        f"{BASE_URL}/api/logs",
    )

    assert body == {"type": "bathroom", "companionId": CINDY_ID}


async def test_set_journal_omits_untouched_fields(client: EinVaultClient) -> None:
    """Omitting body preserves stored text server-side; never send null."""
    body, _ = await _capture_request(
        lambda: client.async_set_journal(
            companion_id=CINDY_ID, entry_date=date(2026, 8, 2), mood="great"
        ),
        "post",
        f"{BASE_URL}/api/journal",
    )

    assert body == {"companionId": CINDY_ID, "date": "2026-08-02", "mood": "great"}
    assert "body" not in body


async def test_quick_log_execute_sends_empty_body(client: EinVaultClient) -> None:
    """All quick-log configuration must stay in EinVault."""
    body, headers = await _capture_request(
        lambda: client.async_execute_quick_log("ql-1", idempotency_key="key-2"),
        "post",
        f"{BASE_URL}/api/quick-logs/ql-1/execute",
    )

    assert body is None
    assert headers["Idempotency-Key"] == "key-2"


async def test_token_is_never_placed_in_the_url(client: EinVaultClient) -> None:
    """The token belongs in a header — a query string lands in access logs."""
    captured: dict[str, Any] = {}

    async def callback(url: Any, **kwargs: Any) -> CallbackResult:
        captured["url"] = str(url)
        captured["headers"] = kwargs.get("headers") or {}
        return CallbackResult(status=200, payload={"companions": []})

    with aioresponses() as mocked:
        mocked.get(f"{BASE_URL}/api/companions", callback=callback)
        await client.async_get_companions()

    assert TOKEN not in captured["url"]
    assert captured["headers"]["Authorization"] == f"Bearer {TOKEN}"


async def test_log_activity_requires_a_target(client: EinVaultClient) -> None:
    """Both companion fields are optional upstream, but omitting both 400s."""
    with pytest.raises(ValueError, match="target companion is required"):
        await client.async_log_activity(event_type="walk")


async def test_log_activity_rejects_both_targets(client: EinVaultClient) -> None:
    with pytest.raises(ValueError, match="not both"):
        await client.async_log_activity(event_type="walk", companion_id="a", companion_ids=["b"])


async def test_log_activity_rejects_out_of_range_duration(client: EinVaultClient) -> None:
    with pytest.raises(ValueError, match="between 1 and 480"):
        await client.async_log_activity(
            event_type="walk", companion_id=CINDY_ID, duration_minutes=481
        )
