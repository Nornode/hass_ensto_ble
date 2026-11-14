"""Support for Ensto BLE number controls."""
import logging
from datetime import timedelta

from homeassistant.components.number import (
   NumberEntity,
   NumberDeviceClass,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers import device_registry as dr

from .base_entity import EnstoConfigEntity
from .const import CURRENCY_SYMBOLS
from .config_flow import CONF_CURRENCY, DEFAULT_CURRENCY

from . import EnstoConfigEntry

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1
SCAN_INTERVAL = timedelta(seconds=30)

async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnstoConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up numbers from a config entry."""
    manager = entry.runtime_data
    currency = entry.data.get(CONF_CURRENCY, DEFAULT_CURRENCY)
    
    entities = [
        EnstoBoostDurationNumber(manager),
        EnstoBoostOffsetNumber(manager),
        EnstoBoostPowerOffsetNumber(manager),
        EnstoRoomSensorCalibrationNumber(manager),
        EnstoHeatingPowerNumber(manager),
        EnstoEnergyUnitPriceNumber(manager, currency),
        EnstoVacationTempOffsetNumber(manager),
        EnstoVacationPowerOffsetNumber(manager),
    ]

    model = manager.model_number if manager.model_number else ""
    if "ECO16" in model:
        entities.extend([
            EnstoFloorLimitNumber(manager, "low"),
            EnstoFloorLimitNumber(manager, "high"),
            EnstoFloorAreaNumber(manager),
        ])

    if manager.supports_external_control():
        entities.extend([
            EnstoExternalControlTemperatureNumber(manager),
            EnstoExternalControlOffsetNumber(manager),
        ])

    async_add_entities(entities, False)

class EnstoBoostDurationNumber(EnstoConfigEntity, NumberEntity):
    """Number entity for controlling boost duration."""
   
    def __init__(self, manager):
        super().__init__(manager)
        self._attr_name = "Boost duration"
        self._attr_unique_id = f"{dr.format_mac(self._manager.mac_address)}_boost_duration"
        self._attr_native_min_value = 0
        self._attr_native_max_value = 180
        self._attr_native_step = 5
        self._attr_native_unit_of_measurement = "min"
        self._attr_mode = "box"

    @property
    def native_value(self) -> float | None:
        """Return the boost duration from config cache."""
        boost = self._manager.config_data.get("boost")
        if boost:
            return boost.get("setpoint_minutes")
        return None

    async def async_set_native_value(self, value: float) -> None:
        """Update the boost duration."""
        try:
            settings = self._manager.config_data.get("boost") or await self._manager.read_boost()
            if settings:
                await self._manager.write_boost(
                    enabled=settings.get("enabled", False),
                    offset_degrees=settings.get("offset_degrees", 0.0),
                    offset_percentage=settings.get("offset_percentage", 0),
                    duration_minutes=int(value)
                )
                settings["setpoint_minutes"] = int(value)
                self._manager.config_data["boost"] = settings
                self.async_write_ha_state()
                if self._manager.coordinator:
                    await self._manager.coordinator.async_request_refresh()
        except Exception as e:
            _LOGGER.error("Failed to set boost duration: %s", e)

class EnstoBoostOffsetNumber(EnstoConfigEntity, NumberEntity):
    """Number entity for controlling boost temperature offset."""

    def __init__(self, manager):
        super().__init__(manager)
        self._attr_name = "Boost temperature offset"
        self._attr_unique_id = f"{dr.format_mac(self._manager.mac_address)}_boost_temp_offset"
        self._attr_native_min_value = -20
        self._attr_native_max_value = 20
        self._attr_native_step = 0.5
        self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        self._attr_device_class = NumberDeviceClass.TEMPERATURE
        self._attr_mode = "box"

    @property
    def available(self) -> bool:
        """Return if entity is available (all modes except Power mode)."""
        if not super().available:
            return False
        if self._manager.coordinator and self._manager.coordinator.data:
            return self._manager.coordinator.data.get("heating_mode") != "Power"
        return True

    @property
    def native_value(self) -> float | None:
        """Return boost temperature offset from config cache."""
        boost = self._manager.config_data.get("boost")
        if boost:
            return boost.get("offset_degrees")
        return None

    async def async_set_native_value(self, value: float) -> None:
        """Update the boost temperature offset."""
        try:
            settings = self._manager.config_data.get("boost") or await self._manager.read_boost()
            if settings:
                await self._manager.write_boost(
                    enabled=settings.get("enabled", False),
                    offset_degrees=value,
                    offset_percentage=settings.get("offset_percentage", 0),
                    duration_minutes=settings.get("setpoint_minutes", 60)
                )
                settings["offset_degrees"] = value
                self._manager.config_data["boost"] = settings
                self.async_write_ha_state()
                if self._manager.coordinator:
                    await self._manager.coordinator.async_request_refresh()
        except Exception as e:
            _LOGGER.error("Failed to set boost temperature offset: %s", e)

class EnstoBoostPowerOffsetNumber(EnstoConfigEntity, NumberEntity):
    """Number entity for controlling boost power offset percentage."""

    def __init__(self, manager):
        super().__init__(manager)
        self._attr_name = "Boost power offset"
        self._attr_unique_id = f"{dr.format_mac(self._manager.mac_address)}_boost_power_offset"
        self._attr_native_min_value = -100
        self._attr_native_max_value = 100
        self._attr_native_step = 1
        self._attr_native_unit_of_measurement = "%"
        self._attr_mode = "box"

    @property
    def available(self) -> bool:
        """Return if entity is available (only in Power mode)."""
        if not super().available:
            return False
        if self._manager.coordinator and self._manager.coordinator.data:
            return self._manager.coordinator.data.get("heating_mode") == "Power"
        return False

    @property
    def native_value(self) -> float | None:
        """Return boost power offset from config cache."""
        boost = self._manager.config_data.get("boost")
        if boost:
            return boost.get("offset_percentage")
        return None

    async def async_set_native_value(self, value: float) -> None:
        """Update the boost power offset percentage."""
        try:
            settings = self._manager.config_data.get("boost") or await self._manager.read_boost()
            if settings:
                await self._manager.write_boost(
                    enabled=settings.get("enabled", False),
                    offset_degrees=settings.get("offset_degrees", 0.0),
                    offset_percentage=int(value),
                    duration_minutes=settings.get("setpoint_minutes", 60)
                )
                settings["offset_percentage"] = int(value)
                self._manager.config_data["boost"] = settings
                self.async_write_ha_state()
                if self._manager.coordinator:
                    await self._manager.coordinator.async_request_refresh()
        except Exception as e:
            _LOGGER.error("Failed to set boost power offset: %s", e)

class EnstoFloorLimitNumber(EnstoConfigEntity, NumberEntity):
    """Control floor temperature limits."""

    def __init__(self, manager, limit_type):
        super().__init__(manager)
        self._limit_type = limit_type
        self._attr_device_class = NumberDeviceClass.TEMPERATURE
        self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        self._attr_native_step = 0.5
        self._attr_mode = "box"
        self._attr_entity_registry_enabled_default = True
        
        if limit_type == "low":
            self._attr_name = "Floor temperature min"
            self._attr_unique_id = f"{dr.format_mac(self._manager.mac_address)}_floor_temp_min"
            self._attr_native_min_value = 5
            self._attr_native_max_value = 42
        else:
            self._attr_name = "Floor temperature max"
            self._attr_unique_id = f"{dr.format_mac(self._manager.mac_address)}_floor_temp_max"
            self._attr_native_min_value = 13
            self._attr_native_max_value = 50

    @property
    def available(self) -> bool:
        """Available only in combination heating mode."""
        if not super().available:
            return False
        if self._manager.coordinator and self._manager.coordinator.data:
            return self._manager.coordinator.data.get("heating_mode") == "Combination"
        return False

    @property
    def native_value(self) -> float | None:
        """Return floor limit from config cache."""
        limits = self._manager.config_data.get("floor_limits")
        if limits:
            return limits.get("low_value" if self._limit_type == "low" else "high_value")
        return None

    async def async_set_native_value(self, value: float) -> None:
        """Set new floor limit value."""
        try:
            limits = self._manager.config_data.get("floor_limits") or await self._manager.read_floor_limits()
            if limits:
                if self._limit_type == "low":
                    await self._manager.write_floor_limits(value, limits['high_value'])
                    limits['low_value'] = value
                else:
                    await self._manager.write_floor_limits(limits['low_value'], value)
                    limits['high_value'] = value
                self._manager.config_data["floor_limits"] = limits
                self.async_write_ha_state()
                if self._manager.coordinator:
                    await self._manager.coordinator.async_request_refresh()
        except Exception as e:
            _LOGGER.error("Failed to set floor temperature limit: %s", e)

class EnstoRoomSensorCalibrationNumber(EnstoConfigEntity, NumberEntity):
    """Number entity for room sensor calibration."""

    def __init__(self, manager):
        super().__init__(manager)
        self._attr_name = "Room sensor calibration"
        self._attr_unique_id = f"{dr.format_mac(self._manager.mac_address)}_room_sensor_calibration"
        self._attr_native_min_value = -5.0
        self._attr_native_max_value = 5.0
        self._attr_native_step = 0.1
        self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        self._attr_device_class = NumberDeviceClass.TEMPERATURE
        self._attr_mode = "box"

    @property
    def native_value(self) -> float | None:
        """Return room sensor calibration from config cache."""
        return self._manager.config_data.get("calibration")

    async def async_set_native_value(self, value: float) -> None:
        """Update room sensor calibration value."""
        try:
            success = await self._manager.write_room_sensor_calibration(value)
            if success:
                self._manager.config_data["calibration"] = value
                self.async_write_ha_state()
                if self._manager.coordinator:
                    await self._manager.coordinator.async_request_refresh()
        except Exception as e:
            _LOGGER.error("Failed to set room sensor calibration: %s", e)

class EnstoHeatingPowerNumber(EnstoConfigEntity, NumberEntity):
    """Number entity for controlling heating power."""

    def __init__(self, manager):
        super().__init__(manager)
        self._attr_name = "Heating power"
        self._attr_unique_id = f"{dr.format_mac(self._manager.mac_address)}_heating_power"
        self._attr_native_min_value = 0
        self._attr_native_max_value = 9999
        self._attr_native_step = 1
        self._attr_native_unit_of_measurement = "W"
        self._attr_mode = "box"
        self._attr_device_class = NumberDeviceClass.POWER

    @property
    def native_value(self) -> float | None:
        """Return heating power from config cache."""
        h_power = self._manager.config_data.get("heating_power")
        if h_power:
            return h_power.get("heating_power")
        return None

    async def async_set_native_value(self, value: float) -> None:
        """Update the heating power value."""
        try:
            success = await self._manager.write_heating_power(int(value))
            if success:
                self._manager.config_data["heating_power"] = {"heating_power": int(value)}
                self.async_write_ha_state()
                if self._manager.coordinator:
                    await self._manager.coordinator.async_request_refresh()
        except Exception as e:
            _LOGGER.error("Failed to set custom heating power: %s", e)

class EnstoFloorAreaNumber(EnstoConfigEntity, NumberEntity):
    """Number entity for controlling floor area."""

    def __init__(self, manager):
        super().__init__(manager)
        self._attr_name = "Floor area"
        self._attr_unique_id = f"{dr.format_mac(self._manager.mac_address)}_floor_area"
        self._attr_native_min_value = 0
        self._attr_native_max_value = 65535
        self._attr_native_step = 1
        self._attr_native_unit_of_measurement = "m²"
        self._attr_mode = "box"
        self._attr_device_class = NumberDeviceClass.AREA

    @property
    def native_value(self) -> float | None:
        """Return floor area from config cache."""
        fa = self._manager.config_data.get("floor_area")
        if fa:
            return fa.get("floor_area")
        return None

    async def async_set_native_value(self, value: float) -> None:
        """Update the floor area value."""
        try:
            success = await self._manager.write_floor_area(int(value))
            if success:
                self._manager.config_data["floor_area"] = {"floor_area": int(value)}
                self.async_write_ha_state()
                if self._manager.coordinator:
                    await self._manager.coordinator.async_request_refresh()
        except Exception as e:
            _LOGGER.error("Failed to set custom floor area: %s", e)

class EnstoEnergyUnitPriceNumber(EnstoConfigEntity, NumberEntity):
    """Number entity for controlling energy unit price."""

    def __init__(self, manager, currency: int):
        super().__init__(manager)
        self._attr_name = "Energy unit price"
        self._attr_unique_id = f"{dr.format_mac(self._manager.mac_address)}_energy_unit_price"
        self._attr_native_min_value = 0
        self._attr_native_max_value = 655.35
        self._attr_native_step = 0.01
        self._attr_mode = "box"
        self._currency = currency
        self._attr_entity_category = EntityCategory.CONFIG
        self._attr_native_unit_of_measurement = f"{CURRENCY_SYMBOLS.get(currency, '€')}/kWh"

    @property
    def native_value(self) -> float | None:
        """Return energy unit price from config cache."""
        eu = self._manager.config_data.get("energy_unit")
        if eu:
            return eu.get("price")
        return None

    async def async_set_native_value(self, value: float) -> None:
        """Update the energy unit price value."""
        try:
            success = await self._manager.write_energy_unit(self._currency, value)
            if success:
                self._manager.config_data["energy_unit"] = {"currency_code": self._currency, "price": value}
                self.async_write_ha_state()
                if self._manager.coordinator:
                    await self._manager.coordinator.async_request_refresh()
        except Exception as e:
            _LOGGER.error("Failed to set energy unit price: %s", e)

class EnstoVacationTempOffsetNumber(EnstoConfigEntity, NumberEntity):
    """Number entity for vacation mode temperature offset in celsius."""

    def __init__(self, manager):
        super().__init__(manager)
        self._attr_name = "Vacation temperature offset"
        self._attr_unique_id = f"{dr.format_mac(self._manager.mac_address)}_vacation_temp_offset"
        self._attr_native_min_value = -20.0
        self._attr_native_max_value = 20.0
        self._attr_native_step = 0.5
        self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        self._attr_device_class = NumberDeviceClass.TEMPERATURE
        self._attr_mode = "box"

    @property
    def available(self) -> bool:
        """Return if entity is available (all modes except Power mode)."""
        if not super().available:
            return False
        if self._manager.coordinator and self._manager.coordinator.data:
            return self._manager.coordinator.data.get("heating_mode") != "Power"
        return True

    @property
    def native_value(self) -> float | None:
        """Return vacation temperature offset from config cache."""
        vac = self._manager.config_data.get("vacation_time")
        if vac:
            return vac.get("offset_temperature")
        return None

    async def async_set_native_value(self, value: float) -> None:
        """Update the vacation temperature offset value."""
        try:
            settings = self._manager.config_data.get("vacation_time") or await self._manager.read_vacation_time()
            if settings:
                await self._manager.write_vacation_time(
                    time_from=settings.get("time_from"),
                    time_to=settings.get("time_to"),
                    offset_temperature=value,
                    offset_percentage=settings.get("offset_percentage", 0),
                    enabled=settings.get("enabled", False)
                )
                settings["offset_temperature"] = value
                self._manager.config_data["vacation_time"] = settings
                self.async_write_ha_state()
                if self._manager.coordinator:
                    await self._manager.coordinator.async_request_refresh()
        except Exception as e:
            _LOGGER.error("Failed to set vacation temperature offset: %s", e)

class EnstoVacationPowerOffsetNumber(EnstoConfigEntity, NumberEntity):
    """Number entity for vacation mode power offset percentage."""

    def __init__(self, manager):
        super().__init__(manager)
        self._attr_name = "Vacation power offset"
        self._attr_unique_id = f"{dr.format_mac(self._manager.mac_address)}_vacation_power_offset"
        self._attr_native_min_value = -100
        self._attr_native_max_value = 100
        self._attr_native_step = 1
        self._attr_native_unit_of_measurement = "%"
        self._attr_mode = "box"

    @property
    def available(self) -> bool:
        """Return if entity is available (only in Power mode)."""
        if not super().available:
            return False
        if self._manager.coordinator and self._manager.coordinator.data:
            return self._manager.coordinator.data.get("heating_mode") == "Power"
        return False

    @property
    def native_value(self) -> float | None:
        """Return vacation power offset from config cache."""
        vac = self._manager.config_data.get("vacation_time")
        if vac:
            return vac.get("offset_percentage")
        return None

    async def async_set_native_value(self, value: float) -> None:
        """Update the vacation power offset percentage."""
        try:
            settings = self._manager.config_data.get("vacation_time") or await self._manager.read_vacation_time()
            if settings:
                await self._manager.write_vacation_time(
                    time_from=settings.get("time_from"),
                    time_to=settings.get("time_to"),
                    offset_temperature=settings.get("offset_temperature", 0.0),
                    offset_percentage=int(value),
                    enabled=settings.get("enabled", False)
                )
                settings["offset_percentage"] = int(value)
                self._manager.config_data["vacation_time"] = settings
                self.async_write_ha_state()
                if self._manager.coordinator:
                    await self._manager.coordinator.async_request_refresh()
        except Exception as e:
            _LOGGER.error("Failed to set vacation power offset: %s", e)

class EnstoExternalControlTemperatureNumber(EnstoConfigEntity, NumberEntity):
    """Number entity for external control absolute temperature (mode 5)."""

    def __init__(self, manager):
        super().__init__(manager)
        self._attr_name = "External control temperature"
        self._attr_unique_id = f"{dr.format_mac(self._manager.mac_address)}_external_control_temperature"
        self._attr_native_min_value = 5.0
        self._attr_native_max_value = 35.0
        self._attr_native_step = 0.1
        self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        self._attr_device_class = NumberDeviceClass.TEMPERATURE
        self._attr_mode = "box"
        self._attr_icon = "mdi:thermometer"

    @property
    def available(self) -> bool:
        """Return if entity is available (external control mode 5)."""
        if not super().available:
            return False
        fc = self._manager.config_data.get("force_control")
        return fc is not None and fc.get("mode") == 5

    @property
    def native_value(self) -> float | None:
        """Return external control temperature from config cache."""
        fc = self._manager.config_data.get("force_control")
        if fc:
            return fc.get("temperature")
        return None

    async def async_set_native_value(self, value: float) -> None:
        """Update the external control temperature."""
        try:
            settings = self._manager.config_data.get("force_control") or await self._manager.read_force_control()
            if settings:
                await self._manager.write_force_control(
                    mode=settings.get("mode", 2),
                    temperature=value,
                    temperature_offset=settings.get("temperature_offset", 0.0)
                )
                settings["temperature"] = value
                self._manager.config_data["force_control"] = settings
                self.async_write_ha_state()
                if self._manager.coordinator:
                    await self._manager.coordinator.async_request_refresh()
        except Exception as e:
            _LOGGER.error("Failed to set external control temperature: %s", e)

class EnstoExternalControlOffsetNumber(EnstoConfigEntity, NumberEntity):
    """Number entity for external control temperature offset (mode 6)."""

    def __init__(self, manager):
        super().__init__(manager)
        self._attr_name = "External control offset"
        self._attr_unique_id = f"{dr.format_mac(self._manager.mac_address)}_external_control_offset"
        self._attr_native_min_value = -20.0
        self._attr_native_max_value = 20.0
        self._attr_native_step = 0.1
        self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        self._attr_device_class = NumberDeviceClass.TEMPERATURE
        self._attr_mode = "box"
        self._attr_icon = "mdi:thermometer-plus"

    @property
    def available(self) -> bool:
        """Return if entity is available (external control mode 6)."""
        if not super().available:
            return False
        fc = self._manager.config_data.get("force_control")
        return fc is not None and fc.get("mode") == 6

    @property
    def native_value(self) -> float | None:
        """Return external control offset from config cache."""
        fc = self._manager.config_data.get("force_control")
        if fc:
            return fc.get("temperature_offset")
        return None

    async def async_set_native_value(self, value: float) -> None:
        """Update the external control temperature offset."""
        try:
            settings = self._manager.config_data.get("force_control") or await self._manager.read_force_control()
            if settings:
                await self._manager.write_force_control(
                    mode=settings.get("mode", 2),
                    temperature=settings.get("temperature", 20.0),
                    temperature_offset=value
                )
                settings["temperature_offset"] = value
                self._manager.config_data["force_control"] = settings
                self.async_write_ha_state()
                if self._manager.coordinator:
                    await self._manager.coordinator.async_request_refresh()
        except Exception as e:
            _LOGGER.error("Failed to set external control offset: %s", e)
