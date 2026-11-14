"""Unit tests for Phase 1 Safe Hardening:
- Platform declarations (PARALLEL_UPDATES, SCAN_INTERVAL, no dead _attr_scan_interval)
- Disconnection handling & unpair guard
"""
import unittest
from unittest.mock import MagicMock

# Set up Home Assistant & bleak mocks
import tests.mocks

from custom_components.hass_ensto_ble import sensor, number, switch, select, datetime as dt_mod
from custom_components.hass_ensto_ble.ensto_thermostat_manager import EnstoThermostatManager

class TestPhase1Hardening(unittest.IsolatedAsyncioTestCase):
    """Test platform module configurations and manager callbacks."""

    def test_platform_parallel_updates_and_scan_interval(self):
        """Verify all platform modules declare PARALLEL_UPDATES=1 and SCAN_INTERVAL."""
        modules = [sensor, number, switch, select, dt_mod]
        for mod in modules:
            self.assertTrue(hasattr(mod, "PARALLEL_UPDATES"), f"{mod.__name__} missing PARALLEL_UPDATES")
            self.assertEqual(getattr(mod, "PARALLEL_UPDATES"), 1, f"{mod.__name__} PARALLEL_UPDATES != 1")
            self.assertTrue(hasattr(mod, "SCAN_INTERVAL"), f"{mod.__name__} missing SCAN_INTERVAL")

    def test_no_dead_attr_scan_interval_on_entities(self):
        """Verify dead _attr_scan_interval is not declared on any platform entity class."""
        modules = [sensor, number, switch, select, dt_mod]
        for mod in modules:
            for attr_name in dir(mod):
                attr = getattr(mod, attr_name)
                if isinstance(attr, type):
                    self.assertFalse(
                        "_attr_scan_interval" in attr.__dict__,
                        f"{attr.__name__} in {mod.__name__} still declares _attr_scan_interval"
                    )

    async def test_disconnected_callback_clears_state(self):
        """Verify _on_disconnected clears _authenticated and drops client."""
        hass = MagicMock()
        manager = EnstoThermostatManager(hass, "AA:BB:CC:DD:EE:FF")
        mock_client = MagicMock()
        manager.client = mock_client
        manager._authenticated = True

        manager._on_disconnected(mock_client)

        self.assertFalse(manager._authenticated)
        self.assertIsNone(manager.client)
        self.assertIs(manager._stale_client, mock_client)

if __name__ == "__main__":
    unittest.main()
