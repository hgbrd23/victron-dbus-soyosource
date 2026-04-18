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
2. **Inverter on D-Bus** — Publish the Soyosource as `com.victronenergy.inverter.soyosource_<N>`
   so it appears in the GUIv2 "Inverter/Charger" tile (and in VRM). We use the
   `inverter` service type, not `pvinverter`, because the Soyosource is a
   battery → AC grid-tie inverter (not solar). This was the main reason it was
   showing up as "Essential Loads" in early testing — wrong service type.

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
  `com.victronenergy.inverter.soyosource_<N>`.
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
  service handles the missing telemetry gracefully (Dc/* and Temperature stay
  empty on D-Bus).
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

- **GUIv2 Inverter/Charger tile shows state, not power, for `inverter` service
  type.** That tile is purpose-built for Multiplus/Quattro (`com.victronenergy.
  vebus`). For our simpler battery→AC inverter, the tile correctly identifies
  the state ("Inverting") but the wattage is only on the detail page. The
  power *is* aggregated by systemcalc (`Dc/InverterCharger/Power`) and visible
  in the Battery tile (-Wh discharge) and on the device list page.

- **Don't register as `vebus` to get power on the tile.** Tested and reverted:
  `dbus-systemcalc-py/delegates/dvcc.py` finds the vebus service, treats it as
  a Multi, and crashes with `TypeError: '<' not supported between instances of
  'str' and 'int'` while comparing our `/FirmwareVersion='unknown'` (string) to
  `VEBUS_FIRMWARE_REQUIRED` (int). The crashloop in systemcalc takes
  `com.victronenergy.system` off the bus entirely — every other service that
  reads from it then fails. A numeric `/FirmwareVersion` would clear that
  specific check, but `vebus` triggers many more code paths that assume a real
  Multi (BMS handshake, DVCC charge control, ESS hub-4, etc.) — every one is a
  potential landmine. Stick with `inverter`.

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
