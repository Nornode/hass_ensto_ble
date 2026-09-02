# RCA: ECO16BT (AA:BB:CC:DD:EE:FF) repeated "Device not found" / connectivity loss

**Scope:** Investigation only. No code was changed as part of this document — findings are
recorded here first, per request, before any fix is implemented.

**Method:** Static analysis of `custom_components/hass_ensto_ble/*.py` on `main`, plus live,
read-only diagnostics collected via SSH against the reporter's Home Assistant OS instance
(HA Core running in the `homeassistant` Docker container). Live evidence
was pulled from `docker logs homeassistant --since 48h/72h`, `bluetoothctl`, and `ha supervisor logs`.

---

## 1. TL;DR

The integration's own `ERROR ... Failed to connect: Device ... not found` message is a *symptom*,
not the disease. It fires whenever `bluetooth.async_ble_device_from_address()` returns `None`,
i.e. Home Assistant's Bluetooth manager currently has **no advertisement on record** for this MAC
from any registered scanner (local adapter or ESPHome proxy). Once that happens, the integration
has no recovery path other than the full `ConfigEntryNotReady` retry loop, which is capped at a
~10 minute interval and retries forever with no backing-off/backing-on logic tied to actual device
visibility.

Live logs show this is not a one-off — it is a *sustained, hours-long outage* that started with a
concrete, evidenced trigger:

1. **20:55:04 (2026-08-31)** — a burst of ~15 simultaneous BLE failures across many unrelated
   code paths (sensor, select, vacation-time, floor-limits) within the same few milliseconds.
   This is direct evidence that the integration's ~31 independently-polling entities share one
   `BleakClient` with **no lock serializing GATT operations**, so one connection loss cascades
   instantly into a wall of unrelated failures instead of one clean error.
2. **20:57:43–21:08** — the config entry itself failed to reconnect, and one retry attempt
   surfaced a very specific upstream error: `No backend with an available connection slot ...
   6 scanner(s) registered, 6 scanning, 2 connectable`. This is Home Assistant's Bluetooth
   manager explicitly reporting that all *connectable* slots (local adapter + ESPHome proxy)
   were exhausted at that moment.
3. **21:10:21 onward, for the next ~17 hours (through 14:16 today)** — every single retry says
   "Device ... not found", meaning HA's Bluetooth manager has not seen **one single
   advertisement** from this device since ~20:58. This is not a code bug in the polling logic
   anymore — it's the device being genuinely absent from every scanner's view, for hours,
   correlated with the point where multiple concurrent/aborted BLE connection attempts happened.

The two problems compound each other: the no-lock polling architecture is what most plausibly
drove the device into this long silent state (via overlapping/aborted connection attempts around
20:55–21:08), and the integration's all-or-nothing setup + fixed-interval retry is why, once that
happened, there was no way to recover other than waiting or manually intervening.

---

## 2. Confirmed live evidence (from `docker logs homeassistant`)

### 2.1 The mass-failure burst (20:55:04, 2026-08-31)

Within a ~10 ms window, the log shows (abbreviated, full text pulled via SSH):

```
20:55:04.396 [ensto_thermostat_manager] Device not connected.
20:55:04.396 [select] Error updating floor sensor type: 'NoneType' object has no attribute 'read_gatt_char'
20:55:04.402 [ensto_thermostat_manager] Device not connected.   (x7 more, same timestamp)
20:55:04.403 [ensto_thermostat_manager] Error reading vacation time: 'NoneType' object has no attribute 'read_gatt_char'
20:55:04.404 [ensto_thermostat_manager] Failed to read floor limits: 'NoneType' object has no attribute 'read_gatt_char'
```

Roughly 15 error lines land in the same 10 ms window, from at least 4 distinct call sites
(`select` platform, vacation time read, floor limits read, plus several generic "Device not
connected." lines from other sensors/numbers). This is the signature of ~31 entities polling on
the same 30-second `SCAN_INTERVAL` all discovering, independently and simultaneously, that
`self.client` is `None`/disconnected — see §3.1 for the code-level cause.

A second, near-identical burst repeats at `20:55:34` (the next 30s poll tick), confirming this is
the polling cycle, not a one-off event.

### 2.2 The reconnect attempt and connection-slot exhaustion (20:57:43–21:08:04)

```
20:57:43.767 Failed to connect: AA:BB:CC:DD:EE:FF - ... Failed to connect after 4 attempt(s): TimeoutError
20:58:22.768 Setup of config entry 'ECO16BT AA:BB:CC:DD:EE:FF' for hass_ensto_ble integration cancelled
             File ".../__init__.py", line 72, in async_setup_entry
             File ".../ensto_thermostat_manager.py", line 167, in ensure_connection
             File ".../ensto_thermostat_manager.py", line 118, in connect
               self.client = await establish_connection(BleakClientWithServiceCache, device, self.mac_address)
21:00:24.398 Failed to connect: ... Failed to connect after 4 attempt(s): TimeoutError
21:02:01.594 Failed to connect: ... Failed to connect after 4 attempt(s): TimeoutError
21:03:32.776 Failed to connect: ... Failed to connect after 4 attempt(s): TimeoutError
21:04:28.925 Failed to connect: ... Failed to connect after 9 attempt(s): No backend with an
             available connection slot that can reach address AA:BB:CC:DD:EE:FF was found: in
             connectable history; 6 scanner(s) registered, 6 scanning, 2 connectable; last
             advertisement 341s ago via hci0 (11:22:33:44:55:66): The proxy/adapter is out of
             connection slots or the device is no longer reachable; Add additional proxies
21:05:45.054 (same "9 attempt(s)" / "2 connectable" message, last advertisement 417s ago)
21:07:41.185 (same message again, last advertisement 533s ago)
```

This is bleak-retry-connector / habluetooth's own diagnostic message, not something the
integration constructs — it directly states there were **6 registered scanners** (the local
`hci0` Realtek adapter plus 5 remote/ESPHome proxy sources) but only **2 "connectable"** at that
moment, and neither had a free slot to establish a fresh GATT connection to this MAC. The
`last advertisement Ns ago` figure climbs from 341s → 417s → 533s across these three retries,
meaning the device's last seen advertisement was frozen at ~20:58:47 and nothing saw a fresher one
in the following ~9 minutes.

### 2.3 Sustained "Device not found" (21:10:21, 2026-08-31 → 14:16:08, 2026-09-01, ongoing)

From 21:10:21 onward, every single retry (about every ~10 minutes, matching the interval in the
originally-reported logs) reverts to the plain:

```
Failed to connect: Device AA:BB:CC:DD:EE:FF not found
Error setting up Ensto BLE: Device AA:BB:CC:DD:EE:FF not found
```

This is `bluetooth.async_ble_device_from_address()` returning `None` — i.e. **no scanner has any
advertisement on record for this MAC at all**, not even a stale/weak one. This has now persisted
continuously for **~17+ hours** at the time of writing, confirmed by the last log line pulled
during this investigation (`14:16:08.125`, still "not found").

### 2.4 Current device state (live check via `bluetoothctl`, 2026-09-01 14:19)

```
Device AA:BB:CC:DD:EE:FF (public)
  Name: ECO16BT xxxxxx
  Paired: yes / Bonded: yes / Trusted: yes
  Connected: no
  RSSI: 0xffffffb0 (-80)
```

The local BlueZ stack on the HA host still has the device bonded and knows a (cached) RSSI of
**-80 dBm**, which is a weak/marginal signal for a reliable BLE GATT connection (typical usable
range is roughly -70 dBm or better for stable connections; -80 dBm is at the edge where
connection attempts intermittently time out even when the device is technically "in range"). This
figure may be stale (last-seen, not necessarily current), but it is consistent with this device
sitting at the margin of reception for the local adapter, making it more dependent on the ESPHome
proxy fleet than a strongly-placed device would be — and thus more exposed to the "no connectable
slot" condition seen in §2.2 when proxies are busy or momentarily unavailable.

### 2.5 Ruled out / inconclusive

- **HA Core crash-loop as the trigger:** two `Home Assistant Core service shutdown` entries were
  found in the container's s6-supervisor log stream, but that log format has no date stamps and
  could not be reliably correlated to the 2026-08-31 20:55 incident window; `ha supervisor logs`
  shows no watchdog/unresponsive-restart events near that time. **Not confirmed as a cause** —
  noted only so it isn't re-investigated as a promising lead without new evidence.
- **NAS mount failures** (`NAS_Media`, `NAS_audio_music`, `NAS_Backup` reload/restart failures,
  recurring every ~15 minutes in supervisor logs) are a real, ongoing issue on this HA instance but
  are unrelated to Bluetooth/Ensto — a separate storage/systemd-mount problem, out of scope here.
- The originally suspected "Home Assistant MCP" on the HA host was not reachable from this
  session's tool environment; all live diagnostics above were instead gathered via direct
  read-only SSH commands, as explicitly authorized.

---

## 3. Root causes, ranked by evidence strength

### 3.1 CONFIRMED (live evidence + code): No shared lock across concurrently-polling entities

`custom_components/hass_ensto_ble/ensto_thermostat_manager.py` has one `asyncio.Lock`
(`self._connect_lock`, line 59) that serializes only the `connect()` call itself (line 104–160).
It does **not** protect the ~30+ individual `read_*`/`write_*` GATT operations. Grepping the file
shows the same pattern repeated over 20 times:

```python
if not self.client or not self.client.is_connected:
    _LOGGER.error("Device not connected.")
    return None   # or False
...
except BleakError as e:
    ...
    self.client = None
```

Meanwhile, `sensor.py`, `switch.py`, `select.py`, and `number.py` define ~31 entities, all with
`_attr_should_poll = True` and the same `SCAN_INTERVAL = timedelta(seconds=30)` from `const.py`,
each independently calling into the shared manager on its own 30-second tick, against the single
shared `BleakClient`. There is no `DataUpdateCoordinator` and no GATT-operation-level mutex, aside
from the partial `EnstoRealTimeCoordinator` (`data_coordinator.py`), which only wraps the
real-time-indication read and is only used by a subset of sensors.

Consequence, confirmed by §2.1: the instant `self.client` becomes `None` or disconnected (for any
reason — signal loss, a `BleakError` in one unrelated read, a genuine device-side disconnect), every
other entity's next poll tick independently discovers this and fails, all within the same event-loop
pass, each logging its own error. This is exactly the burst pattern observed twice in a row
(20:55:04 and 20:55:34, 30 seconds apart).

**Important nuance found during this investigation:** not all read paths behave identically.
`read_split_characteristic()` (line 230, used by real-time data / power consumption / monitoring
data / calendar day) calls `await self.ensure_connection()` at its start (line 243) and so *can*
proactively trigger a reconnect. The ~20 simpler single-characteristic `read_*`/`write_*` methods
(vacation time, floor limits, floor sensor type, alarm, boost, etc.) do **not** call
`ensure_connection()` — they only check `is_connected` and bail out, relying on some other call
path to eventually restore the connection. This inconsistency means most entities cannot self-heal
after a disconnect; they will keep logging "Device not connected." every 30 seconds until either a
`read_split_characteristic()`-based caller happens to reconnect, or the whole config entry is torn
down and rebuilt.

### 3.2 CONFIRMED (live evidence): BLE connection-slot exhaustion during the reconnect window

The environment has 6 registered BLE scan sources (local Realtek `hci0` adapter + ESPHome
Bluetooth proxy `btproxy-stenv`, likely reporting multiple logical scanners) but only 2 were
"connectable" at the time of the incident (§2.2), and neither had a free slot for this MAC. Several
other BLE devices share this stack (Car Charger, Office Floor, Facade Lights, Stairs Upper per
config entries), all of which compete for the same limited connectable slots — ESPHome proxies
typically support only ~3 simultaneous GATT connections each. This is an environmental/capacity
constraint, not a bug in this integration, but the integration has no way to detect or gracefully
back off from it (it just surfaces the raw exception through `ConfigEntryNotReady`).

### 3.3 CONFIRMED (code): All-or-nothing setup with no availability-based recovery

`async_setup_entry` (`__init__.py:56-288`) wraps the *entire* sequence — connect, pair, read
factory-reset id, read model/hw/sw version, write currency, register services, forward platform
setups — in a single `try/except Exception` that always raises `ConfigEntryNotReady` on any
failure (line 286-288). This means:

- A single transient BLE hiccup during setup tears down the entire integration (all ~31 entities),
  not just the one failing operation.
- Recovery relies entirely on Home Assistant's built-in exponential backoff for
  `ConfigEntryNotReady`, capped at the ~10 minute interval visible in every log the user has ever
  seen from this integration — there is no shorter/smarter retry once the device reappears.
- The integration never calls `bluetooth.async_register_callback()` to get an event-driven signal
  the moment HA's Bluetooth manager sees a fresh advertisement for this MAC; it only finds out on
  its next fixed-interval retry, which can be up to ~10 minutes after the device is actually back.

### 3.4 CONTRIBUTING FACTOR (live evidence, unconfirmed as root cause): marginal signal strength

RSSI -80 dBm (§2.4) for this specific device is weak enough that it plausibly makes this device the
most sensitive one in the household to any transient scanner/proxy contention — other BLE devices
on the same stack with stronger signal would likely tolerate a busy proxy slot better than one
already at the edge of range. This does not explain the multi-hour total silence on its own, but it
is a believable amplifier: a marginal device is the one most likely to fall out of every scanner's
advertisement cache and stay out once something disturbs the connection (per §3.1/§3.2), since
proxies/adapter don't need to be "very unlucky" to miss its next advertisement.

---

## 4. What this does *not* explain (open question)

Live logs confirm the device stopped advertising to *any* scanner from ~20:58:47 onward for at
least ~17 hours. Static code/log analysis from the HA side cannot fully explain why a BLE
peripheral would stop advertising for that long — plausible device-side explanations (not
verifiable from the HA host alone) include: the thermostat's own radio/firmware entering a stuck
"believes it's still connected" state after the aborted connection attempts in §2.2 (a known
failure mode for some BLE peripherals when a central aborts mid-handshake), or a physical/power
issue on the thermostat itself. Confirming this would require checking the physical device
directly (power-cycle test) — out of scope for this remote, read-only investigation.

---

## 5. Suggested direction for follow-up (not implemented yet)

Recorded here for reference only — no code changes have been made. In rough priority order once
this document is reviewed:

1. Introduce a single `asyncio.Lock` (or a proper `DataUpdateCoordinator`) around *all* GATT
   operations, not just `connect()`, so a mid-cycle disconnect fails once instead of cascading
   across every entity on the same poll tick (§3.1).
2. Make every `read_*`/`write_*` method call `ensure_connection()` consistently (or centralize
   through the coordinator), instead of only the `read_split_characteristic` path doing so, so any
   entity's poll can self-heal a dropped connection.
3. Split `async_setup_entry`'s all-in-one try/except so a transient failure doesn't force a full
   teardown/rebuild of all 31 entities, and consider `bluetooth.async_register_callback()` to react
   to the device reappearing instead of waiting for the next fixed-interval retry (§3.3).
4. Reduce per-cycle BLE traffic — e.g. `EnstoPowerConsumptionSensor` currently reads both
   `read_power_consumption()` and the ~887-byte `read_monitoring_data()` independently every 30s;
   `EnstoDateTimeSensor` and `EnstoCurrentPowerSensor` also bypass the shared real-time cache.
   Lowering traffic reduces the chance of the concurrent-access failures in §3.1.

Item 5 (device-side stuck-advertising state, §4) is not something integration code can fix, but a
graceful reconnect/backoff strategy (items 1–3) would prevent the integration's own polling
architecture from being the thing that triggers it in the first place.
