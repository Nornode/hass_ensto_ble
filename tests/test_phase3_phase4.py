"""Tests for Phase 3 (Connection lifecycle & backoff) and Phase 4 (Semantics & binary sensor)."""
import unittest
from unittest.mock import MagicMock, AsyncMock, patch
import time

from tests.mocks import (
    Entity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from custom_components.hass_ensto_ble.ensto_thermostat_manager import EnstoThermostatManager
from custom_components.hass_ensto_ble.binary_sensor import EnstoConnectivityBinarySensor
from custom_components.hass_ensto_ble.sensor import async_setup_entry as sensor_async_setup_entry
from custom_components.hass_ensto_ble.data_coordinator import EnstoDataUpdateCoordinator
from bleak.exc import BleakError


class TestPhase3ConnectionAndPhase4Semantics(unittest.IsolatedAsyncioTestCase):
    """Test suite for Phase 3 reconnect backoff and Phase 4 entities."""

    async def asyncSetUp(self):
        self.hass = MagicMock()
        self.mac = "AA:BB:CC:DD:EE:FF"
        self.manager = EnstoThermostatManager(self.hass, self.mac)
        self.coordinator = EnstoDataUpdateCoordinator(self.hass, self.manager)
        self.manager.coordinator = self.coordinator

    async def test_reconnect_backoff_and_jitter(self):
        """Test exponential backoff prevents hammering connection on repeated failure."""
        self.assertEqual(self.manager._failed_connect_count, 0)
        self.assertEqual(self.manager._reconnect_backoff_until, 0.0)

        mock_device = MagicMock()
        # Mock connect failure
        with patch("homeassistant.components.bluetooth.async_ble_device_from_address", return_value=mock_device), \
             patch("custom_components.hass_ensto_ble.ensto_thermostat_manager.establish_connection", AsyncMock(side_effect=Exception("Link down"))):
            with self.assertRaises(Exception):
                await self.manager.ensure_connection()

            self.assertEqual(self.manager._failed_connect_count, 1)
            self.assertGreater(self.manager._reconnect_backoff_until, time.monotonic())

            # Immediate second call should raise BleakError due to backoff without invoking connect()
            with patch.object(self.manager, "connect") as mock_connect:
                with self.assertRaises(BleakError) as ctx:
                    await self.manager.ensure_connection()

                self.assertIn("Connection backoff active", str(ctx.exception))
                mock_connect.assert_not_called()

            # Calling with force=True should bypass the backoff
            with patch.object(self.manager, "connect", side_effect=Exception("Force fail")) as mock_connect_force:
                with self.assertRaises(Exception):
                    await self.manager.ensure_connection(force=True)
                mock_connect_force.assert_called_once()

    async def test_reconnect_backoff_resets_on_success(self):
        """Test that backoff counter and timer reset to zero on successful connect."""
        self.manager._failed_connect_count = 5
        self.manager._reconnect_backoff_until = time.monotonic() + 100

        mock_client = MagicMock()
        mock_client.is_connected = True
        mock_client.pair = AsyncMock()
        mock_device = MagicMock()

        with patch("homeassistant.components.bluetooth.async_ble_device_from_address", return_value=mock_device), \
             patch("homeassistant.components.bluetooth.async_scanner_devices_by_address", return_value=[]), \
             patch("custom_components.hass_ensto_ble.ensto_thermostat_manager.establish_connection", AsyncMock(return_value=mock_client)), \
             patch.object(self.manager, "read_factory_reset_id", AsyncMock(return_value=12345)), \
             patch.object(self.manager, "write_factory_reset_id", AsyncMock()), \
             patch.object(self.manager, "read_model_number", AsyncMock(return_value="ECO16BT")), \
             patch.object(self.manager, "read_device_name", AsyncMock(return_value="Thermostat")):

            await self.manager.connect()

            self.assertEqual(self.manager._failed_connect_count, 0)
            self.assertEqual(self.manager._reconnect_backoff_until, 0.0)
            self.assertTrue(self.manager._authenticated)

    async def test_connectivity_binary_sensor(self):
        """Test diagnostic connectivity sensor behavior."""
        sensor = EnstoConnectivityBinarySensor(self.coordinator, self.manager)
        self.assertEqual(sensor.unique_id, "aa:bb:cc:dd:ee:ff_connectivity")
        self.assertTrue(sensor.available)  # Stays available when reporting link down

        # When client is None or not connected
        self.assertFalse(sensor.is_on)

        # When client is connected and authenticated
        mock_client = MagicMock()
        mock_client.is_connected = True
        self.manager.client = mock_client
        self.manager._authenticated = True
        self.coordinator.last_update_success = True

        self.assertTrue(sensor.is_on)

        # If disconnected
        mock_client.is_connected = False
        self.assertFalse(sensor.is_on)

    async def test_room_temperature_absence_and_registry_cleanup(self):
        """Test floor-only device omits room temp sensor and cleans up registry."""
        mock_entry = MagicMock()
        mock_entry.runtime_data = self.manager

        # Simulated coordinator data for a floor-only thermostat (room_temperature is None)
        self.coordinator.data = {
            "floor_temperature": 21.5,
            "room_temperature": None,
            "target_temperature": 22.0,
            "relay_active": False,
            "boost_enabled": False,
            "active_mode": "manual",
            "heating_mode": "floor",
            "boost_setpoint_minutes": 30,
            "boost_remaining_minutes": 0,
            "alarm_code": 0,
        }

        mock_ent_reg = MagicMock()
        mock_ent_reg.async_get_entity_id.return_value = "sensor.office_thermostat_room_temperature"

        added_entities = []
        def mock_add_entities(entities, update):
            added_entities.extend(entities)

        with patch("homeassistant.helpers.entity_registry.async_get", return_value=mock_ent_reg):
            await sensor_async_setup_entry(self.hass, mock_entry, mock_add_entities)

        # Check that room temperature sensor is NOT in added entities
        types = [getattr(e, "_sensor_type", None) for e in added_entities]
        self.assertNotIn("room", types)
        self.assertIn("floor", types)

        # Check that old entity registry entry was removed
        mock_ent_reg.async_get_entity_id.assert_called_with("sensor", "hass_ensto_ble", "aa:bb:cc:dd:ee:ff_room_temp")
        mock_ent_reg.async_remove.assert_called_with("sensor.office_thermostat_room_temperature")


if __name__ == "__main__":
    unittest.main()
