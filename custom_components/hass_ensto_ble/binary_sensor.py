"""Support for Ensto BLE binary sensors."""
from __future__ import annotations

import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import EnstoConfigEntry
from .base_entity import EnstoCoordinatorEntity

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnstoConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up binary sensors from a config entry."""
    manager = entry.runtime_data
    coordinator = manager.get_real_time_coordinator()

    async_add_entities([
        EnstoConnectivityBinarySensor(coordinator, manager),
    ])

class EnstoConnectivityBinarySensor(EnstoCoordinatorEntity, BinarySensorEntity):
    """Diagnostic binary sensor indicating whether the thermostat BLE link is active."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Connectivity"

    def __init__(self, coordinator, manager) -> None:
        """Initialize the connectivity binary sensor."""
        super().__init__(coordinator, manager)
        self._attr_unique_id = f"{dr.format_mac(self._manager.mac_address)}_connectivity"

    @property
    def available(self) -> bool:
        """Connectivity sensor stays available to report disconnected status."""
        return True

    @property
    def is_on(self) -> bool:
        """Return True if coordinator update succeeded and device is connected and authenticated."""
        return (
            self.coordinator.last_update_success
            and self._manager.client is not None
            and getattr(self._manager.client, "is_connected", False)
            and getattr(self._manager, "_authenticated", False)
        )
