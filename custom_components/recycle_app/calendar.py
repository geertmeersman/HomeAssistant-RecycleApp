"""RecycleApp Calendar."""

from datetime import date, datetime
import logging

from homeassistant import config_entries
from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.const import ATTR_FRIENDLY_NAME, Platform
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    callback,
)
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
import homeassistant.helpers.entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from .api import FostPlusApi
from .const import DOMAIN, MANUFACTURER, WEBSITE
from .info import AppInfo
from .recycling_park_calendar import RecyclingParkCalendarEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: config_entries.ConfigEntry,
    async_add_entities: AddEntitiesCallback,
):
    """Set up the RecycleApp Calendar platform.

    This function is called by Home Assistant after the user has created a config entry
    for the RecycleApp integration.

    The function will create a calendar entity for the user's street and house number,
    and if the user has selected recycling parks in the options, it will create a
    separate calendar entity for each of those parks.

    The function will also add the calendar entity to the list of entities to be
    tracked by Home Assistant.

    Args:
        hass: The Home Assistant instance.
        config_entry: The config entry containing the user configuration.
        async_add_entities: Callback to add new entities to Home Assistant.

    Returns:
        None

    Raises:
        ConfigEntryNotReady: If required data is not yet available.

    """
    app_info: AppInfo = hass.data[DOMAIN][config_entry.entry_id]
    fractions: dict[str, tuple[str, str]] = config_entry.options.get("fractions")
    config = config_entry.data
    zip_code_id: str = config["zipCodeId"]
    street_id: str = config["streetId"]
    house_number: int = config["houseNumber"]

    unique_id = app_info["unique_id"]
    entities = [
        RecycleAppCalendarEntity(
            coordinator=app_info["collect_coordinator"],
            zip_code_id=zip_code_id,
            street_id=street_id,
            house_number=house_number,
            unique_id=f"{unique_id}-calendar",
            device_info=app_info["collect_device"],
            fractions=fractions,
        )
    ]

    parks: list[str] = config_entry.options.get("parks", [])

    if len(parks) > 0:
        parks_found = app_info["recycling_park_coordinator"].data
        for park_id, park_info in parks_found.items():
            if park_id not in parks:
                continue
            device_info = DeviceInfo(
                entry_type=DeviceEntryType.SERVICE,
                identifiers={(DOMAIN, f"{unique_id}-{park_id}")},
                name=park_info["name"],
                manufacturer=MANUFACTURER,
                model="Recycling park",
                configuration_url=WEBSITE,
            )

            entities.append(
                RecyclingParkCalendarEntity(
                    app_info["recycling_park_coordinator"],
                    f"{unique_id}-{park_id}-calendar",
                    park_id,
                    device_info,
                )
            )

    async_add_entities(entities)


class RecycleAppCalendarEntity(
    CoordinatorEntity[DataUpdateCoordinator[dict[str, list[date]]]], CalendarEntity
):
    """Representation of a Collect Calendar element."""

    _attr_has_entity_name = True
    _attr_name = None

    def __init__(
        self,
        coordinator: DataUpdateCoordinator[dict[str, list[date]]],
        zip_code_id: str,
        street_id: str,
        house_number: int,
        unique_id: str,
        fractions: dict[str, tuple[str, str]],
        device_info: DeviceInfo,
    ) -> None:
        """Initialize a Calendar entity.

        Args:
            coordinator: The coordinator for this entity
            zip_code_id: The zip code id
            street_id: The street id
            house_number: The house number
            unique_id: A unique id for this entity
            fractions: A dictionary mapping fraction keys to tuples of (fraction name, fraction id)
            device_info: The device info for this entity

        """
        super().__init__(coordinator)
        self._zip_code_id = zip_code_id
        self._street_id = street_id
        self._house_number = house_number
        self._fractions = fractions
        self._attr_unique_id = unique_id
        self._attr_device_info = device_info
        self._remove_change_listener: CALLBACK_TYPE | None = None

    @property
    def event(self) -> CalendarEvent | None:
        """Return the next upcoming event."""
        if self._remove_change_listener:
            self._remove_change_listener()
            self._remove_change_listener = None
        next_collect: date = date.max
        labels: list[str] | None = None
        base_id = self.unique_id.replace("-calendar", "-")
        entity_registry = er.async_get(self.hass)
        if self.coordinator.data is None:
            return None

        entity_ids: list[str] = []
        for fraction_id, event_dates in self.coordinator.data.items():
            entity_id = entity_registry.async_get_entity_id(
                Platform.SENSOR, DOMAIN, base_id + fraction_id
            )
            if not entity_id:
                continue

            entity_ids.append(entity_id)
            state = self.hass.states.get(entity_id)
            if not state:
                continue

            if event_dates[0] > next_collect:
                continue

            if event_dates[0] < next_collect:
                labels = []
                next_collect = event_dates[0]
            labels.append(
                state.attributes.get(
                    ATTR_FRIENDLY_NAME, self._fractions[fraction_id][1]
                )
            )

        @callback
        def update(_event: Event[EventStateChangedData]) -> None:
            """Update state and reschedule next alarms."""
            _LOGGER.debug("Update %s: tracked dependencies", self.entity_id)
            self.async_write_ha_state()

        if not labels:
            self._remove_change_listener = async_track_state_change_event(
                self.hass, entity_ids, update
            )
            return None

        return CalendarEvent(
            start=next_collect,
            end=next_collect,
            summary=" - ".join(labels),
        )

    async def async_will_remove_from_hass(self) -> None:
        """Run when entity will be removed from hass.

        Remove listeners.
        """
        await super().async_will_remove_from_hass()
        if self._remove_change_listener:
            self._remove_change_listener()
        self._remove_change_listener = None

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        """Return calendar events within a datetime range.

        This is only called when opening the calendar in the UI.

        Args:
            hass: The Home Assistant instance.
            start_date: The start of the datetime range to fetch events for.
            end_date: The end of the datetime range to fetch events for.

        Returns:
            list[CalendarEvent]: A list of calendar events within the specified date range,
                sorted by start date.

        """
        api = FostPlusApi()
        base_id = self.unique_id.replace("-calendar", "-")
        entity_registry = er.async_get(hass)
        collections: dict[str, list[date]] = await self.hass.async_add_executor_job(
            api.get_collections,
            self._zip_code_id,
            self._street_id,
            self._house_number,
            start_date,
            end_date,
        )

        events = [
            CalendarEvent(
                start=d,
                end=d,
                summary=state.attributes.get(
                    ATTR_FRIENDLY_NAME, self._fractions[collection_type][1]
                ),
            )
            for collection_type, dates in collections.items()
            if (
                entity_id := entity_registry.async_get_entity_id(
                    Platform.SENSOR, DOMAIN, base_id + collection_type
                )
            )
            if (state := hass.states.get(entity_id))
            for d in dates
        ]

        return sorted(events, key=lambda e: e.start)
