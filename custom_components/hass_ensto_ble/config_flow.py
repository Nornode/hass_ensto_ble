"""Config flow for Ensto BLE integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN, CURRENCY_MAP
from .ensto_thermostat_manager import EnstoThermostatManager

_LOGGER = logging.getLogger(__name__)

# CONF_CURRENCY = "currency"
CONF_CURRENCY = "Please select a currency for energy cost calculations"
DEFAULT_CURRENCY = 1  # EUR

class EnstoConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Ensto BLE."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovered_devices = {}
        self._mac_address = None
        self._manager = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step.
        
        If user_input is None, scans for devices in pairing mode and shows device selection form.
        If user_input contains MAC address, attempts to connect and verify the device.
        """
        if user_input is not None:
            self._mac_address = user_input["Please select an Ensto BLE device in pairing mode"]

            # Set unique_id and abort if already configured
            await self.async_set_unique_id(self._mac_address)
            self._abort_if_unique_id_configured()

            # Move to adapter selection step
            return await self.async_step_adapter()

        # Initialize manager for device scanning
        manager = EnstoThermostatManager(self.hass, "")
        manager.setup()
        
        # Scan for devices that are in pairing mode (wait up to 60 seconds)
        import asyncio
        pairing_devices = {}
        for attempt in range(12):
            pairing_devices = manager.find_devices_in_pairing_mode()
            if pairing_devices:
                break
            if attempt < 11:
                await asyncio.sleep(5)

        if not pairing_devices:
            return self.async_abort(
                reason="No Ensto BLE devices in pairing mode found after 60 seconds. Hold BLE reset button for >0.5 seconds. Blue LED will blink."
            )

        self._discovered_devices = {}
        for addr, (device, _) in pairing_devices.items():
            # Get RSSI value for the device
            rssi = device.rssi if hasattr(device, 'rssi') else None
            
            # Include RSSI in the device name if available
            if rssi is not None:
                self._discovered_devices[addr] = f"{device.name} ({addr}) [{rssi} dBm]"
            else:
                self._discovered_devices[addr] = f"{device.name} ({addr})"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("Please select an Ensto BLE device in pairing mode"): vol.In(self._discovered_devices)
                }
            )
        )

    async def async_step_adapter(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle adapter selection."""
        if user_input is not None:
            self._adapter_source = user_input["Please select a Bluetooth Adapter"]
            return await self.async_step_currency()
            
        from homeassistant.components.bluetooth import async_scanner_devices_by_address
        devices_iter = async_scanner_devices_by_address(self.hass, self._mac_address, connectable=True)
        devices_list = list(devices_iter) if devices_iter else []
        
        adapters = {"auto": "Auto-Select (Best Signal)"}
        for d in devices_list:
            details = getattr(d.ble_device, 'details', None)
            if isinstance(details, dict):
                src = details.get('source', details.get('path', str(details)))
            else:
                src = getattr(details, 'source', str(type(details)))
                
            scanner_name = getattr(d.scanner, 'name', src) if hasattr(d, 'scanner') else src
            rssi = getattr(d.advertisement, 'rssi', 'unknown') if hasattr(d, 'advertisement') else 'unknown'
            adapters[src] = f"{scanner_name} ({rssi} dBm)"
            
        return self.async_show_form(
            step_id="adapter",
            data_schema=vol.Schema({
                vol.Required("Please select a Bluetooth Adapter", default="auto"): vol.In(adapters)
            })
        )

    async def async_step_currency(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle currency selection."""
        if user_input is not None:
            # Initialize manager and get device info
            self._manager = EnstoThermostatManager(self.hass, self._mac_address, getattr(self, '_adapter_source', 'auto'))
            self._manager.setup()
            
            # Try to connect and authenticate the device
            try: 
                await self._manager.ensure_connection()
            except Exception as e:
                _LOGGER.error("Action connect for [%s]: %s", self._mac_address, e)
                return self.async_abort(
                    reason=f"Connection failed: {str(e)}"
                )
            
            # Create config entry with device info and currency
            model = self._manager.model_number or "Unknown Model"
            name = self._manager.device_name or self._mac_address
            title = f"{model} {name}"
            
            return self.async_create_entry(
                title=title,
                data={
                    "mac_address": self._mac_address,
                    "adapter_source": getattr(self, '_adapter_source', 'auto'),
                    CONF_CURRENCY: user_input[CONF_CURRENCY],
                }
            )

        # Show currency selection form
        currency_options = {code: name for code, name in CURRENCY_MAP.items()}

        return self.async_show_form(
            step_id="currency",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_CURRENCY, default=DEFAULT_CURRENCY): vol.In(currency_options)
                }
            )
        )
