"""Shared test fixtures.

Every JSON fixture under ``tests/fixtures`` was captured from a live EinVault
1.x instance rather than hand-written, so parsing tests exercise real
nullability — notably ``subtypes: null`` and ``entry: null``.
"""

from __future__ import annotations

from collections.abc import Generator
import json
import pathlib
import re
import sys
from typing import Any
from unittest.mock import PropertyMock, patch

from aioresponses import aioresponses
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.syrupy import HomeAssistantSnapshotExtension
from syrupy.assertion import SnapshotAssertion

from custom_components.einvault.const import CONF_API_TOKEN, CONF_URL, DOMAIN

FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures"

BASE_URL = "http://192.168.1.246:7387"
TOKEN = "evk_test_token_not_a_real_credential"

CINDY_ID = "gnc6gumwexrp3cs"
LILLY_ID = "ztmzsi6kq5lc2wl"


def load_fixture_json(name: str) -> Any:
    """Load a captured API response by file name."""
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def pytest_configure(config: pytest.Config) -> None:
    """Make the test environment behave the same on Linux CI and Windows."""
    # Home Assistant hardcodes aiohttp's AsyncResolver when building its shared
    # client session. That resolver is backed by aiodns, which spawns a pycares
    # "_run_safe_shutdown_loop" thread that outlives the test and trips Home
    # Assistant's own lingering-thread assertion in verify_cleanup. That thread
    # is not platform-specific, so this patch must not be either.
    #
    # Every request in these tests is intercepted by aioresponses, so DNS is
    # never exercised and a threaded resolver is a faithful stand-in.
    from aiohttp.resolver import ThreadedResolver
    from homeassistant.helpers import aiohttp_client

    aiohttp_client.AsyncResolver = ThreadedResolver  # type: ignore[misc]

    if sys.platform != "win32":
        return

    # Windows only: pytest-homeassistant-custom-component calls
    # pytest_socket.disable_socket(allow_unix_socket=True), which blocks
    # creation of every AF_INET socket. On Linux that is harmless because
    # socket.socketpair() uses AF_UNIX, so asyncio can still build the event
    # loop's self-pipe. Windows has no AF_UNIX, socketpair() is emulated over
    # AF_INET, and every async test dies during event-loop setup.
    #
    # Left Windows-scoped on purpose: CI runs Linux, where socket blocking
    # still catches an accidental real network call.
    import pytest_socket

    pytest_socket.disable_socket = lambda **kwargs: None  # type: ignore[assignment]


@pytest.fixture
def snapshot(snapshot: SnapshotAssertion) -> SnapshotAssertion:
    """Apply Home Assistant's snapshot serializer.

    pytest-homeassistant-custom-component ships this same override, but syrupy
    registers its own ``snapshot`` fixture and wins the plugin ordering, so the
    plain Amber extension ends up active and registry entries serialize as raw
    reprs — including ``id`` and ``device_id``, which are regenerated on every
    run and would make the snapshots unstable. Redefining it here takes
    precedence over both plugins.
    """
    return snapshot.use_extension(HomeAssistantSnapshotExtension)


@pytest.fixture
def entity_registry_enabled_by_default() -> Generator[None]:
    """Force every entity to register enabled.

    Home Assistant Core ships this fixture but this version of
    pytest-homeassistant-custom-component does not re-export it, and
    ``snapshot_platform`` refuses to run while any entity is disabled. The
    diagnostic call-count sensor ships disabled on purpose, which
    ``test_sensor.py`` asserts separately.
    """
    with patch(
        "homeassistant.helpers.entity.Entity.entity_registry_enabled_default",
        PropertyMock(return_value=True),
    ):
        yield


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(request: pytest.FixtureRequest) -> None:
    """Allow this custom integration to load, but only for tests that use hass.

    Requesting ``enable_custom_integrations`` unconditionally would drag the
    whole Home Assistant fixture chain into the pure client tests, which are
    deliberately independent of it.
    """
    if "hass" in request.fixturenames:
        request.getfixturevalue("enable_custom_integrations")


ENTRY_ID = "01JQ8ZK9ABCDEFGHJKMNPQRSTV"
"""Pinned so snapshots stay stable.

Entity unique ids are built from the config entry id, and device ids are
derived from the device identifiers, which contain it too. With a randomly
generated entry id every snapshot would differ on every run.
"""


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Return a config entry matching what the config flow would create."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="192.168.1.246:7387",
        unique_id=BASE_URL,
        entry_id=ENTRY_ID,
        data={CONF_URL: BASE_URL, CONF_API_TOKEN: TOKEN},
    )


def endpoint(path: str) -> re.Pattern[str]:
    """Match an endpoint regardless of its query string.

    The client appends pagination parameters to most reads, and aioresponses
    matches on the full URL, so bare paths would never match. Matching on the
    path keeps these tests about the call budget rather than about exact query
    formatting.
    """
    return re.compile(rf"^{re.escape(BASE_URL + path)}(\?.*)?$")


def mock_full_refresh(mocked: aioresponses, *, repeat: bool = True) -> None:
    """Register every endpoint a refresh touches."""
    mocked.get(
        endpoint("/api/companions"),
        payload=load_fixture_json("companions.json"),
        repeat=repeat,
    )
    mocked.get(
        endpoint("/api/quick-logs"),
        payload=load_fixture_json("quick_logs_empty.json"),
        repeat=repeat,
    )
    mocked.get(endpoint("/api/users"), payload=load_fixture_json("users.json"), repeat=repeat)
    mocked.get(
        endpoint("/api/reminders"),
        payload=load_fixture_json("reminders.json"),
        repeat=repeat,
    )
    mocked.get(endpoint("/api/shifts"), payload={"shifts": [], "hasMore": False}, repeat=repeat)
    mocked.get(
        endpoint("/api/logs"),
        payload=load_fixture_json("logs_cindy.json"),
        repeat=repeat,
    )
    mocked.get(
        endpoint("/api/weight"),
        payload=load_fixture_json("weight_lilly.json"),
        repeat=repeat,
    )
