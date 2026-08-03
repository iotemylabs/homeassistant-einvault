"""Calendar platform for EinVault, backed by the personal ICS feed.

The feed is a better source than the REST API for calendar purposes, for three
reasons:

* it carries real ``RRULE`` recurrence, so a yearly reminder shows every future
  occurrence — the REST API exposes only the current one;
* it covers health events, reminders, and caretaker shifts in a single
  document;
* it authenticates on a token in its own path rather than through
  ``requireApiToken``, so fetching it costs nothing against the
  30-request/60-second API budget.

One fetch per refresh serves every calendar entity. Events are attributed to
companions using the ``CATEGORIES`` property, which EinVault emits as
``<kind>,<companionName>``.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import EinVaultConfigEntry
from .coordinator import EinVaultCalendarCoordinator, EinVaultDataUpdateCoordinator
from .entity import EinVaultCompanionEntity, EinVaultInstanceEntity
from .models import Companion

if TYPE_CHECKING:
    from ical.event import Event

_LOGGER = logging.getLogger(__name__)

# The coordinator owns all polling and the client serialises every request
# through a semaphore, so Home Assistant does not need to throttle this
# platform on top of that.
PARALLEL_UPDATES = 0

SHIFT_CATEGORY = "shift"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EinVaultConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EinVault calendars, if a feed URL has been configured."""
    calendar_coordinator = entry.runtime_data.calendar_coordinator
    if calendar_coordinator is None:
        _LOGGER.debug(
            "No EinVault calendar feed configured; skipping calendar entities. "
            "Add one under the integration's options using the URL from "
            "EinVault Settings, Calendar feed"
        )
        return

    coordinator = entry.runtime_data.coordinator
    known: set[str] = set()

    @callback
    def _async_add_companion_calendars() -> None:
        """Create a calendar for companions we have not seen yet."""
        new = [
            companion
            for companion_id, companion in coordinator.data.companions.items()
            if companion_id not in known
        ]
        if not new:
            return
        known.update(companion.id for companion in new)
        async_add_entities(
            EinVaultCompanionCalendar(coordinator, calendar_coordinator, companion)
            for companion in new
        )

    async_add_entities([EinVaultShiftCalendar(coordinator, calendar_coordinator)])
    _async_add_companion_calendars()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_companion_calendars))


def _is_all_day(value: date | datetime) -> bool:
    """Whether an ICS value represents a whole day.

    ``datetime`` is a subclass of ``date``, so the check has to be this way
    round.
    """
    return not isinstance(value, datetime)


def _ensure_tz(value: date | datetime) -> date | datetime:
    """Give a floating ICS time a timezone.

    EinVault emits ``DTSTART`` without a TZID whenever it cannot build a
    VTIMEZONE block for the configured zone, which RFC 5545 defines as
    *floating* local time — it means "whatever the local time is for whoever is
    reading this". Home Assistant refuses naive datetimes outright, so they are
    resolved against Home Assistant's own timezone, which is the closest
    reading of the spec's intent. Dates (all-day events) are left alone.
    """
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
    return value


def _to_ha_event(event: Event) -> CalendarEvent:
    """Convert an expanded ICS occurrence into a Home Assistant event."""
    start = _ensure_tz(event.start)
    end = _ensure_tz(event.end) if event.end is not None else start

    # A zero-length event renders as nothing in the UI. EinVault emits these
    # for point-in-time reminders, which have a due moment but no duration.
    if not _is_all_day(start) and start == end:
        end = start + timedelta(minutes=30)

    return CalendarEvent(
        summary=event.summary or "",
        start=start,
        end=end,
        description=event.description,
        location=event.location,
        uid=event.uid,
    )


class EinVaultCalendarBase(CalendarEntity):
    """Shared timeline handling for EinVault calendars."""

    _calendar_coordinator: EinVaultCalendarCoordinator

    def _matches(self, event: Event) -> bool:
        """Whether an event belongs on this calendar."""
        raise NotImplementedError

    def _occurrences(self, start: datetime, end: datetime) -> list[Event]:
        """Expand the feed over a window and keep what belongs here."""
        if not self._calendar_coordinator.last_update_success:
            # The first fetch can fail without raising, leaving data unset.
            return []
        calendar = self._calendar_coordinator.data
        timeline = calendar.timeline_tz(start.tzinfo or dt_util.UTC)
        return [event for event in timeline.overlapping(start, end) if self._matches(event)]

    @property
    def event(self) -> CalendarEvent | None:
        """Return the next upcoming event.

        Recurrence is expanded over a bounded window rather than to infinity —
        a yearly reminder would otherwise require walking every occurrence.
        """
        now = dt_util.now()
        upcoming = self._occurrences(now, now + timedelta(days=365))
        if not upcoming:
            return None
        return _to_ha_event(upcoming[0])

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        """Return every event in a window."""
        return [_to_ha_event(event) for event in self._occurrences(start_date, end_date)]


class EinVaultCompanionCalendar(EinVaultCompanionEntity, EinVaultCalendarBase, CalendarEntity):
    """Reminders and health events for one companion."""

    def __init__(
        self,
        coordinator: EinVaultDataUpdateCoordinator,
        calendar_coordinator: EinVaultCalendarCoordinator,
        companion: Companion,
    ) -> None:
        """Initialise the calendar."""
        super().__init__(coordinator, companion, "calendar")
        self._calendar_coordinator = calendar_coordinator
        self._attr_translation_key = "companion"

    async def async_added_to_hass(self) -> None:
        """Track both coordinators.

        Companion metadata comes from the API coordinator; the events come from
        the feed coordinator, which refreshes on its own slower cadence.
        """
        await super().async_added_to_hass()
        self.async_on_remove(
            self._calendar_coordinator.async_add_listener(self.async_write_ha_state)
        )

    @property
    def available(self) -> bool:
        """Available only when the feed has parsed and the companion exists."""
        return super().available and self._calendar_coordinator.last_update_success

    def _matches(self, event: Event) -> bool:
        """Match on the companion name EinVault puts in CATEGORIES.

        The feed identifies companions by name, not id — a rename changes both
        the feed and our copy of the companion at the same time, so the match
        stays correct. Two companions sharing a name is the one case this
        cannot separate; both calendars would then show both sets of events.
        """
        companion = self.companion
        if companion is None:
            return False
        return companion.name in (event.categories or [])


class EinVaultShiftCalendar(EinVaultInstanceEntity, EinVaultCalendarBase, CalendarEntity):
    """Caretaker shifts, which belong to the instance rather than a companion."""

    def __init__(
        self,
        coordinator: EinVaultDataUpdateCoordinator,
        calendar_coordinator: EinVaultCalendarCoordinator,
    ) -> None:
        """Initialise the calendar."""
        super().__init__(coordinator, "shift_calendar")
        self._calendar_coordinator = calendar_coordinator

    async def async_added_to_hass(self) -> None:
        """Track the feed coordinator as well as the API one."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._calendar_coordinator.async_add_listener(self.async_write_ha_state)
        )

    @property
    def available(self) -> bool:
        """Available once the feed has parsed successfully."""
        return super().available and self._calendar_coordinator.last_update_success

    def _matches(self, event: Event) -> bool:
        """Keep shift events only."""
        return SHIFT_CATEGORY in (event.categories or [])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Explain an empty calendar rather than leaving it mysterious."""
        return {
            "note": (
                "Shifts exist only for caretaker-role users and are created in "
                "EinVault under Admin, Users."
            )
        }
