"""Data update coordinator for EinVault.

The design here is dominated by one constraint: the server allows 30 requests
per 60 seconds keyed on client IP, checked before the token is even resolved,
and shared with every other consumer on that address. So the refresh is built
to a fixed, auditable budget rather than fetching whatever each entity wants.

Per fast refresh, with *N* companions:

======  ====================================================================
1       ``/api/reminders``  — no ``companionId``, covers every companion
1       ``/api/shifts``
*N*     ``/api/logs``       — bucketed client-side into latest-per-type
*N*     ``/api/weight``
======  ====================================================================

That is **6 calls for two companions**. Slow-changing collections
(companions, quick logs, users) refresh on a separate hourly cadence and merge
into the same data object.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
import logging
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    EinVaultAuthError,
    EinVaultClient,
    EinVaultError,
    EinVaultRateLimitError,
)
from .const import (
    CONF_INCLUDE_ARCHIVED,
    CONF_SCAN_INTERVAL,
    DEFAULT_INCLUDE_ARCHIVED,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    LIST_PAGE_SIZE,
    SLOW_REFRESH_INTERVAL,
)
from .models import Companion, LogEvent, QuickLog, Reminder, Shift, User, WeightEntry

if TYPE_CHECKING:
    from . import EinVaultConfigEntry

_LOGGER = logging.getLogger(__name__)


@dataclass
class EinVaultData:
    """Everything the entity platforms read from.

    ``users`` is not strictly needed by phase 2's sensors, but shifts carry
    only a ``userId`` — resolving a caretaker to a display name requires a
    client-side join, so the roster is fetched on the slow timer alongside the
    other rarely-changing collections.
    """

    companions: dict[str, Companion] = field(default_factory=dict)
    latest_events: dict[str, dict[str, LogEvent]] = field(default_factory=dict)
    latest_weight: dict[str, WeightEntry | None] = field(default_factory=dict)
    reminders: list[Reminder] = field(default_factory=list)
    shifts: list[Shift] = field(default_factory=list)
    quick_logs: list[QuickLog] = field(default_factory=list)
    users: list[User] = field(default_factory=list)
    calls_last_refresh: int = 0


class EinVaultDataUpdateCoordinator(DataUpdateCoordinator[EinVaultData]):
    """Coordinate polling against a rate-limited EinVault instance."""

    config_entry: EinVaultConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: EinVaultConfigEntry,
        client: EinVaultClient,
    ) -> None:
        """Initialise the coordinator."""
        self.client = client

        scan_interval = int(entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=timedelta(seconds=scan_interval),
        )

        # Slow-changing collections, carried between fast refreshes.
        self._companions: dict[str, Companion] = {}
        self._quick_logs: list[QuickLog] = []
        self._users: list[User] = []
        self._next_slow_refresh: float = 0.0

        # Warn once per rate-limit episode, not once per entity per refresh.
        self._rate_limit_warned = False

    @property
    def include_archived(self) -> bool:
        """Whether archived companions should get entities."""
        return bool(self.config_entry.options.get(CONF_INCLUDE_ARCHIVED, DEFAULT_INCLUDE_ARCHIVED))

    async def _async_update_data(self) -> EinVaultData:
        """Fetch the current state, staying inside the call budget."""
        calls_before = self.client.request_count

        try:
            data = await self._async_fetch()
        except EinVaultAuthError as err:
            # A rotated or revoked token — prompt for reauth rather than
            # retrying forever against a credential that will never work.
            raise ConfigEntryAuthFailed(str(err)) from err
        except EinVaultRateLimitError as err:
            data = self._handle_rate_limit(err)
        except EinVaultError as err:
            raise UpdateFailed(str(err)) from err

        # Recorded after the fact so the diagnostic sensor reports the refresh
        # that just happened rather than the one before it.
        data.calls_last_refresh = self.client.request_count - calls_before
        return data

    def _handle_rate_limit(self, err: EinVaultRateLimitError) -> EinVaultData:
        """Ride out a rate-limit episode without dropping every entity.

        The client already holds a local cooldown and refuses to spend more of
        the shared budget, so the useful thing here is to keep the last known
        state visible rather than marking every entity unavailable over a
        condition that resolves itself in a minute.
        """
        if not self._rate_limit_warned:
            _LOGGER.warning(
                "EinVault is rate limiting requests (30 per 60s per client IP, shared "
                "with every other consumer on this address); backing off for %.0fs. "
                "Consider a longer update interval, or point Home Assistant at the "
                "instance's local address so it gets its own budget",
                err.retry_after,
            )
            self._rate_limit_warned = True

        if self.data is not None:
            return self.data

        raise UpdateFailed(str(err))

    async def _async_fetch(self) -> EinVaultData:
        """Issue the refresh. Every call here is sequential by construction."""
        await self._async_refresh_slow_collections()

        companions = self._visible_companions()

        # One call for every companion's reminders, one for shifts.
        reminders = (await self.client.async_get_reminders(status="all")).items
        shifts = (await self.client.async_get_shifts()).items

        previous = self.data
        latest_events: dict[str, dict[str, LogEvent]] = {}
        latest_weight: dict[str, WeightEntry | None] = {}

        for companion_id in companions:
            latest_events[companion_id] = await self._async_latest_events(companion_id, previous)
            latest_weight[companion_id] = await self._async_latest_weight(companion_id, previous)

        self._rate_limit_warned = False

        return EinVaultData(
            companions=companions,
            latest_events=latest_events,
            latest_weight=latest_weight,
            reminders=reminders,
            shifts=shifts,
            quick_logs=list(self._quick_logs),
            users=list(self._users),
        )

    async def _async_latest_events(
        self, companion_id: str, previous: EinVaultData | None
    ) -> dict[str, LogEvent]:
        """Return the newest event of each type for one companion.

        A single ``/api/logs`` call is bucketed client-side. Fetching one
        request per event type would multiply the budget by six for no gain.

        A failure for one companion must not lose the others, so the previous
        value is retained and the refresh continues.
        """
        try:
            page = await self.client.async_get_logs(companion_id, limit=LIST_PAGE_SIZE)
        except EinVaultRateLimitError:
            raise
        except EinVaultAuthError:
            raise
        except EinVaultError as err:
            _LOGGER.debug("Could not read logs for companion %s: %s", companion_id, err)
            if previous is not None:
                return previous.latest_events.get(companion_id, {})
            return {}

        # The API returns newest first, so the first occurrence of each type
        # is that type's most recent event.
        newest: dict[str, LogEvent] = {}
        for event in page.items:
            newest.setdefault(event.type, event)
        return newest

    async def _async_latest_weight(
        self, companion_id: str, previous: EinVaultData | None
    ) -> WeightEntry | None:
        """Return the most recent weight entry for one companion."""
        try:
            page = await self.client.async_get_weight(companion_id, limit=1)
        except EinVaultRateLimitError:
            raise
        except EinVaultAuthError:
            raise
        except EinVaultError as err:
            _LOGGER.debug("Could not read weight for companion %s: %s", companion_id, err)
            if previous is not None:
                return previous.latest_weight.get(companion_id)
            return None

        return page.items[0] if page.items else None

    async def _async_refresh_slow_collections(self) -> None:
        """Refresh companions, quick logs, and users at most hourly.

        These change on human timescales. Polling them every cycle would add
        three calls per refresh to no purpose.
        """
        now = self.hass.loop.time()
        if self._companions and now < self._next_slow_refresh:
            return

        self._companions = {c.id: c for c in await self.client.async_get_companions()}
        self._quick_logs = await self.client.async_get_quick_logs()

        try:
            self._users = (await self.client.async_get_users()).items
        except EinVaultRateLimitError:
            raise
        except EinVaultAuthError:
            raise
        except EinVaultError as err:
            # The roster is only needed to put a name to a shift. Losing it
            # should not take the whole integration down.
            _LOGGER.debug("Could not read the user roster: %s", err)

        self._next_slow_refresh = now + SLOW_REFRESH_INTERVAL

    def _visible_companions(self) -> dict[str, Companion]:
        """Apply the archived-companions option."""
        if self.include_archived:
            return dict(self._companions)
        return {
            companion_id: companion
            for companion_id, companion in self._companions.items()
            if not companion.is_archived
        }
