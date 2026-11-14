"""Base entity for Ensto BLE integration."""
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from .const import DOMAIN

class EnstoBaseEntity(Entity):
    """Base entity class for Ensto BLE entities."""

    _attr_has_entity_name = True

    def __init__(self, manager):
        """Initialize the base entity."""
        self._manager = manager

    @property
    def available(self) -> bool:
        """Return True if device is available and data is fresh."""
        return getattr(self._manager, "available", True)

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info for Home Assistant device registry.

        Contains:
        - identifiers: MAC address as the device identifier
        - name: device name or default name with MAC
        - manufacturer: fixed value "Ensto"
        - model: model number if available
        - sw_version: software version if available
        - hw_version: hardware version if available
        """
        name = self._manager.device_name or f"Ensto Thermostat {self._manager.mac_address}"

        # Keyword arguments work with both the TypedDict (before 2026.11) and the dataclass
        return DeviceInfo(
            identifiers={(DOMAIN, self._manager.mac_address)},
            name=name,
            manufacturer="Ensto",
            model=self._manager.model_number or "model name not available",
            sw_version=getattr(self._manager, "sw_version", None),
            hw_version=getattr(self._manager, "hw_version", None),
        )

class EnstoCoordinatorEntity(CoordinatorEntity):
    """Base entity class for Ensto BLE entities backed by DataUpdateCoordinator."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator, manager):
        """Initialize the coordinator entity."""
        super().__init__(coordinator)
        self._manager = manager

    @property
    def available(self) -> bool:
        """Return True if coordinator update succeeded and device is available."""
        return self.coordinator.last_update_success and getattr(self._manager, "available", True)

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info for Home Assistant device registry."""
        name = self._manager.device_name or f"Ensto Thermostat {self._manager.mac_address}"

        return DeviceInfo(
            identifiers={(DOMAIN, self._manager.mac_address)},
            name=name,
            manufacturer="Ensto",
            model=self._manager.model_number or "model name not available",
            sw_version=getattr(self._manager, "sw_version", None),
            hw_version=getattr(self._manager, "hw_version", None),
        )

class EnstoConfigEntity(EnstoBaseEntity):
    """Base entity class for slow-changing configuration entities."""

    _attr_should_poll = False

    async def async_added_to_hass(self) -> None:
        """Subscribe to config cache updates."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"ensto_config_updated_{self._manager.mac_address}",
                self.async_write_ha_state,
            )
        )
