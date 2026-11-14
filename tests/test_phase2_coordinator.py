"""Unit tests for Phase 2 Coordinator-based data model:
- DataUpdateCoordinator operation and UpdateFailed handling
- CoordinatorEntity zero-poll behavior and live state mapping
- Mode-dependent availability without GATT reads
- Config cache tier and refresh triggers
"""
import unittest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch

# Shared mocks
import tests.mocks
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.hass_ensto_ble.data_coordinator import EnstoDataUpdateCoordinator
from custom_components.hass_ensto_ble.ensto_thermostat_manager import EnstoThermostatManager
from custom_components.hass_ensto_ble.sensor import (
    EnstoTemperatureSensor,
    EnstoStateSensor,
    EnstoModeSensor,
    EnstoCurrentPowerSensor,
)
from custom_components.hass_ensto_ble.switch import EnstoBoostSwitch
from custom_components.hass_ensto_ble.select import EnstoHeatingModeSelect
from custom_components.hass_ensto_ble.number import (
    EnstoFloorLimitNumber,
    EnstoBoostPowerOffsetNumber,
    EnstoBoostDurationNumber,
)

class TestPhase2Coordinator(unittest.IsolatedAsyncioTestCase):
    """Test Phase 2 coordinator and entity behaviors."""

    async def asyncSetUp(self):
        self.hass = MagicMock()
        self.manager = EnstoThermostatManager(self.hass, "AA:BB:CC:DD:EE:FF")
        self.mock_client = MagicMock()
        self.mock_client.is_connected = True
        self.manager.client = self.mock_client
        self.manager._authenticated = True
        self.coordinator = EnstoDataUpdateCoordinator(self.hass, self.manager)
        self.manager.coordinator = self.coordinator

    async def test_coordinator_fetch_success(self):
        """Test coordinator fetches real-time packet and updates data."""
        sample_real_time_data = {
            "target_temperature": 21.5,
            "room_temperature": 20.0,
            "floor_temperature": 22.0,
            "relay_active": True,
            "active_mode": "Manual",
            "heating_mode": "Floor",
            "boost_enabled": False,
            "boost_setpoint_minutes": 60,
            "boost_remaining_minutes": 0,
            "alarm_code": 0,
            "active_alarms": [],
        }
        self.manager.ensure_connection = AsyncMock()
        self.manager._ble_read_split = AsyncMock(return_value=b"dummy_bytes")
        self.manager.parse_real_time_indication = MagicMock(return_value=sample_real_time_data)
        self.manager.async_refresh_config_cache = AsyncMock()
        self.manager.read_date_and_time = AsyncMock(return_value=None)

        await self.coordinator.async_refresh()

        self.assertTrue(self.coordinator.last_update_success)
        self.assertEqual(self.coordinator.data["target_temperature"], 21.5)
        self.assertEqual(self.coordinator.data["floor_temperature"], 22.0)
        self.assertTrue(self.coordinator.data["relay_active"])
        self.assertEqual(self.coordinator.data["heating_mode"], "Floor")

    async def test_coordinator_raises_update_failed_on_read_failure(self):
        """Test coordinator raises UpdateFailed when BLE read fails."""
        self.manager.ensure_connection = AsyncMock()
        self.manager._ble_read_split = AsyncMock(return_value=None)

        with self.assertRaises(UpdateFailed):
            await self.coordinator._async_update_data()

    async def test_coordinator_entities_read_live_data_without_polling(self):
        """Test coordinator entities do not poll and read directly from coordinator.data."""
        self.coordinator.data = {
            "target_temperature": 22.5,
            "floor_temperature": 21.0,
            "relay_active": True,
            "boost_enabled": True,
            "heating_mode": "Floor",
            "active_mode": "Manual",
        }
        self.manager.config_data["heating_power"] = {"heating_power": 1500}

        target_sensor = EnstoTemperatureSensor(self.coordinator, self.manager, "target")
        floor_sensor = EnstoTemperatureSensor(self.coordinator, self.manager, "floor")
        power_sensor = EnstoCurrentPowerSensor(self.coordinator, self.manager)
        boost_switch = EnstoBoostSwitch(self.coordinator, self.manager)
        mode_select = EnstoHeatingModeSelect(self.coordinator, self.manager)

        # All entities must have should_poll = False
        self.assertFalse(target_sensor.should_poll)
        self.assertFalse(floor_sensor.should_poll)
        self.assertFalse(power_sensor.should_poll)
        self.assertFalse(boost_switch.should_poll)
        self.assertFalse(mode_select.should_poll)

        # Values read from coordinator data
        self.assertEqual(target_sensor.native_value, 22.5)
        self.assertEqual(floor_sensor.native_value, 21.0)
        self.assertEqual(power_sensor.native_value, 1500)
        self.assertTrue(boost_switch.is_on)
        self.assertEqual(mode_select.current_option, "Floor")

        # When relay is off, current power is 0 without reading GATT
        self.coordinator.data["relay_active"] = False
        self.assertEqual(power_sensor.native_value, 0)

    async def test_mode_dependent_entities_use_coordinator_mode(self):
        """Test mode-dependent entities compute availability from coordinator data without extra GATT reads."""
        floor_limit_low = EnstoFloorLimitNumber(self.manager, "low")
        boost_power_offset = EnstoBoostPowerOffsetNumber(self.manager)

        # In Floor mode: floor limits unavailable (Combination only), boost power unavailable (Power only)
        self.coordinator.data = {"heating_mode": "Floor"}
        self.assertFalse(floor_limit_low.available)
        self.assertFalse(boost_power_offset.available)

        # In Combination mode: floor limits available
        self.coordinator.data = {"heating_mode": "Combination"}
        self.assertTrue(floor_limit_low.available)
        self.assertFalse(boost_power_offset.available)

        # In Power mode: boost power offset available
        self.coordinator.data = {"heating_mode": "Power"}
        self.assertFalse(floor_limit_low.available)
        self.assertTrue(boost_power_offset.available)

    async def test_config_entity_reads_cache_and_requests_refresh_on_write(self):
        """Test config numbers read cached settings and request coordinator refresh after write."""
        self.manager.config_data["boost"] = {
            "enabled": False,
            "offset_degrees": 2.0,
            "offset_percentage": 0,
            "setpoint_minutes": 60,
        }
        self.manager.write_boost = AsyncMock(return_value=True)
        self.coordinator.async_request_refresh = AsyncMock()

        duration_num = EnstoBoostDurationNumber(self.manager)
        duration_num.async_write_ha_state = MagicMock()
        self.assertFalse(duration_num.should_poll)
        self.assertEqual(duration_num.native_value, 60)

        await duration_num.async_set_native_value(90)

        # Verified cache updated, write sent, and coordinator refresh requested
        self.assertEqual(self.manager.config_data["boost"]["setpoint_minutes"], 90)
        self.manager.write_boost.assert_awaited_once_with(
            enabled=False,
            offset_degrees=2.0,
            offset_percentage=0,
            duration_minutes=90
        )
        self.coordinator.async_request_refresh.assert_awaited_once()

if __name__ == "__main__":
    unittest.main()
