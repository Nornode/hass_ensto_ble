"""Fixtures for Hass Ensto BLE tests using pytest-homeassistant-custom-component."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hass_ensto_ble.const import DOMAIN

TEST_MAC = "AA:BB:CC:DD:EE:FF"

SAMPLE_REAL_TIME_DATA = {
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


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable loading custom integrations in all tests."""
    return


@pytest.fixture(autouse=True)
def mock_bluetooth_deps(hass: HomeAssistant) -> None:
    """Mark bluetooth dependencies as loaded for integration setup."""
    hass.config.components.add("bluetooth")
    hass.config.components.add("bluetooth_adapters")


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Return a mock config entry for Hass Ensto BLE."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="ECO16BT Test Thermostat",
        data={
            "mac_address": TEST_MAC,
            "adapter_source": "auto",
        },
        unique_id=TEST_MAC,
    )


@pytest.fixture
def mock_ble_client() -> MagicMock:
    """Return a mocked BleakClient."""
    client = MagicMock()
    client.is_connected = True
    client.read_gatt_char = AsyncMock(return_value=b"\x40\x00")
    client.write_gatt_char = AsyncMock(return_value=None)
    client.disconnect = AsyncMock(return_value=None)
    return client
