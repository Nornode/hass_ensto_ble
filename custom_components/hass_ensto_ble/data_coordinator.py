"""DataUpdateCoordinator for Ensto BLE thermostats."""
import asyncio
import time
import logging
from datetime import timedelta
from typing import Optional

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import REAL_TIME_INDICATION_UUID

_LOGGER = logging.getLogger(__name__)

CONNECT_SETTLE_SECONDS = 3
CONFIG_REFRESH_INTERVAL = 3600  # 60 minutes
CLOCK_SYNC_INTERVAL = 14400    # 4 hours

class EnstoDataUpdateCoordinator(DataUpdateCoordinator[dict]):
    """Coordinator to manage fetching Ensto BLE real-time data."""

    def __init__(self, hass: HomeAssistant, manager) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"Ensto BLE {manager.mac_address}",
            update_interval=timedelta(seconds=30),
        )
        self.manager = manager
        self._last_config_refresh: Optional[float] = None
        self._last_clock_sync: Optional[float] = None

    async def _async_update_data(self) -> dict:
        """Fetch data from the Ensto BLE thermostat."""
        try:
            await self.manager.ensure_connection()
        except Exception as err:
            raise UpdateFailed(f"Connection failed for {self.manager.mac_address}: {err}") from err

        # Wait for device to settle if just connected
        connected_at = self.manager.connected_at
        if connected_at is not None:
            wait = CONNECT_SETTLE_SECONDS - (time.monotonic() - connected_at)
            if wait > 0:
                _LOGGER.debug(
                    "Waiting %.1fs for %s (%s) to settle after connecting",
                    wait, self.manager.device_name or "device", self.manager.mac_address
                )
                await asyncio.sleep(wait)

        raw_data = await self.manager._ble_read_split(REAL_TIME_INDICATION_UUID, "real-time indication")
        if not raw_data:
            raise UpdateFailed(f"Failed to read real-time data for {self.manager.mac_address}")

        data = self.manager.parse_real_time_indication(raw_data)
        if not data:
            raise UpdateFailed(f"Failed to parse real-time data for {self.manager.mac_address}")

        now_mono = time.monotonic()

        # Check if slow config refresh is due (on connect or every 60 min)
        if (
            self._last_config_refresh is None
            or now_mono - self._last_config_refresh > CONFIG_REFRESH_INTERVAL
        ):
            try:
                await self.manager.async_refresh_config_cache()
                self._last_config_refresh = now_mono
            except Exception as e:
                _LOGGER.warning("Error during config refresh for %s: %s", self.manager.mac_address, e)

        # Check if clock sync is due (on connect or every 4 hours)
        if (
            self._last_clock_sync is None
            or now_mono - self._last_clock_sync > CLOCK_SYNC_INTERVAL
        ):
            try:
                date_data = await self.manager.read_date_and_time()
                if date_data:
                    from homeassistant.util import dt as dt_util
                    from datetime import datetime
                    device_utc = datetime(
                        date_data['year'], date_data['month'], date_data['day'],
                        date_data['hour'], date_data['minute'], date_data['second'],
                        tzinfo=dt_util.UTC
                    )
                    ha_utc = dt_util.utcnow()
                    diff = abs((ha_utc - device_utc).total_seconds())
                    if diff > 60:
                        await self.manager.sync_date_and_time(diff)
                    from homeassistant.helpers.dispatcher import async_dispatcher_send
                    async_dispatcher_send(self.hass, f"ensto_datetime_update_{self.manager.mac_address}")
                self._last_clock_sync = now_mono
            except Exception as e:
                _LOGGER.warning("Error during clock sync for %s: %s", self.manager.mac_address, e)

        return data

    async def get_real_time_data(self, max_age_seconds=25):
        """Backward-compatible helper returning coordinator data."""
        if self.data is None:
            await self.async_refresh()
        return self.data

# Backward compatibility alias
EnstoRealTimeCoordinator = EnstoDataUpdateCoordinator
