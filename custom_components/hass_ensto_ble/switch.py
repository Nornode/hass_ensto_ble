"""Support for Ensto BLE switches."""
import logging
from datetime import datetime, timedelta

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util

from .base_entity import EnstoCoordinatorEntity, EnstoConfigEntity
from . import EnstoConfigEntry

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1
SCAN_INTERVAL = timedelta(seconds=30)

async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnstoConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up switches from a config entry."""
    manager = entry.runtime_data
    coordinator = manager.get_real_time_coordinator()

    switches = [
        EnstoBoostSwitch(coordinator, manager),
        EnstoAdaptiveTempSwitch(manager),
        EnstoDaylightSavingSwitch(manager),
        EnstoVacationModeSwitch(manager),
        EnstoCalendarModeSwitch(manager),
    ]
    async_add_entities(switches, False)

class EnstoBoostSwitch(EnstoCoordinatorEntity, SwitchEntity):
    """Representation of Ensto Boost switch backed by coordinator."""

    def __init__(self, coordinator, manager):
        super().__init__(coordinator, manager)
        self._attr_name = "Boost mode"
        self._attr_unique_id = f"{dr.format_mac(self._manager.mac_address)}_boost_switch"

    @property
    def is_on(self) -> bool:
        """Return true if boost mode is active, directly from real-time data."""
        if self.coordinator.data:
            return bool(self.coordinator.data.get("boost_enabled", False))
        return False

    async def async_turn_on(self, **kwargs) -> None:
        """Turn the boost on."""
        try:
            settings = self._manager.config_data.get("boost") or await self._manager.read_boost()
            if settings:
                await self._manager.write_boost(
                    enabled=True,
                    offset_degrees=settings.get("offset_degrees", 2.0),
                    offset_percentage=settings.get("offset_percentage", 0),
                    duration_minutes=settings.get("setpoint_minutes", 60)
                )
                settings["enabled"] = True
                self._manager.config_data["boost"] = settings
                if self.coordinator.data:
                    self.coordinator.data["boost_enabled"] = True
                self.async_write_ha_state()
                await self.coordinator.async_request_refresh()
        except Exception as e:
            _LOGGER.error("Failed to enable boost mode: %s", e)

    async def async_turn_off(self, **kwargs) -> None:
        """Turn the boost off."""
        try:
            settings = self._manager.config_data.get("boost") or await self._manager.read_boost()
            if settings:
                await self._manager.write_boost(
                    enabled=False,
                    offset_degrees=settings.get("offset_degrees", 2.0),
                    offset_percentage=settings.get("offset_percentage", 0),
                    duration_minutes=settings.get("setpoint_minutes", 60)
                )
                settings["enabled"] = False
                self._manager.config_data["boost"] = settings
                if self.coordinator.data:
                    self.coordinator.data["boost_enabled"] = False
                self.async_write_ha_state()
                await self.coordinator.async_request_refresh()
        except Exception as e:
            _LOGGER.error("Failed to disable boost mode: %s", e)

class EnstoAdaptiveTempSwitch(EnstoConfigEntity, SwitchEntity):
    """Representation of Ensto Adaptive Temperature Control switch."""

    def __init__(self, manager):
        super().__init__(manager)
        self._attr_name = "Adaptive temperature control"
        self._attr_unique_id = f"{dr.format_mac(self._manager.mac_address)}_adaptive_temp_switch"

    @property
    def is_on(self) -> bool:
        """Return true if adaptive temperature control is active."""
        return bool(self._manager.config_data.get("adaptive_temp", False))

    async def async_turn_on(self, **kwargs) -> None:
        """Turn adaptive temperature control on."""
        try:
            success = await self._manager.write_adaptive_temp_control(True)
            if success:
                self._manager.config_data["adaptive_temp"] = True
                self.async_write_ha_state()
                if self._manager.coordinator:
                    await self._manager.coordinator.async_request_refresh()
        except Exception as e:
            _LOGGER.error("Failed to enable adaptive temperature control: %s", e)

    async def async_turn_off(self, **kwargs) -> None:
        """Turn adaptive temperature control off."""
        try:
            success = await self._manager.write_adaptive_temp_control(False)
            if success:
                self._manager.config_data["adaptive_temp"] = False
                self.async_write_ha_state()
                if self._manager.coordinator:
                    await self._manager.coordinator.async_request_refresh()
        except Exception as e:
            _LOGGER.error("Failed to disable adaptive temperature control: %s", e)

class EnstoDaylightSavingSwitch(EnstoConfigEntity, SwitchEntity):
    """Representation of Ensto Daylight Saving switch."""

    def __init__(self, manager):
        super().__init__(manager)
        self._attr_name = "Daylight saving"
        self._attr_unique_id = f"{dr.format_mac(self._manager.mac_address)}_daylight_saving_switch"

    @property
    def is_on(self) -> bool:
        """Return true if daylight saving is enabled."""
        dst = self._manager.config_data.get("daylight_saving")
        if dst:
            return bool(dst.get("enabled", False))
        return False

    async def async_turn_on(self, **kwargs) -> None:
        """Turn daylight saving on."""
        ha_tz = dt_util.DEFAULT_TIME_ZONE
        january_utc = datetime(2025, 1, 15, 12, 0, 0, tzinfo=dt_util.UTC)
        january_local = january_utc.astimezone(ha_tz)
        tz_offset = int(january_local.utcoffset().total_seconds() / 60)

        success = await self._manager.write_daylight_saving(
            enabled=True,
            winter_to_summer=0,
            summer_to_winter=0,
            timezone_offset=tz_offset
        )
        if success:
            self._manager.config_data["daylight_saving"] = {"enabled": True, "timezone_offset": tz_offset}
            self.async_write_ha_state()
            if self._manager.coordinator:
                await self._manager.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        """Turn daylight saving off."""
        ha_tz = dt_util.DEFAULT_TIME_ZONE
        january_utc = datetime(2025, 1, 15, 12, 0, 0, tzinfo=dt_util.UTC)
        january_local = january_utc.astimezone(ha_tz)
        tz_offset = int(january_local.utcoffset().total_seconds() / 60)

        success = await self._manager.write_daylight_saving(
            enabled=False,
            winter_to_summer=0,
            summer_to_winter=0,
            timezone_offset=tz_offset
        )
        if success:
            self._manager.config_data["daylight_saving"] = {"enabled": False, "timezone_offset": tz_offset}
            self.async_write_ha_state()
            if self._manager.coordinator:
                await self._manager.coordinator.async_request_refresh()

class EnstoVacationModeSwitch(EnstoConfigEntity, SwitchEntity):
    """Representation of Ensto Vacation Mode switch."""

    def __init__(self, manager):
        super().__init__(manager)
        self._attr_name = "Vacation mode"
        self._attr_unique_id = f"{dr.format_mac(self._manager.mac_address)}_vacation_mode_switch"
        self._attr_icon = "mdi:palm-tree"

    @property
    def is_on(self) -> bool:
        """Return true if vacation mode is active."""
        if self._manager.coordinator and self._manager.coordinator.data:
            if self._manager.coordinator.data.get("active_mode") == "Vacation":
                return True
        vac = self._manager.config_data.get("vacation_time")
        if vac:
            return bool(vac.get("enabled", False))
        return False

    async def async_turn_on(self, **kwargs) -> None:
        """Turn vacation mode on."""
        try:
            settings = self._manager.config_data.get("vacation_time") or await self._manager.read_vacation_time()
            if settings:
                await self._manager.write_vacation_time(
                    time_from=settings['time_from'],
                    time_to=settings['time_to'],
                    offset_temperature=settings['offset_temperature'],
                    offset_percentage=settings['offset_percentage'],
                    enabled=True
                )
                settings["enabled"] = True
                self._manager.config_data["vacation_time"] = settings
                self.async_write_ha_state()
                async_dispatcher_send(self.hass, f"ensto_datetime_update_{self._manager.mac_address}")
                if self._manager.coordinator:
                    await self._manager.coordinator.async_request_refresh()
        except Exception as e:
            _LOGGER.error("Failed to enable vacation mode: %s", e)

    async def async_turn_off(self, **kwargs) -> None:
        """Turn vacation mode off."""
        try:
            settings = self._manager.config_data.get("vacation_time") or await self._manager.read_vacation_time()
            if settings:
                await self._manager.write_vacation_time(
                    time_from=settings['time_from'],
                    time_to=settings['time_to'],
                    offset_temperature=settings['offset_temperature'],
                    offset_percentage=settings['offset_percentage'],
                    enabled=False
                )
                settings["enabled"] = False
                self._manager.config_data["vacation_time"] = settings
                self.async_write_ha_state()
                async_dispatcher_send(self.hass, f"ensto_datetime_update_{self._manager.mac_address}")
                if self._manager.coordinator:
                    await self._manager.coordinator.async_request_refresh()
        except Exception as e:
            _LOGGER.error("Failed to disable vacation mode: %s", e)

class EnstoCalendarModeSwitch(EnstoConfigEntity, SwitchEntity):
    """Representation of Ensto Calendar Mode switch."""

    def __init__(self, manager):
        super().__init__(manager)
        self._attr_name = "Calendar mode"
        self._attr_unique_id = f"{dr.format_mac(self._manager.mac_address)}_calendar_mode_switch"
        self._attr_icon = "mdi:calendar-clock"

    @property
    def is_on(self) -> bool:
        """Return true if calendar mode is active."""
        if self._manager.coordinator and self._manager.coordinator.data:
            if self._manager.coordinator.data.get("active_mode") == "Calendar":
                return True
        cal = self._manager.config_data.get("calendar_mode")
        if cal:
            return bool(cal.get("enabled", False))
        return False

    async def async_turn_on(self, **kwargs) -> None:
        """Turn calendar mode on."""
        try:
            success = await self._manager.write_calendar_mode(True)
            if success:
                self._manager.config_data["calendar_mode"] = {"enabled": True}
                self.async_write_ha_state()
                if self._manager.coordinator:
                    await self._manager.coordinator.async_request_refresh()
        except Exception as e:
            _LOGGER.error("Failed to enable calendar mode: %s", e)

    async def async_turn_off(self, **kwargs) -> None:
        """Turn calendar mode off."""
        try:
            success = await self._manager.write_calendar_mode(False)
            if success:
                self._manager.config_data["calendar_mode"] = {"enabled": False}
                self.async_write_ha_state()
                if self._manager.coordinator:
                    await self._manager.coordinator.async_request_refresh()
        except Exception as e:
            _LOGGER.error("Failed to disable calendar mode: %s", e)
