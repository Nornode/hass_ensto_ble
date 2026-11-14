"""Home Assistant integration tests using pytest-homeassistant-custom-component."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from bleak.exc import BleakError
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hass_ensto_ble.const import DOMAIN
from custom_components.hass_ensto_ble.data_coordinator import EnstoDataUpdateCoordinator
from custom_components.hass_ensto_ble.ensto_thermostat_manager import (
    EnstoThermostatManager,
)
from tests.conftest import SAMPLE_REAL_TIME_DATA, TEST_MAC


def _patch_manager_for_setup(
    mock_ble_client: MagicMock,
    real_time_data: dict | None = None,
):
    """Patch EnstoThermostatManager BLE I/O for clean HA setup tests."""
    data = dict(SAMPLE_REAL_TIME_DATA if real_time_data is None else real_time_data)

    async def fake_ensure_connection(self, force: bool = False):
        self.client = mock_ble_client
        self._authenticated = True
        self.model_number = "ECO16BT"
        self.device_name = "Test Thermostat"
        self.connected_at = None

    return (
        patch("homeassistant.components.bluetooth.async_get_scanner", return_value=MagicMock()),
        patch.object(EnstoThermostatManager, "ensure_connection", new=fake_ensure_connection),
        patch.object(EnstoThermostatManager, "read_software_revision", new=AsyncMock(return_value="1.0.0")),
        patch.object(EnstoThermostatManager, "read_hardware_revision", new=AsyncMock(return_value="1.0")),
        patch.object(EnstoThermostatManager, "_ble_read_split", new=AsyncMock(return_value=b"dummy")),
        patch.object(EnstoThermostatManager, "parse_real_time_indication", return_value=data),
        patch.object(EnstoThermostatManager, "async_refresh_config_cache", new=AsyncMock()),
        patch.object(EnstoThermostatManager, "read_date_and_time", new=AsyncMock(return_value=None)),
        patch.object(EnstoThermostatManager, "read_energy_unit", new=AsyncMock(return_value={"currency_code": 1, "price": 0.15})),
        patch.object(EnstoThermostatManager, "cleanup", new=AsyncMock()),
    )


async def test_setup_and_unload_entry(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_ble_client: MagicMock,
) -> None:
    """Test full integration setup and unload through Home Assistant."""
    mock_config_entry.add_to_hass(hass)

    patches = _patch_manager_for_setup(mock_ble_client)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9]:
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        assert mock_config_entry.state is ConfigEntryState.LOADED
        assert hass.services.has_service(DOMAIN, "get_calendar_day")
        assert hass.services.has_service(DOMAIN, "set_calendar_day")

        # Verify connectivity binary sensor is registered and 'on'
        entity_reg = er.async_get(hass)
        conn_entity_id = entity_reg.async_get_entity_id(
            "binary_sensor", DOMAIN, "aa:bb:cc:dd:ee:ff_connectivity"
        )
        assert conn_entity_id is not None
        conn_state = hass.states.get(conn_entity_id)
        assert conn_state is not None
        assert conn_state.state == STATE_ON

        # Unload cleanly
        assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED
    assert not hass.services.has_service(DOMAIN, "get_calendar_day")
    assert not hass.services.has_service(DOMAIN, "set_calendar_day")


async def test_setup_connection_failure_triggers_retry(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that initial BLE connection failure puts config entry into SETUP_RETRY."""
    mock_config_entry.add_to_hass(hass)

    with (
        patch("homeassistant.components.bluetooth.async_get_scanner", return_value=MagicMock()),
        patch.object(EnstoThermostatManager, "ensure_connection", new=AsyncMock(side_effect=BleakError("Link down"))),
        patch.object(EnstoThermostatManager, "cleanup", new=AsyncMock()),
    ):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_coordinator_failure_marks_entities_unavailable_except_connectivity(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_ble_client: MagicMock,
) -> None:
    """Test that coordinator read failure marks sensors unavailable while connectivity reports off."""
    mock_config_entry.add_to_hass(hass)

    patches = _patch_manager_for_setup(mock_ble_client)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9]:
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        manager: EnstoThermostatManager = mock_config_entry.runtime_data
        coordinator: EnstoDataUpdateCoordinator = manager.get_real_time_coordinator()

        entity_reg = er.async_get(hass)
        conn_entity_id = entity_reg.async_get_entity_id(
            "binary_sensor", DOMAIN, "aa:bb:cc:dd:ee:ff_connectivity"
        )
        target_entity_id = entity_reg.async_get_entity_id(
            "sensor", DOMAIN, "aa:bb:cc:dd:ee:ff_target_temp"
        )
        assert conn_entity_id is not None
        assert target_entity_id is not None
        assert hass.states.get(target_entity_id).state == "21.5"

        # Simulate BLE read failure on next coordinator refresh
        with patch.object(EnstoThermostatManager, "_ble_read_split", new=AsyncMock(return_value=None)):
            await coordinator.async_refresh()
            await hass.async_block_till_done()

        assert hass.states.get(target_entity_id).state == STATE_UNAVAILABLE
        conn_state = hass.states.get(conn_entity_id)
        assert conn_state is not None
        assert conn_state.state == STATE_OFF


async def test_floor_only_omits_room_temp_and_cleans_stale_registry_entry(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_ble_client: MagicMock,
) -> None:
    """Test floor-only thermostat omits room temp sensor and removes stale registry entry."""
    mock_config_entry.add_to_hass(hass)

    entity_reg = er.async_get(hass)
    stale_entry = entity_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        "aa:bb:cc:dd:ee:ff_room_temp",
        config_entry=mock_config_entry,
    )
    assert entity_reg.async_get(stale_entry.entity_id) is not None

    floor_only_data = dict(SAMPLE_REAL_TIME_DATA)
    floor_only_data["room_temperature"] = None

    patches = _patch_manager_for_setup(mock_ble_client, real_time_data=floor_only_data)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9]:
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert entity_reg.async_get(stale_entry.entity_id) is None
    assert (
        entity_reg.async_get_entity_id("sensor", DOMAIN, "aa:bb:cc:dd:ee:ff_room_temp")
        is None
    )
