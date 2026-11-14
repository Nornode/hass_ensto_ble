"""Support for Ensto BLE selects."""
import logging
from datetime import timedelta
from typing import Optional

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers import device_registry as dr

from .base_entity import EnstoCoordinatorEntity, EnstoConfigEntity
from .const import (
    FLOOR_SENSOR_CONFIG, MODE_MAP, SUPPORTED_MODES_ECO16, SUPPORTED_MODES_ELTE6,
    EXTERNAL_CONTROL_MODES,
)

from . import EnstoConfigEntry

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1
SCAN_INTERVAL = timedelta(seconds=30)

FLOOR_SENSOR_TYPES = {v['sensor_type']: k for k, v in FLOOR_SENSOR_CONFIG.items()}

async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnstoConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up selects from a config entry."""
    manager = entry.runtime_data
    coordinator = manager.get_real_time_coordinator()
    
    selects = [EnstoHeatingModeSelect(coordinator, manager)]
    
    model = manager.model_number if manager.model_number else ""
    if "ECO16" in model:
        selects.append(EnstoFloorSensorSelect(manager))
    
    if manager.supports_external_control():
        selects.append(EnstoExternalControlModeSelect(manager))
    
    async_add_entities(selects, False)

class EnstoHeatingModeSelect(EnstoCoordinatorEntity, SelectEntity):
    """Representation of Ensto Heating Mode select backed by coordinator."""

    def __init__(self, coordinator, manager):
        super().__init__(coordinator, manager)
        self._attr_name = "Heating mode"
        self._attr_unique_id = f"{dr.format_mac(self._manager.mac_address)}_heating_mode_select"
        self._available_modes = None

    @property
    def current_option(self) -> Optional[str]:
        """Return current heating mode directly from coordinator real-time data."""
        if self.coordinator.data:
            return self.coordinator.data.get("heating_mode")
        return None

    @property
    def options(self) -> list[str]:
        """Return list of available heating modes."""
        if self._available_modes is None:
            model = self._manager.model_number if self._manager.model_number else ""
            if "ELTE6" in model:
                allowed_numbers = SUPPORTED_MODES_ELTE6
            else:
                allowed_numbers = SUPPORTED_MODES_ECO16

            self._available_modes = [MODE_MAP[num] for num in sorted(allowed_numbers)]
        return self._available_modes

    async def async_select_option(self, option: str) -> None:
        """Change heating mode on the device."""
        mode_number = next(num for num, name in MODE_MAP.items() if name == option)
        success = await self._manager.write_heating_mode(mode_number)
        if success:
            if self.coordinator.data:
                self.coordinator.data["heating_mode"] = option
            self._manager.config_data["heating_mode"] = {"mode_number": mode_number, "mode_name": option}
            self.async_write_ha_state()
            await self.coordinator.async_request_refresh()

class EnstoFloorSensorSelect(EnstoConfigEntity, SelectEntity):
    """Representation of Ensto Floor Sensor Type select."""

    def __init__(self, manager):
        super().__init__(manager)
        self._attr_name = "Floor sensor type"
        self._attr_unique_id = f"{dr.format_mac(self._manager.mac_address)}_floor_sensor_select"

    @property
    def current_option(self) -> Optional[str]:
        """Return current floor sensor type from config cache."""
        fs = self._manager.config_data.get("floor_sensor_type")
        if fs:
            sensor_type = fs.get("sensor_type")
            for name, cfg in FLOOR_SENSOR_CONFIG.items():
                if cfg["sensor_type"] == sensor_type:
                    return name
        return None

    @property
    def options(self) -> list[str]:
        """Return list of available floor sensor types."""
        return list(FLOOR_SENSOR_CONFIG.keys())

    async def async_select_option(self, option: str) -> None:
        """Change floor sensor type."""
        try:
            if option in FLOOR_SENSOR_CONFIG:
                success = await self._manager.write_floor_sensor_config(FLOOR_SENSOR_CONFIG[option])
                if success:
                    self._manager.config_data["floor_sensor_type"] = {
                        "sensor_type": FLOOR_SENSOR_CONFIG[option]["sensor_type"]
                    }
                    self.async_write_ha_state()
                    if self._manager.coordinator:
                        await self._manager.coordinator.async_request_refresh()
        except Exception as e:
            _LOGGER.error("Error setting floor sensor type: %s", e)

class EnstoExternalControlModeSelect(EnstoConfigEntity, SelectEntity):
    """Select entity for external control mode."""

    def __init__(self, manager):
        super().__init__(manager)
        self._attr_name = "External control mode"
        self._attr_unique_id = f"{dr.format_mac(self._manager.mac_address)}_external_control_mode"
        self._attr_icon = "mdi:thermostat"
        self._attr_options = list(EXTERNAL_CONTROL_MODES.values())

    @property
    def current_option(self) -> Optional[str]:
        """Return current mode from config cache."""
        fc = self._manager.config_data.get("force_control")
        if fc:
            return fc.get("mode_name", "Off")
        return "Off"

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return super().available

    async def async_select_option(self, option: str) -> None:
        """Change external control mode."""
        try:
            current = self._manager.config_data.get("force_control") or await self._manager.read_force_control()
            if current:
                mode = next(
                    num for num, name in EXTERNAL_CONTROL_MODES.items() 
                    if name == option
                )
                await self._manager.write_force_control(
                    mode=mode,
                    temperature=current.get('temperature', 20.0),
                    temperature_offset=current.get('temperature_offset', 0.0)
                )
                current["mode"] = mode
                current["mode_name"] = option
                self._manager.config_data["force_control"] = current
                self.async_write_ha_state()
                if self._manager.coordinator:
                    await self._manager.coordinator.async_request_refresh()
        except Exception as e:
            _LOGGER.error("Failed to set external control mode: %s", e)
