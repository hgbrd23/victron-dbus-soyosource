# victron-dbus-soyosource

> **Keep this file up to date.** Whenever you make a non-trivial change to the
> architecture, control-loop behaviour, D-Bus surface, config schema,
> install layout, or hit a new VenusOS/Victron gotcha, update the relevant
> section of this file in the same change — don't wait for the user to ask.
> Think of it as the long-lived design log: future-you (or another agent in a
> fresh session) will rely on it to avoid re-learning things the hard way.

## Project Purpose

Bridge between Victron VenusOS (GX device) and Soyosource GTN PV inverter(s) via RS-485.
The service runs on VenusOS, reads grid power demand from VenusOS D-Bus, and sends
power demand commands to the Soyosource inverter over RS-485 using the virtual meter protocol.
This replaces the original Soyosource power meter — the VenusOS device already has
accurate grid power readings from its own energy meter.

## Architecture

```
VenusOS D-Bus (grid meter readings)
        |
        v
  dbus-soyosource.py  (this service)
        |
        v
  RS-485 (4800 baud, 8N1) --> Soyosource GTN Inverter
```

The service has two roles:
1. **Virtual Meter** — Send power demand frames to the Soyosource inverter over RS-485
2. **Inverter on D-Bus** — Publish the Soyosource as `com.victronenergy.vebus.soyosource_<N>`
   (Multi emulation, default). Topologically a Soyosource in grid-feedback mode
   behaves identically to a Multiplus in ESS grid-export mode: DC → inverter →
   out the AC-input terminal → onto the grid bus. By publishing
   `/Ac/ActiveIn/L1/P = -last_demand` (negative = pushing OUT) and
   `/Ac/Out/<L>/P = 0` (no essential-loads bus), systemcalc's
   `ConsumptionOnInput[Lx] = Grid[Lx] − ActiveIn[Lx]` computation produces the
   correct L1 load. This also lights up the native "Inverter / Charger" tile
   with state "Inverting" and keeps our production OUT of "Solar yield"
   (which was the cost of the previous pvinverter attempt). The landmine
   mitigations required to make vebus safe are documented in the gotchas
   below — specifically `/FirmwareVersion` as int, `/Hub4/*` no-op stubs,
   `/Bms/AllowTo{Charge,Discharge} = None`, and `ProductId = 0xA144`.

   Two alternative `ServiceType` values remain supported:
   - `pvinverter` — same accounting math via `Position=0` aggregation, but
     the dashboard shows us under "Solar yield". Use this if a real Multiplus
     is also present (we'd collide on `/VebusService` otherwise).
   - `inverter` — legacy; double-counts our output as "Essential Loads" in
     Total Consumption. Keep the option in the switch for forward
     compatibility / debugging, but don't recommend it.

## Soyosource RS-485 Protocol

Reference: https://github.com/syssi/esphome-soyosource-gtn-virtual-meter

### Communication Parameters
- Baud rate: 4800
- Data bits: 8
- Stop bits: 1
- Parity: None
- Inter-frame gap: 50ms

### Power Demand Frame (Host -> Inverter, 8 bytes)

```
Byte  Value       Description
0     0x24        Device address
1     0x56        Command (power demand)
2     0x00        Fixed
3     0x21        Fixed
4     MSB         Power high byte (watts / power_demand_divider)
5     LSB         Power low byte
6     0x80        Fixed
7     CHK         Checksum = (264 - byte[4] - byte[5]) & 0xFF
```

Example: 0W = `24 56 00 21 00 00 80 08`, 100W = `24 56 00 21 00 64 80 A4`

### Status Query Frame (Host -> Inverter, 8 bytes)

```
24 00 00 00 00 00 00 00
```

### Status Response (Inverter -> Host, 15 bytes)

```
Byte   Description
0-3    Header: 23 01 01 00
4      Operation status (0x00 = normal)
5-6    Battery voltage (x 0.1V, big-endian)
7-8    Battery current (x 0.1A, big-endian)
9-10   AC voltage (x 1V, big-endian)
11     AC frequency (x 0.5Hz)
12-13  Temperature raw (subtract 300, x 0.1 deg C, big-endian)
14     Checksum (not validated by ESPHome impl)
```

### Power Demand Calculation Modes
1. **DUMB_OEM_BEHAVIOR** — Simple threshold, default
2. **NEGATIVE_MEASUREMENTS_REQUIRED** — Accounts for grid export
3. **RESTART_ON_CROSSING_ZERO** — Resets when grid export detected

## Current State

Working implementation. Verified end-to-end on a Raspberry Pi 2 running VenusOS
with a 1200W Soyosource GTN inverter and a DSD TECH SH-U11F USB-RS485 adapter.

Modules:
- `soyosource.py` — pure protocol module (frame build/parse, checksum, demand calc).
  Validated against frames captured from the OEM power meter.
- `dbus-soyosource.py` — main service: poll grid power from D-Bus, calculate
  demand, send frames over RS-485, publish as
  `com.victronenergy.pvinverter.soyosource_<N>` at Position=0 (default).
  Supports three service types via `ServiceType` config: `pvinverter`
  (recommended — correct accounting), `inverter` (Mode dialog in GUIv2 but
  Essential-Loads double-count), `vebus` (landmines — see gotchas).
- `config.ini` — single source of truth for all runtime values (serial port,
  physical + tracked phase, control-loop tuning, Eco target, safety
  timeouts). Gitignored; users copy from `config.ini.example`.

### Config surface (quick reference)

- `Phase` — physical AC wiring (L1/L2/L3). Drives `/Settings/System/AcPhase`
  and which `/Ac/Out/<p>/*` paths carry values.
- `TrackPhase` — which grid-meter reading(s) the control loop follows.
  L1/L2/L3 = one phase; `ALL` = sum of L1+L2+L3 (skipping phases a
  single-phase meter doesn't report). Default `ALL` because EU utility meters
  net across phases for billing, so a single-phase inverter on L1 can offset
  loads on L2 and L3.
- `TargetGridW` — the reading we converge the tracked power towards.
  Positive = target import; negative = target export (e.g. `-10` aims to
  always give 10 W back to the grid so we never buy). Used to be called
  `BufferW` with a "safety margin" framing — renamed to reflect that it's
  literally "what should the meter read".

### Control loop

The service has three **Mode**s, writable via `/Mode` on the D-Bus service
(GUIv2's *Inverter mode* dialog sets this). Mode constants at the top of
`dbus-soyosource.py`:

- `MODE_ON` (2) — grid-following. Default on every service start.
- `MODE_ECO` (5) — send a constant `EcoPowerDemand` watts; grid meter is still
  polled for diagnostics but not used in the demand calc.
- `MODE_OFF` (4) — **transmit nothing** on RS-485. A short burst of 0 W frames
  is sent on the On→Off transition so the inverter drops to 0 immediately
  instead of waiting out its own ~10 s auto-off timeout.

Mode is **not persisted** — a restart always comes back up in `MODE_ON`. The
design choice: we had localsettings registration, bidirectional sync, and
first-run-only config defaults, but the complexity wasn't worth it for
values the user sets once. Config-only tuning with an ephemeral Mode switch
is the current API.

On-mode loop:

1. Every `UpdateIntervalSeconds` (default 1 s): poll the tracked grid power
   — one `/Ac/Grid/<P>/Power` path for a specific phase, or the sum of
   L1+L2+L3 for `TrackPhase=ALL`.
2. If the reading changed by ≥ 1 W since the last action, recalculate demand:
   `new_demand = last_demand + (grid - target) * damping`, clamped to
   `[min_demand, max_demand]` (or 0 if below min_demand). `target` is the
   `TargetGridW` config value.
3. Every `SendIntervalSeconds` (default 0.5 s): write the current demand frame
   on RS-485.
4. If the grid reading goes stale (no D-Bus updates for `GridStaleTimeoutSeconds`),
   force demand = 0 W.
5. On SIGTERM/SIGINT: send five 0 W frames before exiting.

Eco-mode loop: `last_demand = cfg.eco_power_demand` on every update tick;
TX tick sends that value. Grid-stale safety does not apply (Eco is an explicit
user override — they asked for fixed output).

Off-mode loop: `last_demand = 0`, TX tick returns without writing. The
serial-starter lock is still refreshed each tick so the port stays claimed.

The 1 W change threshold is important. The upstream grid meter sometimes holds
a reading for several seconds before refreshing; naive ramping during that gap
would overshoot dramatically once the new reading arrived. By skipping
recalculation on stale-looking ticks (and treating small float jitter as stale)
the loop stays stable.

### Diagnostics (heartbeat + drift detector)

Demand-change log lines alone don't show the steady-state wedge pattern we've
hit (inverter silently stops producing while we still command max). Two extra
streams cover that gap:

- **Heartbeat** — every 60 s of wall-clock, one INFO line:
  `heartbeat: mode=On demand=335W grid=-27.9W grid_age=0.0s tx=119/60s`
  `tx=N/60s` is the number of successful RS-485 writes in the window (expected
  ≈ 120 at SendIntervalSeconds=0.5s). A collapsing tx rate is the first
  signal of a port-side issue. `grid_age` is wall-clock since the last
  fresh D-Bus grid read.
- **Drift detector** — when `last_demand ≥ 100 W` but `grid ≥ target + 300 W`
  for 30 consecutive update ticks (≈ 30 s), one WARNING line. Cleared with
  an INFO line when grid drops below `target + 100 W` (hysteresis — the
  clear band is tighter than the warn band, so a fluctuating grid near the
  boundary doesn't flap the warning on and off). Inside the 100–300 W
  deadband the state is kept as-is, no log.  No auto-recovery action yet —
  this is pure observation, so we can characterise wedges without the log
  flushing itself. The 300 W tolerance is wide on purpose: household load
  spikes routinely push grid 100–200 W above target even when the inverter
  is fine.

When the inverter wedges and you need to investigate, the heartbeat sequence
around the transition + the DRIFT warning timestamp together pin down whether
the issue is "our frames stopped going out" (tx count drops) vs. "frames go
out, inverter ignores them" (tx count steady, grid stays high).

### Gotchas hit during development

- **VenusOS serial scanners must be told we own the port — via `lock_tty`.**
  `serial-starter.sh` runs a `while true` loop every 2 s, iterating
  `/dev/serial-starter/<tty>` symlinks and issuing `svc -o` for the next
  probe in the cycle (dbus-serialbattery, gps-dbus, vedirect-interface,
  dbus-cgwacs, dbus-modbus-client, dbus-fzsonick-48tl, dbus-imt-si-rs485tc).
  Each one briefly opens the tty and writes its own protocol's init bytes
  — fatal for our half-duplex bus. Things that **do not work**:
  `svc -d` on the scanners (main loop brings them back on the next tick);
  `exclusive=True` on our pyserial open alone (scanners can still be in
  flight when we start); killing the supervise processes (svscan respawns).
  The **right** fix is the same one Victron's own scanners use: create a
  symlink at `/var/lock/serial-starter/<tty>` — the main loop begins each
  iteration with `lock_tty $TTY || continue`, so while our lock exists the
  tty is skipped entirely. We re-create the symlink every TX tick in case
  an in-flight scanner's `trap cleanup EXIT` stripped it, and remove it on
  SIGTERM.
- **DSD TECH SH-U11F is full-duplex** (separate TXD+/TXD- and RXD+/RXD-) but
  A+/B- on the TXD pair carries both directions — the TX transceiver also
  receives. The FT232R's CBUS2 is configured as TXDEN in EEPROM, so direction
  switching is handled in hardware (works on Linux without extra config).
- **PropertiesChanged signals on systemcalc paths are unreliable** across
  VenusOS versions. Polling is safer and the cost is trivial.
- **2022 Soyosource purple mainboards don't answer status queries.** The
  service handles the missing telemetry gracefully — `/Temperature` stays
  empty; `/Dc/0/{Voltage,Current,Power}` are *estimated* (see next gotcha).

- **Cold-boot USB wedge: TX LED flashes, no bytes on the wire.** Reproducible
  on this setup (Pi 2, DSD TECH SH-U11F on a USB hub): after a VenusOS cold
  boot, pyserial opens fine and every `write()` returns success, but the
  inverter stays at 0 W and the battery doesn't discharge. Physical
  unplug+replug of the USB adapter recovers it reliably. The fix is to do
  the same thing in software: write the USB interface name (e.g.
  `1-1.3.1:1.0`) to `/sys/bus/usb/drivers/ftdi_sio/unbind`, wait 1 s, then
  the same name to `.../bind`. The tty comes back with the same name
  (serial-starter lock stays valid) and the by-id symlink also resolves to
  the same tty — our config doesn't need to know or care about the
  underlying tty number. `usb_reset_adapter()` implements the sequence;
  `_maybe_auto_reset()` triggers it once per hour when the battery monitor
  reports ≥ -50 W (i.e. not discharging) despite commanded demand ≥ 100 W,
  after an initial 30 s settling window at startup. Needs
  `/Dc/Battery/Power` on `com.victronenergy.system` (i.e. a battery monitor
  has to be present) — without it the wedge is only visible via the grid-
  based drift detector and the user has to intervene. Root cause of the
  wedge itself is unknown; likely either the FT232R's internal state after
  a warm-boot of the Pi or a USB enumeration race — either way the replug
  fixes it and we don't need to dig further.

- **Missing `/Dc/0/*` on vebus/inverter services inflates "DC Loads" in Total
  Consumption.** systemcalc computes `vebuspower = V * I` per vebus service
  (and `inverter_power += V_dc * I_dc` for non-vebus `inverter` services),
  then folds both into `/Dc/System/Power` via:
  `DcSystemPower = solar + charger + fuelcell + alt + vebus + inverter - battery`
  (sign convention: positive = DC loads consuming power; battery positive =
  charging; vebus/inverter positive = flowing INTO the DC bus, i.e. charging).
  When we're inverting and publish no `/Dc/0/*`, our `vebuspower` (or
  `inverter_power`) contribution is 0, so the battery's discharge gets
  attributed entirely to "DC Loads" — which then lands in Total Consumption
  *on top of* AC Loads, double-counting our output. Symptom on VRM: Total
  Consumption ≈ AC Loads + our commanded demand.
  Fix: always publish `/Dc/0/Voltage` (read from
  `com.victronenergy.system /Dc/Battery/Voltage`, fallback 52 V nominal if
  no battery monitor) and `/Dc/0/Current = -demand / (V × efficiency)` —
  negative because current flows OUT of the DC bus into our inverter.
  Efficiency baked in as `INVERTER_EFFICIENCY = 0.94` (Soyosource datasheet
  ~93–95%). If the inverter *does* answer status queries, we prefer the real
  V/I it reports, negating the current magnitude since the Soyosource reports
  unsigned while systemcalc expects signed.
- **Raspberry Pi 2 power supply**: under-voltage warnings in `dmesg` cause
  intermittent USB disconnects. Unrelated to the service but worth tracking.
- **D-Bus NameExistsException on quick restart**: when daemontools restarts the
  service faster than the previous Python process releases its bus name, the
  new instance throws `NameExistsException` on register. Solved by retrying
  registration with 1 s sleep, up to 20 s. Without this, a single race during
  startup crashlooped the service indefinitely. Also bitten once by a leftover
  zombie from manual testing — always `pkill` test instances before installing.

- **D-Bus proxy pinned to dead connection ID**: `bus.get_object(name, path)`
  resolves the well-known name to a unique connection ID (`:1.X`) at construction
  time. When the target service restarts, the new instance gets a new ID, and
  every subsequent call on the cached proxy fails with `ServiceUnknown: The
  name :1.X was not provided by any .service files`. Fixed by passing
  `follow_name_owner_changes=True` so dbus-python re-resolves on owner change.
  This bit us specifically when we crashed systemcalc with the vebus experiment
  (see below) — without the flag, even after systemcalc recovered we couldn't
  read grid power until the service restarted.

- **`inverter` service type double-counts as Essential Loads.** systemcalc
  treats `/Ac/Out/<L>/P` on an `inverter` service as "loads on the inverter's
  AC-output bus" and adds it to `ConsumptionOnOutput[Lx]`. That's correct for
  a Multiplus (which has a physically distinct AC-Out terminal with its own
  loads), wrong for us (our "AC Out" is the same grid bus the meter sits on,
  so household loads on L1 are already in the grid reading). Result: VRM
  "Total Consumption" = AC Loads + Essential Loads, where Essential Loads
  equals our commanded demand — inflating the number by the full inverter
  output. With the correct L1 load being `Grid[L1] + our_output` (e.g.
  `-147 + 343 = 196 W`), switching to `pvinverter` at `Position=0` gets the
  math right: our output is subtracted from grid in `ConsumptionOnInput[Lx]`
  instead of added to `ConsumptionOnOutput`. This is how AC-coupled Fronius
  setups work and what the ESPHome Soyosource integration uses.

- **`pvinverter` aggregation silently skips if `/Settings/SystemSetup/AcInput1`
  is missing.** systemcalc's `PvInverters.map_position()` (in
  `/opt/victronenergy/dbus-systemcalc-py/delegates/pvinverter.py`) maps our
  `Position=0` to `/Ac/PvOnGrid` *only if* localsettings has
  `/Settings/SystemSetup/AcInput1 ∈ {1=Grid, 2=Genset, 3=Shore}`. On systems
  that never had a Multiplus (like a Pi2 running a bare-minimum VenusOS),
  that setting never gets created — so `map_position()` returns `None`, our
  production isn't aggregated anywhere, and `ConsumptionOnInput[L1]` stays
  clamped to `max(0, Grid[L1])` (i.e. zero when exporting). Symptom:
  `/Ac/PvOnGrid/L1/Power = []` (dbus empty array) even though our service is
  discoverable and publishing `/Ac/L1/Power` correctly.
  Fix: `ensure_acinput1_is_grid()` runs at pvinverter-service startup and
  uses `com.victronenergy.Settings.AddSetting('SystemSetup', 'AcInput1', 1,
  'i', 0, 0)` — idempotent, so safe on systems where a real Multiplus
  previously set it to something else.

- **GUIv2 renders no Mode dialog for `pvinverter` services.** The On/Eco/Off
  switch is only on the Multi/Inverter device pages. With `pvinverter`, our
  writable `/Mode` path still accepts values — but from D-Bus / MQTT /
  Node-RED, not the native UI. Trade-off we accept for correct accounting.
  If the user wants the dialog back, switch `ServiceType = inverter` (cost:
  wrong consumption math) or add a second parallel `inverter` service with
  `/Ac/Out/<L>/P = 0` purely for the UI (hybrid approach — avoided in the
  single-service implementation for simplicity).

- **`vebus` is now the default, after taming the landmines.** First attempt
  crashed systemcalc because `dvcc.py` compared our `/FirmwareVersion='unknown'`
  (string) to `VEBUS_FIRMWARE_REQUIRED` (int) — `TypeError`, systemcalc
  crashloops, `com.victronenergy.system` drops off the bus and every dependent
  service starts failing. Second attempt (now live) addresses this and the
  adjacent assumptions:
  - `/FirmwareVersion = 469` (int, passes the dvcc compare).
  - `/Bms/AllowToCharge = /Bms/AllowToDischarge = None` — systemcalc's BMS
    delegate reads `None` and interprets "no vebus BMS", defers to the
    real battery service (JKBMS on SerialBattery). Avoids the BMS handshake
    dance with a Multi product ID.
  - `ProductId = 0xA144` (ours, not a known Multiplus ID) — nothing in
    Victron's BMS-integration product table matches, so the BMS
    integration-specific code stays dormant.
  - `/Hub4/L1..L3/AcPowerSetpoint`, `/Hub4/DisableCharge`, `/Hub4/DisableFeedIn`,
    `/Hub4/Sustain`, `/Hub4/DoNotFeedInOvervoltage`,
    `/Hub4/BatteryOvervoltageProtectionActivated` exposed as writable no-op
    stubs so ESS (`Hub4Mode=1` / BatteryLife) can write grid setpoints without
    errors. We log writes at DEBUG and otherwise ignore them — our own grid
    follower remains authoritative, and ESS's setpoint would converge to
    roughly the same value anyway on a no-Multi system.
  - `/Hub4/AssistantId = None` — systemcalc's SystemState delegate treats us
    as "no ESS assistant", avoiding the ExternalControl branch that expects
    Hub4Mode=3 coordination.
  - `/Ac/ActiveIn/ActiveInput = 0` (Input 1 live) instead of 240 (disconnected),
    because we want ConsumptionOnInput[L1] to be computed as
    `Grid[L1] - ActiveIn[L1]`. We publish `ActiveIn[L1] = -last_demand`
    (negative = pushing OUT of the AC input terminal, ESS-feedback style),
    which makes the subtraction work out to `Grid[L1] + demand` — the
    correct L1 load.
  - `/Ac/Out/<L>/P = 0` always — no essential-loads bus exists on our
    topology, and publishing nonzero here would double-count into
    `ConsumptionOnOutput[Lx]`.

  Verified live: systemcalc stays up, `/VebusService` elects us, `/Ac/PvOnGrid
  = []` (no Solar-yield pollution), `/Ac/ConsumptionOnInput[L1]` correctly
  reflects `Grid + production`, `/SystemState/State = 9` (Inverter/Charger
  tile reads "Inverting"). `/Dc/Vebus/Power = []` because our 2022 Soyosource
  doesn't answer status queries — doesn't hurt accounting, just leaves the
  DC-side of the Battery tile unfilled.

  Do NOT enable `vebus` alongside a real Multiplus on the same system —
  systemcalc elects `/VebusService` by lowest device instance, and behaviour
  becomes undefined if two vebus services compete. For that case, use
  `ServiceType = pvinverter`.

- **Stale device entries in GUIv2 after switching service type**: each
  `<service_type>.<custom>_<instance>` registration leaves a "remembered" entry
  in the GUIv2 device list, which keeps showing as "Not connected" once we
  stop publishing. The official cleanup is the *Settings → Devices → Remove
  disconnected devices* button. Some related leftovers also accumulate in
  localsettings (e.g. `Settings/SystemSetup/Batteries/Configuration/
  com_victronenergy_vebus/41/*` after a vebus run); those can be removed via
  the `RemoveSettings` D-Bus method on the parent path.

- **Custom settings are NOT shown on the GUIv2 device page.** GUIv2 only
  renders hardcoded paths (Mode, State, AC Out, DC, CustomName). We used to
  register `MaxPowerDemand`, `MinPowerDemand`, `BufferW`, `DampingPercent`
  with `com.victronenergy.settings` under `/Settings/Devices/soyosource_<N>/`
  and mirror them onto writable paths on the inverter service, so Node-RED /
  Home Assistant / dbus-spy clients got a clean per-device API and the values
  persisted across reboots. The round-trip (path write → localsettings →
  callback → mirror back) needed a `_suppress_sync` loop-break flag and a
  two-stage init, and in practice nobody adjusted these at runtime — they get
  set once, in config.ini, and left alone. Ripped out; the single source of
  truth is now `config.ini`, reloaded only on service restart. If a future
  use-case needs live-tunable values, add a fresh D-Bus path with a plain
  onchangecallback and skip localsettings altogether.

  Left behind on pre-existing installs: `/Settings/Devices/soyosource_<N>/{Max,Min,Buffer,Damping}Percent`
  entries in localsettings. Harmless, but can be cleaned with `RemoveSettings`
  on the parent path if you care. New installs don't create them.

- **USB re-enumeration scrambles `/dev/ttyUSB<N>` numbers.** Use a
  `/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_<serial>-if00-port0` path
  in `SerialPort`. Hit this after a firmware upgrade reboot: our adapter
  moved from `ttyUSB3` to `ttyUSB2`, and the hard-coded device number silently
  broke the service.

- **systemcalc returns empty `dbus.Array` during its own restarts.** On
  `/Ac/Grid/<L>/Power`, the value is a `dbus.Array([])` sentinel for a
  couple of seconds after systemcalc starts (while it's still discovering
  services). `float(dbus.Array([]))` raises `TypeError` and crashed the
  service. We now handle non-numeric reads as "no fresh value, retry next
  tick" without crashing.

- **Cached D-Bus proxy pinned to a dead connection ID.** `bus.get_object()`
  without `follow_name_owner_changes=True` resolves the well-known name
  (`com.victronenergy.system`) to a unique ID (`:1.X`) at construction
  time. After systemcalc crashes and restarts, the new instance gets a
  new ID — every call on the cached proxy then fails with `ServiceUnknown:
  The name :1.X was not provided by any .service files`. Pass
  `follow_name_owner_changes=True` so dbus-python re-resolves on owner
  change.

### Reference paths used

The inverter service follows the layout in
`/opt/victronenergy/dbus-systemcalc-py/scripts/dummyinverter.py` and the
service-type definition in `dbus_systemcalc.py` (search for
`com.victronenergy.inverter`). Key paths we publish:

- `/Mode` — writable. 2=On (default), 5=Eco (fixed `EcoPowerDemand` W), 4=Off (no TX).
- `/State` — 9 (inverting) when demand > 0; 0 (off) when demand == 0
- `/Ac/Out/<phase>/{V,I,P,F}` — AC output (only configured phase populated)
- `/Energy/InverterToAcOut` — running kWh counter
- `/Dc/0/{Voltage,Current,Power}` — only set if the inverter answers status

systemcalc consumes the AC output and reports it as `Dc/InverterCharger/Power`
(negated, since power flows from DC to AC). It also increments
`Timers/TimeOnInverter` while we're inverting.

## Tech Stack

- Python 3 on VenusOS (ARM)
- `pyserial` for RS-485 communication
- `dbus` + `gi.repository.GLib` for VenusOS D-Bus integration
- `VeDbusService` from `/opt/victronenergy/dbus-systemcalc-py/ext/velib_python`

## Development Notes

- VenusOS runs on a Victron GX device (e.g., Cerbo GX) — ARM Linux
- Target deployment: USB-to-RS485 adapter on the GX device (typically `/dev/ttyUSBX`)
- The service registers as `com.victronenergy.pvinverter` on D-Bus
- Update interval: 0.5 seconds for sending power demand frames
- Safety: Soyosource automatically switches to 0W if no frame received for a while
