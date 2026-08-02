"""Shared test fixtures.

Every JSON fixture under ``tests/fixtures`` was captured from a live EinVault
1.x instance rather than hand-written, so parsing tests exercise real
nullability — notably ``subtypes: null`` and ``entry: null``.
"""

from __future__ import annotations

import json
import pathlib
import sys
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

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
    """Keep asyncio's self-pipe working under pytest-socket on Windows.

    ``pytest-homeassistant-custom-component`` calls
    ``pytest_socket.disable_socket(allow_unix_socket=True)``, which blocks
    creation of every AF_INET socket. On Linux that is fine —
    ``socket.socketpair()`` uses AF_UNIX, so asyncio can still build the event
    loop's self-pipe. On Windows there is no AF_UNIX, ``socketpair()`` is
    emulated over AF_INET, and every async test dies during event-loop setup
    before a single fixture of ours runs.

    Neutralising the call is scoped to Windows on purpose: CI runs Linux,
    where socket blocking still catches an accidental real network call.
    """
    if sys.platform != "win32":
        return

    import pytest_socket

    pytest_socket.disable_socket = lambda **kwargs: None  # type: ignore[assignment]

    # Home Assistant hardcodes aiohttp's AsyncResolver when building its
    # shared client session. That resolver is backed by aiodns, which refuses
    # to start on Windows' default ProactorEventLoop and leaves a pycares
    # shutdown thread behind that trips HA's own lingering-thread check.
    # Every request in these tests is intercepted by aioresponses, so DNS is
    # never exercised and a threaded resolver is a faithful stand-in.
    from aiohttp.resolver import ThreadedResolver
    from homeassistant.helpers import aiohttp_client

    aiohttp_client.AsyncResolver = ThreadedResolver  # type: ignore[misc]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(request: pytest.FixtureRequest) -> None:
    """Allow this custom integration to load, but only for tests that use hass.

    Requesting ``enable_custom_integrations`` unconditionally would drag the
    whole Home Assistant fixture chain into the pure client tests, which are
    deliberately independent of it.
    """
    if "hass" in request.fixturenames:
        request.getfixturevalue("enable_custom_integrations")


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Return a config entry matching what the config flow would create."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="192.168.1.246:7387",
        unique_id=BASE_URL,
        data={CONF_URL: BASE_URL, CONF_API_TOKEN: TOKEN},
    )
