"""Support for Ensto BLE sensors."""
import logging
from datetime import datetime, timedelta

from homeassistant.components.sensor import (
    SensorEntity,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.const import (
    UnitOfTemperature,
    UnitOfPower,
    UnitOfEnergy,
    STATE_UNKNOWN,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers import entity_registry

from .base_entity import EnstoBaseEntity, EnstoCoordinatorEntity, EnstoConfigEntity
from .const import DOMAIN
from . import EnstoConfigEntry

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1
SCAN_INTERVAL = timedelta(seconds=30)

UNIT_MINUTES = "min"
HISTORY_READ_DELAY = timedelta(minutes=5)

class EnstoBaseSensor(EnstoCoordinatorEntity, SensorEntity):
    """Base class for Ensto sensors backed by DataUpdateCoordinator."""

    def __init__(self, coordinator, manager, sensor_type):
        """Initialize the base sensor."""
        super().__init__(coordinator, manager)
        self._sensor_type = sensor_type
        self._attr_should_poll = False
        self._data_key = None

    @property
    def native_value(self):
        """Return native value directly from coordinator data."""
        if not self.coordinator.data or self._data_key is None:
            return None
        return self.coordinator.data.get(self._data_key)

async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnstoConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensors from a config entry."""
    manager = entry.runtime_data
    coordinator = manager.get_real_time_coordinator()
    parsed_data = coordinator.data
    sensors = []

    if parsed_data is not None:
        sensors.extend([
            EnstoTemperatureSensor(coordinator, manager, "target"),
            EnstoStateSensor(coordinator, manager, "relay"),
            EnstoStateSensor(coordinator, manager, "boost"),
            EnstoModeSensor(coordinator, manager, "active"),
            EnstoModeSensor(coordinator, manager, "heating"),
            EnstoNumberSensor(coordinator, manager, "boost_setpoint"),
            EnstoNumberSensor(coordinator, manager, "boost_remaining"),
            EnstoNumberSensor(coordinator, manager, "alarm"),
            EnstoDateTimeSensor(manager),
            EnstoPowerConsumptionSensor(manager),
            EnstoNameSensor(manager),
            EnstoCurrentPowerSensor(coordinator, manager),
            EnstoEnergySensor(manager),
        ])

        # Add floor sensor only if valid
        if parsed_data.get("floor_temperature") is not None and parsed_data.get("floor_temperature", 0) != 0:
            sensors.append(EnstoTemperatureSensor(coordinator, manager, "floor"))

        # Add room sensor only if valid (Phase 4.1: not created if absent / invalid)
        if parsed_data.get("room_temperature") is not None:
            sensors.append(EnstoTemperatureSensor(coordinator, manager, "room"))
        else:
            # Clean up orphaned room temperature entity from registry if device has no room sensor
            try:
                ent_reg = entity_registry.async_get(hass)
                room_unique_id = f"{dr.format_mac(manager.mac_address)}_room_temp"
                entity_id = ent_reg.async_get_entity_id("sensor", DOMAIN, room_unique_id)
                if entity_id:
                    _LOGGER.info("Removing obsolete room temperature entity %s (device has no room sensor)", entity_id)
                    ent_reg.async_remove(entity_id)
            except Exception as err:
                _LOGGER.debug("Could not check/clean entity registry for %s: %s", manager.mac_address, err)

    async_add_entities(sensors, False)

class EnstoTemperatureSensor(EnstoBaseSensor):
    """Temperature sensor for Ensto thermostat."""

    def __init__(self, coordinator, manager, sensor_type):
        """Initialize the temperature sensor."""
        super().__init__(coordinator, manager, sensor_type)
        self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        self._attr_device_class = SensorDeviceClass.TEMPERATURE
        self._attr_state_class = SensorStateClass.MEASUREMENT

        if sensor_type == "room":
            self._attr_name = "Room temperature"
            self._data_key = "room_temperature"
        elif sensor_type == "floor":
            self._attr_name = "Floor temperature"
            self._data_key = "floor_temperature"
        else:
            self._attr_name = "Target temperature"
            self._data_key = "target_temperature"

        self._attr_unique_id = f"{dr.format_mac(self._manager.mac_address)}_{sensor_type}_temp"

class EnstoStateSensor(EnstoBaseSensor):
    """State sensor for Ensto thermostat."""

    def __init__(self, coordinator, manager, sensor_type):
        """Initialize the sensor."""
        super().__init__(coordinator, manager, sensor_type)

        if sensor_type == "relay":
            self._attr_name = "Relay state"
            self._data_key = "relay_active"
        else:
            self._attr_name = "Boost state"
            self._data_key = "boost_enabled"

        self._attr_unique_id = f"{dr.format_mac(self._manager.mac_address)}_{sensor_type}_state"

class EnstoModeSensor(EnstoBaseSensor):
    """Mode sensor for Ensto thermostat."""

    def __init__(self, coordinator, manager, sensor_type):
        """Initialize the sensor."""
        super().__init__(coordinator, manager, sensor_type)

        if sensor_type == "active":
            self._attr_name = "Active mode"
            self._data_key = "active_mode"
        else:
            self._attr_name = "Heating mode"
            self._data_key = "heating_mode"

        self._attr_unique_id = f"{dr.format_mac(self._manager.mac_address)}_{sensor_type}_mode"

class EnstoNumberSensor(EnstoBaseSensor):
    """Number sensor for Ensto thermostat."""

    def __init__(self, coordinator, manager, sensor_type):
        """Initialize the sensor."""
        super().__init__(coordinator, manager, sensor_type)

        if "boost" in sensor_type:
            self._attr_native_unit_of_measurement = UNIT_MINUTES
            if sensor_type == "boost_setpoint":
                self._attr_name = "Boost setpoint"
                self._data_key = "boost_setpoint_minutes"
            else:
                self._attr_name = "Boost remaining"
                self._data_key = "boost_remaining_minutes"
        else:
            self._attr_name = "Alarm status"
            self._data_key = "alarm_code"
            self._active_alarms_key = "active_alarms"

        self._attr_unique_id = f"{dr.format_mac(self._manager.mac_address)}_{sensor_type}"

    @property
    def native_value(self):
        """Return formatted alarm or numeric value."""
        if not self.coordinator.data:
            return None
        if hasattr(self, '_active_alarms_key'):
            active_alarms = self.coordinator.data.get(self._active_alarms_key, [])
            alarm_code = self.coordinator.data.get(self._data_key, 0)
            if not active_alarms:
                return "No active errors"
            return f"Error {alarm_code}: {', '.join(active_alarms)}"

        return self.coordinator.data.get(self._data_key)

class EnstoDateTimeSensor(EnstoConfigEntity, SensorEntity):
    """DateTime sensor for Ensto thermostat."""

    def __init__(self, manager):
        """Initialize the sensor."""
        super().__init__(manager)
        self._attr_name = "Date and time"
        self._attr_unique_id = f"{dr.format_mac(self._manager.mac_address)}_datetime"
        self._attr_native_value = None

    async def async_added_to_hass(self) -> None:
        """Subscribe to datetime updates."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"ensto_datetime_update_{self._manager.mac_address}",
                self._handle_datetime_update,
            )
        )
        self._update_display_time()

    def _handle_datetime_update(self) -> None:
        """Update display when clock sync dispatches."""
        self._update_display_time()
        self.async_write_ha_state()

    def _update_display_time(self) -> None:
        """Format local time string for state."""
        self._attr_native_value = dt_util.as_local(dt_util.utcnow()).strftime("%-d.%-m.%Y %-H:%M")

class EnstoPowerConsumptionSensor(EnstoBaseEntity, SensorEntity):
    """Sensor for monitoring power consumption."""

    def __init__(self, manager):
        """Initialize the sensor."""
        super().__init__(manager)
        self._attr_name = "Power usage"
        self._attr_unique_id = f"{dr.format_mac(self._manager.mac_address)}_power_usage"
        self._attr_native_unit_of_measurement = "%"
        self._attr_device_class = SensorDeviceClass.POWER_FACTOR
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_native_value = None
        self._attr_extra_state_attributes = {
            "last_24h": [],
            "last_7_days": [],
            "last_12_months": [],
            "temperature_history": []
        }
        self._history_hour = None
        self._attr_should_poll = False

    async def async_added_to_hass(self) -> None:
        """Fetch initial history on load."""
        await super().async_added_to_hass()
        # Trigger background read for power history
        self.hass.async_create_task(self.async_update_history())

    async def async_update_history(self) -> None:
        """Fetch power consumption history when called."""
        try:
            result = await self._manager.read_power_consumption()
            if result and result.get('measurements'):
                current = result['measurements'][0]
                self._attr_native_value = current['ratio']
                self._attr_extra_state_attributes["last_24h"] = [
                    {
                        "time": m['timestamp'].isoformat(),
                        "ratio": m['ratio']
                    }
                    for m in result['measurements']
                ]

            history_hour = (dt_util.now() - HISTORY_READ_DELAY).replace(minute=0, second=0, microsecond=0)
            if history_hour != self._history_hour:
                monitoring_data = await self._manager.read_monitoring_data()
                if monitoring_data:
                    self._history_hour = history_hour
                    self._attr_extra_state_attributes["last_7_days"] = monitoring_data['daily_power']
                    self._attr_extra_state_attributes["last_12_months"] = monitoring_data['monthly_power']
                    self._attr_extra_state_attributes["temperature_history"] = monitoring_data['temperature_history']

            self.async_write_ha_state()
        except Exception as e:
            _LOGGER.error("Error updating power consumption sensor: %s", e)

class EnstoNameSensor(EnstoBaseEntity, SensorEntity):
    """Name sensor for Ensto thermostat."""

    def __init__(self, manager):
        """Initialize the sensor."""
        super().__init__(manager)
        self._attr_name = "Name"
        self._attr_unique_id = f"{dr.format_mac(self._manager.mac_address)}_name"
        self._attr_device_class = "name"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_native_value = self._manager.device_name
        self._attr_should_poll = False

class EnstoCurrentPowerSensor(EnstoCoordinatorEntity, SensorEntity):
    """Current power consumption sensor showing real-time heating power usage."""

    def __init__(self, coordinator, manager):
        """Initialize the current power sensor."""
        super().__init__(coordinator, manager)
        self._attr_name = "Current power"
        self._attr_unique_id = f"{dr.format_mac(self._manager.mac_address)}_current_power"
        self._attr_device_class = SensorDeviceClass.POWER
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_native_unit_of_measurement = UnitOfPower.WATT
        self._attr_should_poll = False

    @property
    def heating_power(self) -> int:
        """Return configured heating power from config cache."""
        hp = self._manager.config_data.get("heating_power")
        if hp:
            return hp.get("heating_power", 0)
        return 0

    @property
    def available(self) -> bool:
        """Return if sensor is available when heating power is configured and device is available."""
        return super().available and self.heating_power > 0

    @property
    def native_value(self):
        """Return calculated current power from heating power setting and relay state."""
        if not self.coordinator.data or self.heating_power <= 0:
            return None
        relay_active = self.coordinator.data.get("relay_active", False)
        return self.heating_power if relay_active else 0

class EnstoEnergySensor(EnstoBaseEntity, SensorEntity, RestoreEntity):
    """Energy consumption sensor (kWh) calculated from Current Power sensor."""

    def __init__(self, manager):
        """Initialize the energy sensor."""
        super().__init__(manager)
        self._attr_name = "Energy consumption"
        self._attr_unique_id = f"{dr.format_mac(self._manager.mac_address)}_energy_consumption"
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        if self._attr_native_value is None:
            self._attr_native_value = 0.0

        self._power_entity_id = None
        self._last_time = None

    async def async_added_to_hass(self) -> None:
        """Restore previous state and find linked power sensor."""
        await super().async_added_to_hass()

        old_state = await self.async_get_last_state()
        if old_state and old_state.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            try:
                self._attr_native_value = float(old_state.state)
            except ValueError:
                _LOGGER.warning(
                    "Could not restore energy value for %s: invalid state %s",
                    self._attr_name, 
                    old_state.state
                )

        registry = entity_registry.async_get(self.hass)
        target_unique_id = f"{dr.format_mac(self._manager.mac_address)}_current_power"

        for entry in registry.entities.values():
            if entry.unique_id == target_unique_id:
                self._power_entity_id = entry.entity_id
                _LOGGER.debug(
                    "Linked power sensor found via registry: %s",
                    self._power_entity_id
                )
                break

    async def async_update(self) -> None:
        """Update the current energy consumption value."""
        try:
            now = dt_util.utcnow()

            if not self._power_entity_id:
                return

            state = self.hass.states.get(self._power_entity_id)

            if state is not None and state.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE):
                try:
                    current_power = float(state.state)
                except ValueError:
                    current_power = 0.0

                if self._last_time is not None:
                    time_diff = (now - self._last_time).total_seconds() / 3600.0
                    energy_used = (current_power * time_diff) / 1000.0
                    self._attr_native_value += energy_used
            
            self._last_time = now

        except Exception as e:
            _LOGGER.error("Error updating energy sensor for [%s]: %s", self._manager.mac_address, e)
