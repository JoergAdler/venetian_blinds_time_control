import logging
from datetime import datetime, timedelta
import asyncio

from homeassistant.components.cover import (
    ATTR_CURRENT_POSITION,
    ATTR_CURRENT_TILT_POSITION,
    ATTR_POSITION,
    ATTR_TILT_POSITION,
    CoverEntityFeature,
    CoverEntity,
)
from homeassistant.const import (
    SERVICE_CLOSE_COVER,
    SERVICE_OPEN_COVER,
    SERVICE_STOP_COVER,
)
from homeassistant.core import callback, HomeAssistant
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers import entity_platform
from homeassistant.helpers.device_registry import DeviceInfo
import voluptuous as vol

from .calculator import TravelCalculator, TravelStatus
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

SERVICE_SET_KNOWN_POSITION = "set_known_position"
SERVICE_SET_KNOWN_TILT_POSITION = "set_known_tilt_position"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    """Set up the blinds cover from a config entry."""
    async_add_entities([BlindsCover(hass, entry)])

    platform = entity_platform.async_get_current_platform()

    platform.async_register_entity_service(
        SERVICE_SET_KNOWN_POSITION,
        {vol.Required("position"): vol.All(vol.Coerce(int), vol.Range(min=0, max=100))},
        "async_set_known_position",
    )
    platform.async_register_entity_service(
        SERVICE_SET_KNOWN_TILT_POSITION,
        {vol.Required("position"): vol.All(vol.Coerce(int), vol.Range(min=0, max=100))},
        "async_set_known_tilt_position",
    )


class BlindsCover(CoverEntity, RestoreEntity):
    """Representation of a blinds cover."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        """Initialize the cover."""
        self.hass = hass
        self.entry = entry

        entry.async_on_unload(entry.add_update_listener(self.async_options_updated))

        self._attr_name = self.entry.options.get("ent_name", self.entry.data.get("ent_name"))
        self._attr_unique_id = f"cover_timebased_synced_uuid_{entry.entry_id}"
        self._attr_should_poll = False
        self._attr_device_class = "blind"

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self.entry.entry_id)},
            name=self._attr_name,
            manufacturer="Blinds Controller (Custom)",
            model="Time-Based",
        )

        self._configure_entity()

        self._unsubscribe_auto_updater = None

        self.travel_calc = TravelCalculator(
            self._travel_time_down, self._travel_time_up, self._startup_delay
        )
        self.tilt_calc = None
        if self.has_tilt_support():
            self.tilt_calc = TravelCalculator(
                self._travel_tilt_closed, self._travel_tilt_open, self._startup_delay
            )

        # Tracks which phase we are in: "tilt" or "cover" or None
        self._movement_phase = None

    def _configure_entity(self):
        """Read configuration from options or data."""
        self._travel_time_down = self.entry.options.get("time_down", self.entry.data.get("time_down", 30.0))
        self._travel_time_up = self.entry.options.get("time_up", self.entry.data.get("time_up", 30.0))
        self._travel_tilt_closed = self.entry.options.get("tilt_closed", self.entry.data.get("tilt_closed", 1.5))
        self._travel_tilt_open = self.entry.options.get("tilt_open", self.entry.data.get("tilt_open", 1.5))
        self._startup_delay = self.entry.options.get("startup_delay", self.entry.data.get("startup_delay", 0.0))
        self._up_switch_entity_id = self.entry.options.get("entity_up", self.entry.data.get("entity_up"))
        self._down_switch_entity_id = self.entry.options.get("entity_down", self.entry.data.get("entity_down"))
        self._send_stop_at_end = self.entry.options.get("send_stop_at_end", self.entry.data.get("send_stop_at_end", True))

    @staticmethod
    async def async_options_updated(hass: HomeAssistant, entry: ConfigEntry):
        """Handle options update."""
        await hass.config_entries.async_reload(entry.entry_id)

    @property
    def supported_features(self) -> CoverEntityFeature:
        """Flag supported features."""
        supported_features = (
            CoverEntityFeature.OPEN
            | CoverEntityFeature.CLOSE
            | CoverEntityFeature.STOP
            | CoverEntityFeature.SET_POSITION
        )
        if self.has_tilt_support():
            supported_features |= (
                CoverEntityFeature.OPEN_TILT
                | CoverEntityFeature.CLOSE_TILT
                | CoverEntityFeature.STOP_TILT
                | CoverEntityFeature.SET_TILT_POSITION
            )
        return supported_features

    @property
    def current_cover_position(self) -> int | None:
        """Return current position of cover."""
        return self.travel_calc.current_position()

    @property
    def current_cover_tilt_position(self) -> int | None:
        """Return current tilt position of cover."""
        if self.has_tilt_support():
            return self.tilt_calc.current_position()
        return None

    @property
    def is_opening(self) -> bool:
        """Return if the cover is opening or not."""
        return self.travel_calc.travel_direction == TravelStatus.DIRECTION_UP

    @property
    def is_closing(self) -> bool:
        """Return if the cover is closing or not."""
        return self.travel_calc.travel_direction == TravelStatus.DIRECTION_DOWN

    @property
    def is_closed(self) -> bool:
        """Return if the cover is closed or not."""
        return self.travel_calc.is_closed()

    def has_tilt_support(self) -> bool:
        """Check if tilt is supported."""
        return self._travel_tilt_open > 0 and self._travel_tilt_closed > 0

    # ------------------------------------------------------------------
    # Open: tilt fully open first, then raise cover
    # ------------------------------------------------------------------

    async def async_open_cover(self, **kwargs):
        """Open the cover — tilt first, then raise."""
        if not self.has_tilt_support():
            if self.travel_calc.current_position() < 100:
                self.travel_calc.start_travel_up()
                self.start_auto_updater()
                await self._async_handle_command(SERVICE_OPEN_COVER)
            return

        if self.tilt_calc.current_position() < 100:
            # Phase 1: open the tilt fully
            self._movement_phase = "tilt"
            self._pending_cover_target = 100
            self.tilt_calc.start_travel_up()
            self.start_auto_updater()
            await self._async_handle_command(SERVICE_OPEN_COVER)
        elif self.travel_calc.current_position() < 100:
            # Tilt already open, go straight to raising cover
            self._movement_phase = "cover"
            self.travel_calc.start_travel_up()
            self.start_auto_updater()
            await self._async_handle_command(SERVICE_OPEN_COVER)

    async def async_close_cover(self, **kwargs):
        """Close the cover — tilt first, then lower."""
        if not self.has_tilt_support():
            if self.travel_calc.current_position() > 0:
                self.travel_calc.start_travel_down()
                self.start_auto_updater()
                await self._async_handle_command(SERVICE_CLOSE_COVER)
            return

        if self.tilt_calc.current_position() > 0:
            # Phase 1: close the tilt fully
            self._movement_phase = "tilt"
            self._pending_cover_target = 0
            self.tilt_calc.start_travel_down()
            self.start_auto_updater()
            await self._async_handle_command(SERVICE_CLOSE_COVER)
        elif self.travel_calc.current_position() > 0:
            # Tilt already closed, go straight to lowering cover
            self._movement_phase = "cover"
            self.travel_calc.start_travel_down()
            self.start_auto_updater()
            await self._async_handle_command(SERVICE_CLOSE_COVER)

    async def async_stop_cover(self, **kwargs):
        """Stop the cover and tilt immediately."""
        self._movement_phase = None
        self._pending_cover_target = None
        self.travel_calc.stop()
        if self.has_tilt_support():
            self.tilt_calc.stop()
        self.stop_auto_updater()
        await self._async_handle_command(SERVICE_STOP_COVER)

    async def async_set_cover_position(self, **kwargs):
        """Move the cover to a specific position, resetting tilt first."""
        position = kwargs[ATTR_POSITION]
        current_position = self.travel_calc.current_position()

        if position == current_position:
            return

        command = SERVICE_OPEN_COVER if position > current_position else SERVICE_CLOSE_COVER

        if self.has_tilt_support():
            # Reset tilt to fully open (going up) or fully closed (going down) first
            tilt_target = 100 if position > current_position else 0
            tilt_current = self.tilt_calc.current_position()

            if tilt_current != tilt_target:
                self._movement_phase = "tilt"
                self._pending_cover_target = position
                if tilt_target == 100:
                    self.tilt_calc.start_travel_up()
                else:
                    self.tilt_calc.start_travel_down()
                self.start_auto_updater()
                await self._async_handle_command(command)
                return

        # No tilt adjustment needed, move cover directly
        self._movement_phase = "cover"
        self.travel_calc.start_travel(position)
        self.start_auto_updater()
        await self._async_handle_command(command)

    # ------------------------------------------------------------------
    # Tilt-only commands (no cover movement)
    # ------------------------------------------------------------------

    async def async_open_cover_tilt(self, **kwargs):
        """Open the tilt only."""
        if self.has_tilt_support() and self.tilt_calc.current_position() < 100:
            self._movement_phase = "tilt_only"
            self.tilt_calc.start_travel_up()
            self.start_auto_updater()
            await self._async_handle_command(SERVICE_OPEN_COVER)

    async def async_close_cover_tilt(self, **kwargs):
        """Close the tilt only."""
        if self.has_tilt_support() and self.tilt_calc.current_position() > 0:
            self._movement_phase = "tilt_only"
            self.tilt_calc.start_travel_down()
            self.start_auto_updater()
            await self._async_handle_command(SERVICE_CLOSE_COVER)

    async def async_stop_cover_tilt(self, **kwargs):
        """Stop the tilt."""
        await self.async_stop_cover()

    async def async_set_cover_tilt_position(self, **kwargs):
        """Move the tilt to a specific position only."""
        if not self.has_tilt_support():
            return

        position = kwargs[ATTR_TILT_POSITION]
        current_tilt_position = self.tilt_calc.current_position()

        if position == current_tilt_position:
            return

        command = SERVICE_OPEN_COVER if position > current_tilt_position else SERVICE_CLOSE_COVER
        self._movement_phase = "tilt_only"
        self.tilt_calc.start_travel(position)
        self.start_auto_updater()
        await self._async_handle_command(command)

    # ------------------------------------------------------------------
    # Auto updater — handles phase transitions
    # ------------------------------------------------------------------

    def start_auto_updater(self):
        """Start the auto updater to update HASS state."""
        if self._unsubscribe_auto_updater is None:
            interval = timedelta(seconds=0.1)
            self._unsubscribe_auto_updater = async_track_time_interval(
                self.hass, self.auto_updater_hook, interval
            )

    def stop_auto_updater(self):
        """Stop the auto updater."""
        if self._unsubscribe_auto_updater is not None:
            self._unsubscribe_auto_updater()
            self._unsubscribe_auto_updater = None

    @callback
    def auto_updater_hook(self, now: datetime) -> None:
        """Call for the updater — handles sequential tilt → cover phases."""
        self.async_schedule_update_ha_state()

        if self._movement_phase in ("tilt", "tilt_only"):
            if self.tilt_calc.position_reached():
                self.tilt_calc.stop()

                if self._movement_phase == "tilt_only":
                    # Done — no cover movement to follow
                    self._movement_phase = None
                    self.stop_auto_updater()
                    self.hass.async_create_task(self.auto_stop_if_necessary())
                else:
                    # Tilt phase done — transition to cover phase
                    self._movement_phase = "cover"
                    target = self._pending_cover_target
                    self._pending_cover_target = None

                    async def _start_cover_phase():
                        if target == 100:
                            self.travel_calc.start_travel_up()
                            await self._async_handle_command(SERVICE_OPEN_COVER)
                        elif target == 0:
                            self.travel_calc.start_travel_down()
                            await self._async_handle_command(SERVICE_CLOSE_COVER)
                        else:
                            current = self.travel_calc.current_position()
                            cmd = SERVICE_OPEN_COVER if target > current else SERVICE_CLOSE_COVER
                            self.travel_calc.start_travel(target)
                            await self._async_handle_command(cmd)

                    self.hass.async_create_task(_start_cover_phase())

                self.async_write_ha_state()

        elif self._movement_phase == "cover":
            if self.travel_calc.position_reached():
                self.travel_calc.stop()
                self._movement_phase = None
                self.stop_auto_updater()
                self.hass.async_create_task(self.auto_stop_if_necessary())
                self.async_write_ha_state()

    async def auto_stop_if_necessary(self):
        """Send stop command if required."""
        if self._send_stop_at_end:
            _LOGGER.debug("Auto-stopping cover %s as it reached its final position.", self.name)
            await self._async_handle_command(SERVICE_STOP_COVER)

    async def _async_handle_command(self, command: str) -> None:
        """Handle the cover commands."""
        if command == SERVICE_OPEN_COVER:
            await self.hass.services.async_call("switch", "turn_off", {"entity_id": self._down_switch_entity_id}, blocking=True)
            await asyncio.sleep(0.1)
            await self.hass.services.async_call("switch", "turn_on", {"entity_id": self._up_switch_entity_id}, blocking=True)
        elif command == SERVICE_CLOSE_COVER:
            await self.hass.services.async_call("switch", "turn_off", {"entity_id": self._up_switch_entity_id}, blocking=True)
            await asyncio.sleep(0.1)
            await self.hass.services.async_call("switch", "turn_on", {"entity_id": self._down_switch_entity_id}, blocking=True)
        elif command == SERVICE_STOP_COVER:
            await self.hass.services.async_call("switch", "turn_off", {"entity_id": self._up_switch_entity_id}, blocking=True)
            await self.hass.services.async_call("switch", "turn_off", {"entity_id": self._down_switch_entity_id}, blocking=True)

        self.async_write_ha_state()

    async def async_added_to_hass(self):
        """Call when entity is added to hass."""
        await super().async_added_to_hass()
        old_state = await self.async_get_last_state()
        if old_state and old_state.attributes.get(ATTR_CURRENT_POSITION) is not None:
            self.travel_calc.set_position(int(old_state.attributes.get(ATTR_CURRENT_POSITION)))
            if self.has_tilt_support() and old_state.attributes.get(ATTR_CURRENT_TILT_POSITION) is not None:
                self.tilt_calc.set_position(int(old_state.attributes.get(ATTR_CURRENT_TILT_POSITION)))

    async def async_set_known_position(self, position: int):
        """Service to set the known position of the cover."""
        self.travel_calc.set_position(position)
        self.async_write_ha_state()

    async def async_set_known_tilt_position(self, position: int):
        """Service to set the known tilt position of the cover."""
        if self.has_tilt_support():
            self.tilt_calc.set_position(position)
            self.async_write_ha_state()
