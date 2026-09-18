"""Support for Ensto BLE devices."""
import logging
import asyncio
import time
from typing import Optional
from bleak import BleakClient
from bleak.exc import BleakError
from bleak_retry_connector import establish_connection, BleakClientWithServiceCache
from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant
from .storage_manager import EnstoStorageManager
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
from homeassistant.util import dt as dt_util
from .data_coordinator import EnstoRealTimeCoordinator

from .const import (
    MANUFACTURER_ID,
    ERROR_CODES_BYTE0,
    ERROR_CODES_BYTE1,
    ACTIVE_MODES,
    EXTERNAL_CONTROL_MODES,
    MODE_MAP,
    CURRENCY_MAP,
    CURRENCY_SYMBOLS,
    DEVICE_NAME_UUID,
    MODEL_NUMBER_UUID,
    SOFTWARE_REVISION_UUID,
    HARDWARE_REVISION_UUID,
    DATE_AND_TIME_UUID,
    DAYLIGHT_SAVING_UUID,
    HEATING_MODE_UUID,
    BOOST_UUID,
    FLOOR_LIMITS_UUID,
    ADAPTIVE_TEMPERATURE_CONTROL_UUID,
    HEATING_POWER_UUID,
    FLOOR_AREA_UUID,
    CALIBRATION_VALUE_FOR_ROOM_TEMPERATURE_UUID,
    ENERGY_UNIT_UUID,
    CALENDAR_CONTROL_UUID,
    CALENDAR_DAY_UUID,
    VACATION_TIME_UUID,
    CALENDAR_MODE_UUID,
    FACTORY_RESET_ID_UUID,
    MONITORING_DATA_UUID,
    REAL_TIME_INDICATION_POWER_CONSUMPTION_UUID,
    FORCE_CONTROL_UUID,
)

_LOGGER = logging.getLogger(__name__)

class EnstoThermostatManager:
    """Manager for Ensto BLE thermostats."""

    def __init__(self, hass: HomeAssistant, mac_address: str, adapter_source: str = "auto") -> None:
        self.adapter_source = adapter_source
        """Initialize the manager."""
        self.hass = hass
        self.mac_address = mac_address
        self.client: Optional[BleakClient] = None
        self._connect_lock = asyncio.Lock()
        self._available = True
        self._unavailable_until = 0
        self._consecutive_failures = 0
        self.scanner = None
        self.storage_manager = EnstoStorageManager(hass)
        self.model_number = None
        self.device_name = None
        self.real_time_coordinator = None

    def get_real_time_coordinator(self):
        if not self.real_time_coordinator:
            self.real_time_coordinator = EnstoRealTimeCoordinator(self)
        return self.real_time_coordinator

    def supports_external_control(self) -> bool:
        """Check if firmware supports external control (1.14+)."""
        if not hasattr(self, 'sw_version') or not self.sw_version:
            return False
        try:
            # Parse version like "1.14.0;..." -> 1.14
            version_str = self.sw_version.split(';')[0]
            parts = version_str.split('.')
            major = int(parts[0])
            minor = int(parts[1]) if len(parts) > 1 else 0
            return (major, minor) >= (1, 14)
        except (ValueError, IndexError):
            _LOGGER.debug("Could not parse firmware version: %s", self.sw_version)
            return False

    def setup(self) -> None:
        """Set up the scanner when needed."""
        self.scanner = bluetooth.async_get_scanner(self.hass)

    async def initialize(self) -> None:
        """Initialize the connection."""
        await self.connect()

    async def disconnect(self) -> None:
        """Safely disconnect and clear client."""
        if self.client:
            try:
                if self.client.is_connected:
                    await self.client.disconnect()
            except Exception as e:
                _LOGGER.debug("Error during disconnect: %s", e)
            finally:
                self.client = None

    async def cleanup(self) -> None:
        """Clean up the connection."""
        await self.disconnect()

    async def connect(self) -> None:
        """Establish connection to the device."""
        if await self._connect_lock.acquire():
            try:
                if self.client and self.client.is_connected:
                    return

                _LOGGER.debug("Finding device %s", self.mac_address)
                
                # Get all available adapters that can see this device
                from homeassistant.components.bluetooth import async_scanner_devices_by_address
                devices_iter = async_scanner_devices_by_address(self.hass, self.mac_address, connectable=True)
                devices_list = list(devices_iter) if devices_iter else []
                
                device = None
                if devices_list:
                    _LOGGER.info("Found %d adapters for %s:", len(devices_list), self.mac_address)
                    for d in devices_list:
                        rssi = getattr(d.advertisement, 'rssi', 'unknown') if hasattr(d, 'advertisement') else 'unknown'
                        details = getattr(d.ble_device, 'details', None)
                        if isinstance(details, dict):
                            src = details.get('source', details.get('path', str(details)))
                        else:
                            src = getattr(details, 'source', str(type(details)))
                        _LOGGER.info(" - Available Adapter: %s (RSSI: %s)", src, rssi)
                        
                    # Sort devices by RSSI (highest/best signal first)
                    devices_list.sort(key=lambda d: getattr(d.advertisement, 'rssi', -100) if hasattr(d, 'advertisement') else -100, reverse=True)
                    
                    if getattr(self, 'adapter_source', 'auto') != 'auto':
                        import time
                        fallback_active = getattr(self, '_fallback_active_until', 0) > time.time()
                        
                        if fallback_active:
                            _LOGGER.warning("Preferred adapter %s is in fallback mode. Auto-selecting best RSSI adapter.", self.adapter_source)
                        else:
                            for d in devices_list:
                                details = getattr(d.ble_device, 'details', None)
                                if isinstance(details, dict):
                                    src = details.get('source', details.get('path', str(details)))
                                else:
                                    src = getattr(details, 'source', str(type(details)))
                                if src == self.adapter_source:
                                    device = d.ble_device
                                    _LOGGER.info("Forced Bluetooth adapter %s for %s based on user config", src, self.mac_address)
                                    break
                    
                    if not device:
                        device = devices_list[0].ble_device
                        best_rssi = getattr(devices_list[0].advertisement, 'rssi', 'unknown') if hasattr(devices_list[0], 'advertisement') else 'unknown'
                        _LOGGER.info("Selected best available adapter for %s based on RSSI (%s dBm)", self.mac_address, best_rssi)

                if not device:
                    device = bluetooth.async_ble_device_from_address(self.hass, self.mac_address)
                    if device:
                        adapter_source = getattr(device.details, 'source', str(type(device.details))) if hasattr(device, 'details') else "Unknown"
                        _LOGGER.info("Fallback selected Bluetooth adapter for %s is: %s", self.mac_address, adapter_source)
                
                if not device:
                    raise Exception(f"Device {self.mac_address} not found")

                # Use bleak-retry-connector
                _LOGGER.debug("Device [%s]: establishing connection", self.mac_address)
                self.client = await establish_connection(BleakClientWithServiceCache, device, self.mac_address)
                _LOGGER.debug("Device [%s]: connection established", self.mac_address)

                # always pair to set encryption
                _LOGGER.debug("Pairing with device %s", self.mac_address)
                try:
                    await self.client.pair()
                    _LOGGER.debug("Paired device %s", self.mac_address)
                except Exception as e:
                    if "Authentication" in str(e) or "AuthenticationFailed" in str(e):
                        _LOGGER.warning("Authentication failed (%s). Attempting to unpair and retry...", e)
                        try:
                            await self.client.unpair()
                            import asyncio
                            await asyncio.sleep(2)
                            await self.client.pair()
                            _LOGGER.debug("Successfully paired after unpairing: %s", self.mac_address)
                        except Exception as unpair_e:
                            _LOGGER.error("Failed to unpair/repair: %s", unpair_e)
                            raise e
                    else:
                        raise e

                # Check storage first
                stored_id = await self.read_device_info()
                
                if stored_id is None:
                    # No stored ID, need to get factory_reset_id from device
                    _LOGGER.info("No stored Factory Reset ID found, attempting pairing...")
                        
                    # Get Factory Reset ID from device
                    try:
                        device_id = await self.read_factory_reset_id()
                        if device_id:
                            _LOGGER.info("Got Factory Reset ID from device")
                            await self.write_device_info(self.mac_address, device_id)
                            stored_id = device_id
                        else:
                            raise Exception("Could not get Factory Reset ID from device")
                    except Exception as e:
                        _LOGGER.error("Error reading Factory Reset ID from device: %s", e)
                        raise
                
                # Write Factory Reset ID to device
                await self.write_factory_reset_id(stored_id)
                
                # Read and store model number and device name after successful connection
                self.model_number = await self.read_model_number()
                self.device_name = await self.read_device_name()
                
                _LOGGER.info("Successfully verified Factory Reset ID and read model number")
                
                # Connection fully succeeded, reset availability
                self._available = True
                self._consecutive_failures = 0
                self._unavailable_until = 0

            except Exception as e:
                _LOGGER.error("Failed to connect: %s", str(e))
                await self.disconnect()
                raise
            finally:
                self._connect_lock.release()
        else:
            raise Exception("Already connecting")

    async def ensure_connection(self) -> None:
        """Ensure that we have a connection to the device."""
        if not self._available and time.time() < self._unavailable_until:
            _LOGGER.debug("Device %s is in cooldown, skipping connection", self.mac_address)
            raise Exception("Device unavailable (cooldown)")

        if not self.client or not self.client.is_connected:
            await self.connect()

    async def _ble_read(self, uuid: str, retries: int = 2) -> Optional[bytes]:
        """Helper to handle connection checks, retries, and error logging for BLE reads."""
        for attempt in range(retries + 1):
            try:
                await self.ensure_connection()
                data = await self.client.read_gatt_char(uuid)
                _LOGGER.debug("BLE read from %s: %s", uuid, data.hex() if data else "None")
                
                # Reset failures on success
                if not self._available:
                    self._available = True
                self._consecutive_failures = 0
                
                return data
            except Exception as e:
                log_level = logging.DEBUG if attempt < retries else logging.WARNING
                _LOGGER.log(log_level, "BLE read failed for %s (attempt %d/%d): %s", uuid, attempt + 1, retries + 1, e)
                await self.disconnect()
                if attempt < retries:
                    await asyncio.sleep(1 * (attempt + 1))
                else:
                    self._consecutive_failures += 1
                    if getattr(self, 'adapter_source', 'auto') != 'auto' and getattr(self, '_fallback_active_until', 0) < time.time() and self._consecutive_failures == 2:
                        self._fallback_active_until = time.time() + 600
                        self._consecutive_failures = 0
                        _LOGGER.warning("Preferred adapter %s failed. Falling back to best available adapter for 10 minutes.", self.adapter_source)
                    elif self._consecutive_failures >= 3:
                        self._available = False
                        self._unavailable_until = time.time() + 120
                        _LOGGER.warning("Device %s marked unavailable for 120s due to consecutive failures.", self.mac_address)
        return None

    async def _ble_write(self, uuid: str, data: bytes, retries: int = 2, response: bool = True) -> bool:
        """Helper to handle connection checks, retries, and error logging for BLE writes."""
        for attempt in range(retries + 1):
            try:
                await self.ensure_connection()
                _LOGGER.debug("BLE write to %s: %s", uuid, data.hex() if data else "None")
                await self.client.write_gatt_char(uuid, data, response=response)
                
                # Reset failures on success
                if not self._available:
                    self._available = True
                self._consecutive_failures = 0
                
                return True
            except Exception as e:
                log_level = logging.DEBUG if attempt < retries else logging.WARNING
                _LOGGER.log(log_level, "BLE write failed for %s (attempt %d/%d): %s", uuid, attempt + 1, retries + 1, e)
                await self.disconnect()
                if attempt < retries:
                    await asyncio.sleep(1 * (attempt + 1))
                else:
                    self._consecutive_failures += 1
                    if getattr(self, 'adapter_source', 'auto') != 'auto' and getattr(self, '_fallback_active_until', 0) < time.time() and self._consecutive_failures == 2:
                        self._fallback_active_until = time.time() + 600
                        self._consecutive_failures = 0
                        _LOGGER.warning("Preferred adapter %s failed. Falling back to best available adapter for 10 minutes.", self.adapter_source)
                    elif self._consecutive_failures >= 3:
                        self._available = False
                        self._unavailable_until = time.time() + 120
                        _LOGGER.warning("Device %s marked unavailable for 120s due to consecutive failures.", self.mac_address)
        return False

    async def write_device_info(self, device_address: str, factory_reset_id: int) -> None:
        """Write device info to Home Assistant storage."""
        await self.storage_manager.async_save_device_data(device_address, factory_reset_id)

    async def read_device_info(self) -> Optional[int]:
        """Read device info from Home Assistant storage."""
        device_data = await self.storage_manager.async_load_device_data(self.mac_address)
        if device_data:
            return device_data.get("factory_reset_id")
        return None

    def find_ensto_devices(self):
        """Scan and find Ensto devices with manufacturer 0x2806 (big endian)."""
        discovered_devices = {}
        
        for discovery_info in bluetooth.async_discovered_service_info(self.hass):
            advertisement_data = discovery_info.advertisement
            if (advertisement_data and
                advertisement_data.manufacturer_data and
                MANUFACTURER_ID in advertisement_data.manufacturer_data):
                discovered_devices[discovery_info.address] = (discovery_info, advertisement_data)
        
        return discovered_devices

    def find_devices_in_pairing_mode(self):
        """Find BLE devices that are in pairing mode (PAIRINGFLAG=1)."""
        ensto_devices = self.find_ensto_devices()
        
        pairing_devices = {}
        for addr, (discovery_info, adv) in ensto_devices.items():
            try:
                service_info = bluetooth.async_last_service_info(self.hass, addr)
                fields = service_info.manufacturer_data[MANUFACTURER_ID].decode('ascii').split(';')
                if len(fields) >= 2 and fields[1] == "1":
                    _LOGGER.info("Device %s address %s is in pairing mode", discovery_info.name, discovery_info.address)
                    pairing_devices[addr] = (discovery_info, adv)
                else:
                    _LOGGER.debug("Device %s address %s is NOT in pairing mode", discovery_info.name, discovery_info.address)

            except Exception as e:
                _LOGGER.error("Error parsing manufacturer data: %s", e)
                
        return pairing_devices

    async def read_factory_reset_id(self) -> Optional[int]:
        """Read Factory Reset ID from the BLE device."""
        import asyncio
        for attempt in range(4):
            try:
                data = await self.client.read_gatt_char(FACTORY_RESET_ID_UUID)
                _LOGGER.debug("BLE read from %s: %s", FACTORY_RESET_ID_UUID, data.hex() if data else "None")
                return int.from_bytes(data[:4], byteorder="little")
            except Exception as e:
                if attempt < 3:
                    _LOGGER.warning("GATT read for factory reset failed (attempt %d/4). Waiting for ESPHome GATT resolution... Error: %s", attempt + 1, e)
                    await asyncio.sleep(2.5)
                else:
                    raise Exception(f"Failed to read factory reset ID after 4 attempts: {e}")

    async def write_factory_reset_id(self, factory_reset_id: int) -> None:
        """Write the Factory Reset ID to the BLE device."""
        try:
            id_bytes = factory_reset_id.to_bytes(4, byteorder="little")
            _LOGGER.debug("BLE write to %s: %s", FACTORY_RESET_ID_UUID, id_bytes.hex())
            await self.client.write_gatt_char(FACTORY_RESET_ID_UUID, id_bytes, response=True)
        except Exception as e:
            raise Exception(f"Failed to write factory reset ID: {e}")

    async def read_split_characteristic(self, characteristic_uuid: str) -> bytes:
        """
        Read BLE characteristic data that uses split format.
        
        Args:
            characteristic_uuid: UUID of the characteristic to read
            
        Returns:
            bytes: Combined data from all split packets
            
        Raises:
            BleakError: If there's an error reading the characteristic
        """
        await self.ensure_connection()
        
        combined_data = bytearray()
        more_data = True
        
        try:
            while more_data:
                # Read next packet
                packet = await self.client.read_gatt_char(characteristic_uuid)
                
                if not packet or len(packet) < 1:
                    break
                    
                header = packet[0]
                data = packet[1:]
                
                # Add data portion to combined data
                combined_data.extend(data)
                
                # Check if this was the last packet (0x40 bit set in header)
                if header & 0x40:
                    more_data = False
                    
            # Remove padding bytes (zeros from the end)
            return bytes(combined_data).rstrip(b'\x00')
            
        except BleakError as e:
            _LOGGER.warning("Error reading characteristic: %s", e)
            # Connection might be dead, clear it
            await self.disconnect()
            raise

    def parse_real_time_indication(self, data: bytes) -> dict:
        """
        Parse real time indication data packet.
        
        Data format:
        BYTE[0-1]: Target temperature (uint16_t, 50-500 scaled to 5.0-50.0)
        BYTE[2]: Temperature setting % (uint8_t, 0-100)
        BYTE[3-4]: Room temperature (int16_t, -50 to 350 scaled to -5.0 to 35.0)
        BYTE[5-6]: Floor temperature (int16_t, -50 to 500 scaled to -5.0 to 50.0)
        BYTE[7]: Active relay state (0=off, 1=on)
        BYTE[8-11]: Alarm code
        BYTE[12]: Active mode (1=manual, 2=calendar, 3=vacation)
        BYTE[13]: Active heating mode
        BYTE[14]: Boost mode (0=disabled, 1=enabled)
        BYTE[15-16]: Boost setpoint minutes
        BYTE[17-18]: Boost remaining minutes
        BYTE[19]: Potentiometer absolute % value (0-100)
        """
        if not data or len(data) < 20:
            _LOGGER.error("Invalid data length: %s", len(data) if data else 0)
            return {}

        try:
            # Target temperature (uint16, scaled)
            target_temp = int.from_bytes(data[0:2], byteorder='little') / 10
            
            # Temperature setting percentage
            temp_setting_percent = data[2]
            
            # Room temperature (int16, scaled)
            room_temp = int.from_bytes(data[3:5], byteorder='little', signed=True) / 10
            
            # Floor temperature (int16, scaled)
            floor_temp = int.from_bytes(data[5:7], byteorder='little', signed=True) / 10
            
            # Active relay state
            relay_active = bool(data[7])
            
            # Parse alarm codes (first 2 bytes only, as others are reserved)
            alarm_bytes = data[8:10]
            active_alarms = []
            
            # Parse each byte and its corresponding error codes
            error_code_maps = [ERROR_CODES_BYTE0, ERROR_CODES_BYTE1]
            for byte_idx, error_map in enumerate(error_code_maps):
                byte_value = alarm_bytes[byte_idx]
                for bit_mask, error_msg in error_map.items():
                    if byte_value & bit_mask:
                        active_alarms.append(error_msg)
            
            # Store both raw code and parsed alarms
            alarm_code = int.from_bytes(alarm_bytes, byteorder='little')

            # Active modes
            active_mode = ACTIVE_MODES.get(data[12], "Unknown")

            # Active heating mode
            heating_mode = MODE_MAP.get(data[13], "Unknown")

            # Boost settings
            boost_enabled = bool(data[14])
            boost_setpoint = int.from_bytes(data[15:17], byteorder='little')
            boost_remaining = int.from_bytes(data[17:19], byteorder='little')
            
            # Potentiometer value
            potentiometer_value = data[19]
            
            return {
                "target_temperature": target_temp,
                "temperature_setting_percent": temp_setting_percent,
                "room_temperature": room_temp,
                "floor_temperature": floor_temp,
                "relay_active": relay_active,
                "alarm_code": alarm_code,
                "active_alarms": active_alarms,
                "active_mode": active_mode,
                "heating_mode": heating_mode,
                "boost_enabled": boost_enabled,
                "boost_setpoint_minutes": boost_setpoint,
                "boost_remaining_minutes": boost_remaining,
                "potentiometer_value": potentiometer_value
            }
        except Exception as e:
            _LOGGER.error("Error parsing real time indication: %s", e)
            return {}

    async def read_model_number(self) -> Optional[str]:
        """Read model number via GATT characteristic."""
        try:
            data = await self.client.read_gatt_char(MODEL_NUMBER_UUID)
            _LOGGER.debug("BLE read from %s: %s", MODEL_NUMBER_UUID, data.hex() if data else "None")
            return data.decode('utf-8')
        except Exception as e:
            _LOGGER.warning("Failed to parse model number: %s", e)
            return None

    async def set_heating_mode(self, mode: int) -> None:
        """Set heating mode."""
        try:
            await self.write_heating_mode(mode)
        except Exception as e:
            _LOGGER.error("Failed to set heating mode: %s", e)

    async def set_adaptive_temp_control(self, enabled: bool) -> None:
        """Set adaptive temperature control."""
        try:
            await self.write_adaptive_temp_control(enabled)
        except Exception as e:
            _LOGGER.error("Failed to set adaptive temperature control: %s", e)

    async def read_boost(self) -> dict:
        """Read boost configuration from device."""
        data = await self._ble_read(BOOST_UUID)
        if not data:
            return None

        try:

            # Parse data
            enabled = bool(data[0])  # First byte is enable flag
            
            # Parse temperature offset (bytes 1-2 as signed int16)
            # Convert from raw value (2150 = 21.5 degrees)
            offset_raw = int.from_bytes(data[1:3], byteorder='little', signed=True)
            offset_degrees = offset_raw / 100.0
            
            # Parse percentage offset (byte 3)
            # Convert percentage offset byte to signed int (range -128 to 127)
            offset_percentage = int.from_bytes([data[3]], byteorder='little', signed=True)
            
            # Parse time setpoint (bytes 4-5 as unsigned int16)
            setpoint_minutes = int.from_bytes(data[4:6], byteorder='little')
            
            # Parse remaining time (bytes 6-7 as unsigned int16)
            remaining_minutes = int.from_bytes(data[6:8], byteorder='little')

            return {
                'enabled': enabled,
                'offset_degrees': offset_degrees,
                'offset_percentage': offset_percentage,
                'setpoint_minutes': setpoint_minutes,
                'remaining_minutes': remaining_minutes
            }

        except Exception as e:
            _LOGGER.warning("Failed to parse boost config: %s", e)
            return None

    async def write_boost(self, enabled: bool, offset_degrees: float, offset_percentage: int, duration_minutes: int) -> bool:
        """Write boost configuration to device."""
        try:
            # Validate input values
            if not (-20 <= offset_degrees <= 20):
                raise ValueError("Temperature offset must be between -20 and 20 degrees")
            if not (-100 <= offset_percentage <= 100):
                raise ValueError("Percentage offset must be between -100 and 100")
            if not (0 <= duration_minutes <= 65535):  # max value for uint16
                raise ValueError("Duration must be between 0 and 65535 minutes")

            # Convert temperature to raw value (multiply by 100)
            # e.g., 21.5 degrees becomes 2150
            offset_raw = int(offset_degrees * 100)

            # Create data packet (8 bytes)
            data = bytearray(8)
            data[0] = 1 if enabled else 0  # Enable/disable flag
            
            # Temperature offset as signed int16
            data[1:3] = offset_raw.to_bytes(2, byteorder='little', signed=True)
            
            # Percentage offset
            # Convert percentage offset to signed int8
            data[3] = offset_percentage.to_bytes(1, byteorder='little', signed=True)[0]
            
            # Duration setpoint as unsigned int16
            data[4:6] = duration_minutes.to_bytes(2, byteorder='little')
            
            # Remaining time bytes are left as 0
            data[6:8] = (0).to_bytes(2, byteorder='little')
            
            # Write to device
        except Exception as e:
            _LOGGER.warning("Failed to prepare boost config data: %s", e)
            return False

        return await self._ble_write(BOOST_UUID, data)


    async def read_heating_mode(self) -> dict:
        """Read heating mode configuration from device."""
        data = await self._ble_read(HEATING_MODE_UUID)
        if not data:
            return None

        try:

            # Get mode number from first byte
            mode_number = data[0]
            mode_name = MODE_MAP.get(mode_number, "Unknown")

            return {
                'mode_number': mode_number,
                'mode_name': mode_name
            }

        except Exception as e:
            _LOGGER.warning("Failed to parse heating mode: %s", e)
            return None

    async def write_heating_mode(self, mode: int) -> bool:
        """Write heating mode configuration to device."""
        try:
    # Validate input mode
            if mode not in MODE_MAP:
                raise ValueError(
                    "Invalid mode. Must be: "
                    "1 (Floor), 2 (Room), 3 (Combination), "
                    "4 (Power), or 5 (Force Control)"
                )

            # Pack data - just a single byte
            data = bytes([mode])
            
            # Write to device
        except Exception as e:
            _LOGGER.warning("Failed to prepare heating mode data: %s", e)
            return False

        return await self._ble_write(HEATING_MODE_UUID, data)


    async def read_adaptive_temp_control(self) -> dict:
        """Read adaptive temperature control setting from device."""
        data = await self._ble_read(ADAPTIVE_TEMPERATURE_CONTROL_UUID)
        if not data:
            return None

        try:

            # Parse first byte as boolean
            enabled = bool(data[0])

            return {
                'enabled': enabled
            }

        except Exception as e:
            _LOGGER.warning("Failed to parse adaptive temperature control: %s", e)
            return None

    async def write_adaptive_temp_control(self, enabled: bool) -> bool:
        """Write adaptive temperature control setting to device."""
        try:
    # Create single byte data
            data = bytes([1 if enabled else 0])
            
            # Write to device
        except Exception as e:
            _LOGGER.warning("Failed to prepare adaptive temperature control data: %s", e)
            return False

        return await self._ble_write(ADAPTIVE_TEMPERATURE_CONTROL_UUID, data)


    async def read_device_name(self) -> Optional[str]:
        """
        Read device name via GATT characteristic.
        
        Returns:
            str - Device name if successful, None if failed or device is unnamed
        """
        try:
            data = await self.client.read_gatt_char(DEVICE_NAME_UUID)
            _LOGGER.debug("BLE read from %s: %s", DEVICE_NAME_UUID, data.hex() if data else "None")
            if not data:
                return None
            
            # Skip first byte and strip null bytes
            name_bytes = data[1:].split(b'\x00')[0]
            
            # If no actual name data, return None
            if not name_bytes:
                return None
                
            # Decode using UTF-8 to handle Nordic characters
            device_name = name_bytes.decode('utf-8')
            return device_name
        except Exception as e:
            _LOGGER.warning("Failed to parse device name: %s", e)
            return None

    async def write_split_characteristic(self, characteristic_uuid: str, data: bytes) -> bool:
        """
        Write BLE characteristic data that needs to be split into multiple packets.
        """
        await self.ensure_connection()
        
        try:
            # Get MTU size and calculate max chunk size
            mtu_size = self.client.mtu_size
            _LOGGER.debug("Current MTU size: %s", mtu_size)
            mtu_size - 3  # Reserve 3 bytes for ATT header
            
            # Calculate chunk size to ensure at least 2 packets
            chunk_size = len(data) // 2 + (len(data) % 2)  # Force split into two chunks
            
            # Calculate how many chunks we need
            total_chunks = 2  # Force minimum of 2 chunks
            
            for current_chunk in range(total_chunks):
                # Calculate start and end positions for this chunk's data
                start_pos = current_chunk * chunk_size
                end_pos = min(start_pos + chunk_size, len(data))
                chunk_data = data[start_pos:end_pos]
                
                # Create header byte:
                if current_chunk == 0:
                    # First packet: header is just sequence number (0)
                    header = 0x80
                else:
                    # Last packet: 0x40 bit set but sequence number stays 0
                    header = 0x40
                
                # Combine header and data
                packet = bytearray([header]) + chunk_data
                
                # Debug log for each packet
                _LOGGER.debug(
                    f"Writing chunk {current_chunk + 1}/{total_chunks}:"
                    f"\nHeader: 0x{header:02x}"
                    f"\nChunk data (bytes): {chunk_data.hex()}"
                    f"\nFull packet (bytes): {packet.hex()}"
                )
                
                # Write the packet
                await self.client.write_gatt_char(characteristic_uuid, packet, response=True)
                
                # Small delay between packets
                await asyncio.sleep(0.1)
            
            return True
            
        except BleakError as e:
            _LOGGER.warning("Error writing characteristic: %s", e)
            # Connection might be dead, clear it
            await self.disconnect()
            raise

    async def read_date_and_time(self) -> dict:
        """Read date and time from device in UTC.

        Returns:
            dict: UTC time components with keys:
                year (int): Full year (e.g. 2024)
                month (int): Month (1-12)
                day (int): Day of month (1-31) 
                hour (int): Hour (0-23)
                minute (int): Minute (0-59)
                second (int): Second (0-59)
            None: If read fails
        """
        data = await self._ble_read(DATE_AND_TIME_UUID)
        if not data:
            return None

        try:

            # Ensure input is the correct length
            if len(data) != 7:
                _LOGGER.error("Device timestamp must be 7 bytes long.")
                return None
            
            # Extract individual bytes
            year = int.from_bytes(data[0:2], byteorder="little")
            month = data[2]
            date = data[3]
            hour = data[4]
            minute = data[5]
            second = data[6]

            return {
                "year": year,
                "month": month,
                "day": date,
                "hour": hour,
                "minute": minute,
                "second": second
            }

        except Exception as e:
            _LOGGER.warning("Failed to parse date and time from device: %s", e)
            return None

    async def write_date_and_time(self, year: int, month: int, day: int, hour: int, minute: int, second: int) -> bool:
        """Write date and time to device.
        
        All time values must be in UTC as the device internally operates in UTC time 
        and handles DST/timezone conversions based on its settings.
        
        Args:
            year: UTC year (0-9999)
            month: UTC month (1-12)
            day: UTC day (1-31)
            hour: UTC hour (0-23)
            minute: UTC minute (0-59)
            second: UTC second (0-59)
            
        Returns:
            bool: True if successful, False if failed
            
        Note:
            Device specification 2.2.1: MCU operates in UTC, data collection is 
            maintained with UTC timestamps. All timestamps must be in UTC regardless 
            of the device's timezone settings.
        """
        try:
    # Validate input values
            if not (0 <= year <= 9999):
                _LOGGER.error("Invalid UTC year: %s", year)
                return False
            if not (1 <= month <= 12):
                _LOGGER.error("Invalid UTC month: %s", month)
                return False
            if not (1 <= day <= 31):
                _LOGGER.error("Invalid UTC day: %s", day)
                return False
            if not (0 <= hour <= 23):
                _LOGGER.error("Invalid UTC hour: %s", hour)
                return False
            if not (0 <= minute <= 59):
                _LOGGER.error("Invalid UTC minute: %s", minute)
                return False
            if not (0 <= second <= 59):
                _LOGGER.error("Invalid UTC second: %s", second)
                return False

            _LOGGER.debug(
                "Writing UTC time to %s for %s: %04d-%02d-%02d %02d:%02d:%02d",
                self.device_name or "Unknown Device",
                self.mac_address,
                year, month, day, hour, minute, second
            )

            # Construct byte array according to device spec 2.2.2:
            # BYTE[0-1]: year as uint16_t
            # BYTE[2]: month 1-12
            # BYTE[3]: date 1-31
            # BYTE[4]: hour 0-23
            # BYTE[5]: minute 0-59
            # BYTE[6]: second 0-59
            year_bytes = year.to_bytes(2, byteorder="little")
            data = bytearray([
                year_bytes[0],    # First byte of year
                year_bytes[1],    # Second byte of year
                month,            # Month
                day,              # Day
                hour,             # Hour
                minute,           # Minute
                second            # Second
            ])

            # Write to device
        except Exception as e:
            _LOGGER.warning("Failed to prepare UTC time to device data: %s", e)
            return False

        return await self._ble_write(DATE_AND_TIME_UUID, data)


    async def read_daylight_saving(self) -> dict:
        """Read daylight saving configuration from device.
        
        Returns:
            dict: Configuration with keys:
                enabled (bool): DST enabled/disabled
                winter_to_summer_offset (int): Offset in minutes for winter->summer transition
                summer_to_winter_offset (int): Offset in minutes for summer->winter transition
                timezone_offset (int): Base timezone offset in minutes from UTC
            None: If read fails
        """
        data = await self._ble_read(DAYLIGHT_SAVING_UUID)
        if not data:
            return None

        try:

            # Parse data
            enabled = bool(data[0])
            # bytes 2-3: winter->summer offset (signed int16)
            winter_to_summer = int.from_bytes(data[2:4], byteorder='little', signed=True)
            # bytes 4-5: summer->winter offset (signed int16)
            summer_to_winter = int.from_bytes(data[4:6], byteorder='little', signed=True)
            # bytes 6-7: timezone offset in minutes (signed int16)
            timezone_offset = int.from_bytes(data[6:8], byteorder='little', signed=True)

            return {
                'enabled': enabled,
                'winter_to_summer_offset': winter_to_summer,
                'summer_to_winter_offset': summer_to_winter,
                'timezone_offset': timezone_offset
            }

        except Exception as e:
            _LOGGER.warning("Failed to parse daylight saving config: %s", e)
            return None

    async def write_daylight_saving(
        self,
        enabled: bool,
        winter_to_summer: int,
        summer_to_winter: int,
        timezone_offset: int
    ) -> bool:
        """Write daylight saving configuration to device.
        
        According to protocol specification (chapter 2.2.3), device needs proper timezone
        and DST settings to handle local time display correctly.
        
        Args:
            enabled: Enable/disable daylight saving
            winter_to_summer: Offset in minutes for winter to summer transition (default 60 = 1h)
            summer_to_winter: Offset in minutes for summer to winter transition (default 60 = 1h)
            timezone_offset: Base timezone offset in minutes (default 120 = UTC+2 for Finland)
            
        Note: 
            For Finland (EET/EEST):
            - timezone_offset should be 120 (UTC+2)
            - DST changes are 1h (60 minutes)
            - Device adds the DST offset automatically when enabled
        """
        try:
            data = bytearray(8)
            data[0] = 1 if enabled else 0  # Enable/disable flag
            data[1] = 0  # Reserved byte
            
            # Winter->summer offset (1h = 60 minutes)
            data[2:4] = winter_to_summer.to_bytes(2, byteorder='little', signed=True)
            
            # Summer->winter offset (1h = 60 minutes)
            data[4:6] = summer_to_winter.to_bytes(2, byteorder='little', signed=True)
            
            # Timezone offset (UTC+2 = 120 minutes for Finland)
            data[6:8] = timezone_offset.to_bytes(2, byteorder='little', signed=True)
        except Exception as e:
            _LOGGER.warning("Failed to prepare daylight saving config data: %s", e)
            return False

        return await self._ble_write(DAYLIGHT_SAVING_UUID, data)

    async def read_floor_limits(self) -> Optional[dict]:
        """Read floor temperature limits from device.
             
        Returns:
            dict with keys:
                low_value: Min floor temp in °C (range 5-42)
                high_value: Max floor temp in °C (range 13-50)
            None if read fails
        """
        data = await self._ble_read(FLOOR_LIMITS_UUID)
        if not data:
            return None

        try:
            return {
                'low_value': int.from_bytes(data[0:2], byteorder='little') / 100,
                'high_value': int.from_bytes(data[2:4], byteorder='little') / 100
            }

        except Exception as e:
            _LOGGER.warning("Failed to parse floor limits: %s", e)
            return None

    async def write_floor_limits(self, low_value: float, high_value: float) -> bool:
        """Write floor temperature limits to device.

        Args:
            low_value: Min floor temp in °C (5-42)
            high_value: Max floor temp in °C (13-50)

        Returns:
            True if write successful, False otherwise

        Note:
            - Min must be at least 8°C lower than max
            - Used only in combination mode (mode 3)
            - Min absolute value: 5°C
            - Max absolute value: 50°C
        """
        # Input validation
        if not (5 <= low_value <= 42):
            _LOGGER.error("Min floor temp must be between 5-42°C")
            return False
        if not (13 <= high_value <= 50):
            _LOGGER.error("Max floor temp must be between 13-50°C")
            return False
        if high_value - low_value < 8:
            _LOGGER.error("Min must be at least 8°C lower than max")
            return False

        data = bytearray(4)
        low_raw = int(low_value * 100)
        high_raw = int(high_value * 100)
        data[0:2] = low_raw.to_bytes(2, byteorder='little')
        data[2:4] = high_raw.to_bytes(2, byteorder='little')

        result = await self._ble_write(FLOOR_LIMITS_UUID, bytes(data), response=False)
        if result:
            _LOGGER.debug(
                "Floor limits written successfully: min=%.1f °C, max=%.1f °C",
                low_value,
                high_value
            )
        return result

    async def read_room_sensor_calibration(self) -> dict:
        """Read room sensor calibration value."""
        data = await self._ble_read(CALIBRATION_VALUE_FOR_ROOM_TEMPERATURE_UUID)
        if not data:
            return None

        try:
            raw_value = int.from_bytes(data[0:2], byteorder='little', signed=True)
            calibration_value = round(raw_value / 10, 1)
            
            return {
                'calibration_value': calibration_value
            }

        except Exception as e:
            _LOGGER.warning("Failed to parse room sensor calibration: %s", e)
            return None

    async def write_room_sensor_calibration(self, value: float) -> bool:
        """Write room sensor calibration value."""
        if not (-5.0 <= value <= 5.0):
            _LOGGER.error("Calibration value must be between -5.0 and +5.0 °C")
            return False

        raw_value = int(value * 10)
        data = raw_value.to_bytes(2, byteorder='little', signed=True)
        return await self._ble_write(CALIBRATION_VALUE_FOR_ROOM_TEMPERATURE_UUID, data)

    async def read_software_revision(self) -> Optional[str]:
        """Read software revision string."""
        data = await self._ble_read(SOFTWARE_REVISION_UUID)
        if not data:
            return None

        try:
            # Parse format: app;ble;bootloader
            return data.decode('utf-8')

        except Exception as e:
            _LOGGER.warning("Failed to parse software revision: %s", e)
            return None

    async def read_hardware_revision(self) -> Optional[str]:
        """Read hardware revision."""
        data = await self._ble_read(HARDWARE_REVISION_UUID)
        if not data:
            return None

        try:
            hw_version = int.from_bytes(data[0:4], byteorder='little')
            return str(hw_version)

        except Exception as e:
            _LOGGER.warning("Failed to parse hardware revision: %s", e)
            return None

    async def read_heating_power(self) -> dict:
        """Read custom heating power configuration from device.
        
        Returns:
            dict with keys:
                heating_power (int): Heating power in Watts (range 0-9999)
        """

        data = await self._ble_read(HEATING_POWER_UUID)
        if not data:
            return None

        try:
            
            heating_power = int.from_bytes(data[0:2], byteorder='little')

            return {
                'heating_power': heating_power
            }

        except Exception as e:
            _LOGGER.warning("Failed to parse custom heating power value: %s", e)
            return None

    async def write_heating_power(self, value: int) -> bool:
        """Write custom heating power configuration to device.
        
        Args:
            value (int): Heating power in Watts (range 0-9999)
            
        Returns:
            bool: True if successful, False otherwise
        """

        if not (0 <= value <= 9999):
            _LOGGER.error("Heating power value must be between 0 and 9999")
            return False

        try:
            data = value.to_bytes(2, byteorder='little')
        except Exception as e:
            _LOGGER.warning("Failed to prepare custom heating power value data: %s", e)
            return False

        return await self._ble_write(HEATING_POWER_UUID, data)


    async def read_floor_area(self) -> dict:
        """Read custom floor area configuration from device.
        
        Returns:
            dict with keys:
                floor_area (int): Floor area in square meters (m²)
        """
            
        data = await self._ble_read(FLOOR_AREA_UUID)
        if not data:
            return None

        try:
            
            floor_area = int.from_bytes(data[0:2], byteorder='little')

            return {
                'floor_area': floor_area
            }

        except Exception as e:
            _LOGGER.warning("Failed to parse custom floor area value: %s", e)
            return None

    async def write_floor_area(self, value: int) -> bool:
        """Write custom floor area configuration to device.
        
        Args:
            value (int): Floor area in square meters (m²)
            
        Returns:
            bool: True if successful, False otherwise
            
        Note:
            Maximum value is 65535 due to uint16_t limitation
        """

        if not (0 <= value <= 65535):
            _LOGGER.error("Floor area value must be between 0 and 65535 for uint16_t")
            return False

        try:
            data = value.to_bytes(2, byteorder='little')
        except Exception as e:
            _LOGGER.warning("Failed to prepare custom floor area value data: %s", e)
            return False

        return await self._ble_write(FLOOR_AREA_UUID, data)


    async def read_energy_unit(self) -> dict:
        """Read energy unit configuration from device.
        
        Returns:
            dict with keys:
                currency_code (int): Currency code number
                currency_name (str): Currency name (e.g. "EUR")
                currency_symbol (str): Currency symbol (e.g. "€") 
                price (float): Price per kWh
                
        Example return value:
            {
                'currency_code': 1,
                'currency_name': 'EUR',
                'currency_symbol': '€',
                'price': 12.34
            }
        """

        data = await self._ble_read(ENERGY_UNIT_UUID)
        if not data:
            return None

        try:
            
            # Parse currency (first byte)
            currency = data[0]
            
            # Parse price (bytes 2-3 as unsigned int16, scaled by 100)
            price_raw = int.from_bytes(data[2:4], byteorder='little')
            price = price_raw / 100.0

            return {
                'currency_code': currency,
                'currency_name': CURRENCY_MAP.get(currency, "Unknown"),
                'currency_symbol': CURRENCY_SYMBOLS.get(currency, ""),
                'price': price
            }
        
        except Exception as e:
            _LOGGER.warning("Failed to parse energy unit configuration: %s", e)
            return None

    async def write_energy_unit(self, currency: int, price: float) -> bool:
        """Write energy unit configuration to device."""

        try:
            if not (0 <= price <= 655.35):
                _LOGGER.error("Price must be between 0 and 655.35: %s", price)
                return False

            # Prepare data packet
            data = bytearray(4)
            data[0] = currency  # Currency code
            data[1] = 0  # Unused byte

            # Convert price to integer scaled by 100
            price_raw = int(price * 100)
            data[2:4] = price_raw.to_bytes(2, byteorder='little')

            # Write to device
        except Exception as e:
            _LOGGER.warning("Failed to prepare energy unit configuration data: %s", e)
            return False

        return await self._ble_write(ENERGY_UNIT_UUID, data)


    async def read_power_consumption(self) -> dict:
        """Read real time power consumption data.
        
        Returns:
            dict: Dictionary containing:
                - 'timestamp': datetime object of the header timestamp
                - 'measurements': List of dicts with:
                    - 'timestamp': datetime of the measurement
                    - 'ratio': Power consumption ratio (0-100%)
        """
        try:
            data = await self.read_split_characteristic(REAL_TIME_INDICATION_POWER_CONSUMPTION_UUID)
            
            if not data:
                _LOGGER.debug("No data received from device")
                return None
            
            if len(data) < 4:
                _LOGGER.warning("Data too short, expected at least 4 bytes for header")
                return None

            # Parse header timestamp
            hour = data[0]  # uint8 hour
            day = data[1]   # uint8 day
            month = data[2] # uint8 month
            year = data[3]  # uint8 year (0-255)

            measurements = []

            # Process measurement pairs (delta hour and ratio)
            for i in range(24):  # 25 hours of data
                offset = 4 + i * 2  # Start after header, 2 bytes per measurement
                delta_hours = data[offset]
                ratio = data[offset + 1]
                
                # Skip invalid values (0xff)
                if ratio == 0xff:
                    continue
                    
                # Calculate timestamp for this measurement
                timestamp = datetime(2000 + year, month, day, hour, tzinfo=dt_util.UTC) - timedelta(hours=delta_hours)
                
                measurements.append({
                    'timestamp': timestamp,
                    'ratio': ratio
                })
            
            return {
                'timestamp': timestamp,
                'measurements': measurements
            }

        except BleakError as e:
            _LOGGER.warning("BLE error reading power consumption: %s", e)
            await self.disconnect()
            return None

        except Exception as e:
            _LOGGER.warning("Error reading power consumption: %s", e)
            return None

    async def read_monitoring_data(self) -> Optional[dict]:
        """Read monitoring data from device which contains power and temperature history.
        
        Returns:
            dict containing:
                - daily_power: List of last 7 days power consumption data
                - monthly_power: List of last 12 months power consumption data
                - temperature_history: List of hourly temperature readings for past week
            None if read fails
        """
        try:
            data = await self.read_split_characteristic(MONITORING_DATA_UUID)
            if not data:
                _LOGGER.debug("No monitoring data received from device")
                return None
            
            result = {
                'daily_power': [],
                'monthly_power': [],
                'temperature_history': []
            }

            # Current position in data array
            pos = 0

            # Parse daily power data (7 days)
            # Daily data starts from beginning of the data buffer (pos = 0)
            # Header: day(1) + month(1) + year(1) = 3 bytes
            # Data per day: delta_day(1) + ratio(1) = 2 bytes
            if len(data) >= pos + 3:
                day = data[pos]
                month = data[pos + 1]
                year = data[pos + 2]
                pos += 3

                # Process 7 days of power data
                for _ in range(7):
                    if len(data) >= pos + 2:
                        delta_days = data[pos]
                        ratio_raw = data[pos + 1]
                        
                        timestamp = datetime(2000 + year, month, day, tzinfo=dt_util.UTC) - timedelta(days=delta_days)
                        
                        # Convert raw value to ratio, using None for unset values (0xff)
                        ratio = None if ratio_raw == 0xff else ratio_raw

                        # Store the values in result
                        result['daily_power'].append({
                            'time': timestamp.isoformat(),
                            'ratio': ratio
                        })
                        pos += 2

            # Parse last 12 month power data
            # Offset calculation: daily data uses 19 bytes, so monthly starts at 19
            if len(data) >= 19:  # Need at least daily data section length before monthly
               pos = 19  # Skip daily data section (3 bytes header + 7 * 2 bytes data = 19)
               
               # Header: month(1) + year(1) = 2 bytes
               # Data per month: delta_month(1) + ratio(1) = 2 bytes
               if len(data) >= pos + 2:
                   month = data[pos]
                   year = data[pos + 1]
                   pos += 2

                   # Process 12 months of power data
                   for _ in range(12):
                       if len(data) >= pos + 2:
                           delta_months = data[pos]
                           ratio_raw = data[pos + 1]

                           # Calculate timestamp using relativedelta for accurate month subtraction
                           timestamp = datetime(2000 + year, month, 1, tzinfo=dt_util.UTC) - relativedelta(months=delta_months)
                           
                           # Convert raw value to ratio, using None for unset values (0xff)
                           ratio = None if ratio_raw == 0xff else ratio_raw

                           # Store the values in result
                           result['monthly_power'].append({
                               'time': timestamp.isoformat(),
                               'ratio': ratio
                           })
                           pos += 2

            # Parse temperature history (24 hours * 7 days)
            if len(data) >= 47:  # 19 (daily) + 28 (monthly) bytes minimum before temperature data
               pos = 47  # Skip daily (19) and monthly (28) data sections
               
               # Header: hour(1) + day(1) + month(1) + year(1) = 4 bytes
               if len(data) >= pos + 4:
                   hour = data[pos]
                   day = data[pos + 1]
                   month = data[pos + 2]
                   year = data[pos + 3]
                   pos += 4

                   # Process 168 hours (24*7) of temperature data
                   # Data per hour: delta_hour(1) + floor_temp(2) + room_temp(2) = 5 bytes
                   for _ in range(168):
                       if len(data) >= pos + 5:
                           delta_hours = data[pos]
                           
                           # Get raw temperature values from bytes
                           floor_temp_raw = int.from_bytes(data[pos+1:pos+3], byteorder='little', signed=True)
                           room_temp_raw = int.from_bytes(data[pos+3:pos+5], byteorder='little', signed=True)

                           # Calculate timestamp for this measurement
                           timestamp = datetime(2000 + year, month, min(max(1, day), 28), hour, tzinfo=dt_util.UTC) - timedelta(hours=delta_hours)

                           # Convert raw values to temperatures, using None for unset values (0x7fff)
                           floor_temp = None if floor_temp_raw == 0x7fff else floor_temp_raw / 10
                           room_temp = None if room_temp_raw == 0x7fff else room_temp_raw / 10
                           
                           # Store the values in result
                           result['temperature_history'].append({
                               'time': timestamp.isoformat(),
                               'floor_temp': floor_temp,
                               'room_temp': room_temp,
                           })
                           pos += 5

            return result

        except BleakError as e:
            _LOGGER.warning("BLE error reading monitoring data: %s", e)
            await self.disconnect()
            return None
        
        except Exception as e:
            _LOGGER.warning("Error reading monitoring data: %s", e)
            return None

    async def read_vacation_time(self) -> Optional[dict]:
        """Read vacation time configuration from device."""
        data = await self._ble_read(VACATION_TIME_UUID)
        if not data:
            return None

        try:
            # Parse wall clock time from device
            from_year = 2000 + data[0]
            from_month = data[1]
            from_day = data[2]
            from_hour = data[3]
            from_minute = data[4]
            
            # Create wall clock time as naive datetime
            time_from_naive = datetime(from_year, from_month, from_day, from_hour, from_minute)
            # Assume it's local time and convert to UTC for Home Assistant
            time_from = dt_util.as_utc(dt_util.as_local(time_from_naive))
            
            # Parse time to wall clock time
            to_year = 2000 + data[5]
            to_month = data[6]
            to_day = data[7]
            to_hour = data[8]
            to_minute = data[9]
            
            # Create wall clock time as naive datetime
            time_to_naive = datetime(to_year, to_month, to_day, to_hour, to_minute)
            # Assume it's local time and convert to UTC for Home Assistant
            time_to = dt_util.as_utc(dt_util.as_local(time_to_naive))

            # Parse temperature offset
            offset_temp_raw = int.from_bytes(data[10:12], byteorder='little', signed=True)
            offset_temperature = offset_temp_raw / 100
            
            # Parse remaining fields
            # Convert percentage offset byte to signed int (range -128 to 127)
            offset_percentage = int.from_bytes([data[12]], byteorder='little', signed=True)
            enabled = bool(data[13])
            active = bool(data[14])

            return {
                'time_from': time_from,
                'time_to': time_to,
                'offset_temperature': offset_temperature,
                'offset_percentage': offset_percentage,
                'enabled': enabled,
                'active': active,
                'raw_data': data.hex()  # Include raw data for logging
            }

        except Exception as e:
            _LOGGER.warning("Failed to parse vacation time: %s", e)
            return None

    async def write_calendar_mode(self, enabled: bool) -> bool:
        """Write calendar mode setting to device.
        
        Args:
            enabled: True to enable calendar mode, False to disable
            
        Returns:
            True if successful, False otherwise
        """
        data = bytes([1 if enabled else 0])
        result = await self._ble_write(CALENDAR_MODE_UUID, data)
        if result:
            _LOGGER.debug("Action [Calendar Mode %s] for [%s]: success",
                         "Enable" if enabled else "Disable", self.mac_address)
        return result

    async def read_calendar_day(self, day: int) -> Optional[dict]:
        """Read calendar day programs from device.
        
        Args:
            day: Day number (1=Monday, 7=Sunday)
            
        Returns:
            dict with 'day' and 'programs' list, or None if failed
        """
        try:
            await self.ensure_connection()
            if not (1 <= day <= 7):
                _LOGGER.error("Invalid day number: %s (must be 1-7)", day)
                return None

            # Write day number to control characteristic
            await self.client.write_gatt_char(CALENDAR_CONTROL_UUID, bytes([day]), response=True)
            
            # Add small delay to let device process the request
            await asyncio.sleep(0.2)
            
            # Read split data from calendar day characteristic
            data = await self.read_split_characteristic(CALENDAR_DAY_UUID)
            
            if not data:
                _LOGGER.error("No calendar day data received")
                return None

            # Parse day number
            day_number = data[0]

            # Calculate number of programs from data length
            if len(data) == 1:
                # Empty day
                programs = []
            elif (len(data) - 1) % 8 == 0:
                # Valid program data: (length - 1) must be divisible by 8
                program_count = (len(data) - 1) // 8
                programs = []
                
                # Parse each program
                for i in range(program_count):
                    offset = 1 + i * 8  # Start after day byte, 8 bytes per program
                    
                    start_hour = data[offset]
                    start_minute = data[offset + 1]
                    end_hour = data[offset + 2]
                    end_minute = data[offset + 3]
                    temp_offset_raw = int.from_bytes(data[offset + 4:offset + 6], byteorder='little', signed=True)
                    temp_offset = temp_offset_raw / 100.0  # Convert from device format (200 = 2.0°C)
                    power_offset = int.from_bytes([data[offset + 6]], byteorder='little', signed=True)
                    enabled = bool(data[offset + 7])
                    
                    program = {
                        'start_hour': start_hour,
                        'start_minute': start_minute,
                        'end_hour': end_hour,
                        'end_minute': end_minute,
                        'temp_offset': temp_offset,
                        'power_offset': power_offset,
                        'enabled': enabled
                    }
                    programs.append(program)

            else:
                _LOGGER.error("Invalid calendar day data length: %d (not 1 + multiple of 8)", len(data))
                return None

            return {
                'day': day_number,
                'programs': programs
            }

        except BleakError as e:
            _LOGGER.warning("BLE error reading calendar day: %s", e)
            await self.disconnect()
            return None
        except Exception as e:
            _LOGGER.warning("Failed to read calendar day: %s", e)
            return None

    async def write_calendar_day(self, day: int, programs: list) -> bool:
        """Write calendar day programs to device.
        
        Args:
            day: Day number (1=Monday, 7=Sunday)
            programs: List of up to 6 program dicts with keys:
                     start_hour, start_minute, end_hour, end_minute,
                     temp_offset, power_offset, enabled
                     
        Returns:
            True if successful, False otherwise
        """
        try:
            await self.ensure_connection()
            if not (1 <= day <= 7):
                _LOGGER.error("Invalid day number: %s (must be 1-7)", day)
                return False
                
            if len(programs) > 6:
                _LOGGER.error("Too many programs: %s (max 6)", len(programs))
                return False

            # Create 49-byte data packet
            data = bytearray(49)
            data[0] = day
            
            # Fill programs (pad with zeroes if less than 6)
            for i in range(6):
                offset = 1 + i * 8
                
                if i < len(programs):
                    program = programs[i]
                    
                    # Validate program data
                    for field in ['start_hour', 'start_minute', 'end_hour', 'end_minute', 'temp_offset', 'power_offset', 'enabled']:
                        if field not in program:
                            _LOGGER.error("Missing field '%s' in program %d", field, i)
                            return False
                    
                    data[offset] = program['start_hour']
                    data[offset + 1] = program['start_minute']
                    data[offset + 2] = program['end_hour']
                    data[offset + 3] = program['end_minute']
                    
                    # Convert temperature offset to device format (20.5°C = 2050)
                    temp_raw = int(program['temp_offset'] * 100)
                    data[offset + 4:offset + 6] = temp_raw.to_bytes(2, byteorder='little', signed=True)
                    
                    # Power offset as signed int8
                    data[offset + 6] = program['power_offset'].to_bytes(1, byteorder='little', signed=True)[0]
                    data[offset + 7] = 1 if program['enabled'] else 0
                else:
                    # Empty program - all zeros (disabled)
                    for j in range(8):
                        data[offset + j] = 0

            # Tell device which day we're writing to
            await self.client.write_gatt_char(CALENDAR_CONTROL_UUID, bytes([day]), response=True)

            # Write using split protocol
            await self.write_split_characteristic(CALENDAR_DAY_UUID, bytes(data))

            # Add small delay to let device process the request
            await asyncio.sleep(0.2)
            
            # Save to flash (write 0 to control characteristic)
            await self.client.write_gatt_char(CALENDAR_CONTROL_UUID, bytes([0]), response=True)

            return True

        except BleakError as e:
            _LOGGER.warning("BLE error writing calendar day: %s", e)
            await self.disconnect()
            return False
        except Exception as e:
            _LOGGER.warning("Failed to write calendar day: %s", e)
            return False

    async def read_force_control(self) -> Optional[dict]:
        """Read force control / external control configuration from device.

        Returns:
            dict with keys:
                enabled (bool): External control enabled/disabled
                mode (int): External control mode number
                mode_name (str): Human-readable mode name
                temperature (float): Absolute temperature for mode 5 (5-35°C)
                temperature_offset (float): Temperature offset for mode 6 (-20 to +20°C)
                data_length (int): Length of data received from device
            None if read fails

        Note:
            Mode values:
            - 1 = OFF (disabled)
            - 5 = Temperature (absolute target, uses byte[8-9])
            - 6 = Temperature change (offset from normal, uses byte[12-13])
        """
        try:
            await self.ensure_connection()

            data = await self.client.read_gatt_char(FORCE_CONTROL_UUID)
            device_name = self.device_name or "Unknown Device"

            if len(data) >= 19:
                # Extended 19-byte format
                mode = data[17]

                # Mode 5 "Temperature": absolute temperature in byte[8-9]
                temp_raw = int.from_bytes(data[8:10], byteorder='little')
                temperature = temp_raw / 10.0

                # Mode 6 "Temperature change": offset in byte[12-13] (signed)
                offset_raw = int.from_bytes(data[12:14], byteorder='little', signed=True)
                temperature_offset = offset_raw / 10.0

                mode_names = EXTERNAL_CONTROL_MODES

                _LOGGER.debug(
                    "Read Force Control %s for %s: mode=%s, temp=%.1f°C, offset=%+.1f°C",
                    device_name, self.mac_address,
                    mode_names.get(mode, "Unknown"),
                    temperature, temperature_offset
                )

                return {
                    'enabled': mode in (5, 6),
                    'mode': mode,
                    'mode_name': EXTERNAL_CONTROL_MODES.get(mode, "Unknown"),
                    'temperature': temperature,
                    'temperature_offset': temperature_offset,
                    'data_length': len(data)
                }

            elif len(data) == 1:
                # Original 1-byte format (potentiometer value only 0-100%)
                _LOGGER.debug(
                    "Read Force Control %s for %s: legacy 1-byte format, value=%d%%",
                    device_name, self.mac_address, data[0]
                )

                return {
                    'enabled': False,
                    'mode': 1,
                    'mode_name': "Off",
                    'temperature': 20.0,
                    'temperature_offset': 0.0,
                    'data_length': len(data)
                }

            _LOGGER.error(
                "Read Force Control %s for %s: unexpected data length %d",
                device_name, self.mac_address, len(data)
            )
            return None

        except BleakError as e:
            _LOGGER.warning("BLE error reading force control: %s", e)
            await self.disconnect()
            return None

        except Exception as e:
            _LOGGER.warning("Failed to read force control: %s", e)
            return None

    async def write_force_control(self, mode: int, temperature: float, temperature_offset: float) -> bool:
        """Write force control / external control configuration to device.

        Args:
            enabled: Enable/disable external control
            mode: External control mode (5=Temperature, 6=Temperature change)
            temperature: Absolute temperature for mode 5 (5.0 to 50.0°C)
            temperature_offset: Temperature offset for mode 6 (-20.0 to +20.0°C)

        Returns:
            True if successful, False otherwise
        """
        try:
            await self.ensure_connection()

            device_name = self.device_name or "Unknown Device"

            # Read current values to preserve unchanged settings
            current = await self.client.read_gatt_char(FORCE_CONTROL_UUID)
            if not current:
                _LOGGER.error(
                    "Write Force Control %s for %s: could not read current settings",
                    device_name, self.mac_address
                )
                return False

            # Check for legacy 1-byte format (old firmware)
            if len(current) < 19:
                _LOGGER.debug(
                    "Write Force Control %s for %s: device uses legacy 1-byte format, external control not supported",
                    device_name, self.mac_address
                )
                return False

            # Start with current data
            data = bytearray(current)

            # Update mode 5 temperature (byte[8-9])
            temp_value = int(max(5.0, min(35.0, temperature)) * 10)
            data[8:10] = temp_value.to_bytes(2, byteorder='little')

            # Update mode 6 offset (byte[12-13], signed)
            offset_value = int(max(-20.0, min(20.0, temperature_offset)) * 10)
            data[12:14] = offset_value.to_bytes(2, byteorder='little', signed=True)

            # Set mode
            if mode in EXTERNAL_CONTROL_MODES:
                data[17] = mode

            await self.client.write_gatt_char(FORCE_CONTROL_UUID, data, response=True)

            mode_names = {2: "Off", 5: "Temperature", 6: "Temperature change"}

            _LOGGER.debug(
                "Wrote Force Control %s for %s: mode=%s, temp=%.1f°C, offset=%+.1f°C",
                device_name, self.mac_address,
                mode_names.get(data[17], "Unknown"),
                temperature, temperature_offset
            )

            return True

        except BleakError as e:
            _LOGGER.warning("BLE error writing force control: %s", e)
            await self.disconnect()
            return False

        except Exception as e:
            _LOGGER.warning("Failed to write force control: %s", e)
            return False
