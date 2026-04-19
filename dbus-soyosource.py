#!/usr/bin/env python3
"""
dbus-soyosource — VenusOS service that drives a Soyosource GTN inverter
over RS-485 using grid power readings from the VenusOS D-Bus.

Two roles:
  1. Virtual meter — sends power demand frames to the inverter every N seconds.
  2. PV inverter on D-Bus — publishes the commanded output power (and, where
     available, the inverter's status response) so it shows up in VRM.
"""

import configparser
import logging
import os
import platform
import signal
import sys
import time

import dbus
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib
import serial

# VenusOS velib_python
sys.path.insert(1, '/opt/victronenergy/dbus-systemcalc-py/ext/velib_python')
from vedbus import VeDbusService  # noqa: E402

import soyosource  # noqa: E402


log = logging.getLogger('dbus-soyosource')


# /Mode values published on the inverter service. GUIv2's Inverter-mode
# dialog writes one of these. On startup we default to MODE_ON — a service
# restart always comes back up inverting, matching user expectation.
MODE_ON = 2    # normal grid-following control loop
MODE_OFF = 4   # silent: no RS-485 frames; inverter auto-offs after its own timeout
MODE_ECO = 5   # fixed-watts mode: send cfg.eco_power_demand regardless of grid reading

VALID_MODES = (MODE_ON, MODE_OFF, MODE_ECO)

_MODE_NAMES = {MODE_ON: 'On', MODE_OFF: 'Off', MODE_ECO: 'Eco'}

# Soyosource DC→AC conversion efficiency. Datasheets quote ~93–95% under load.
# Used to estimate /Dc/0/Current when the inverter doesn't answer status queries
# (2022 purple-board mainboards are silent). Without this estimate, systemcalc
# has no idea we're pulling power from the DC bus and mis-attributes the whole
# battery discharge to "DC Loads", inflating Total Consumption.
INVERTER_EFFICIENCY = 0.94

# Fallback battery voltage when /Dc/Battery/Voltage isn't published on
# com.victronenergy.system (e.g. no battery monitor configured). Typical for a
# 48V LFP pack at rest. Only used to keep the DC estimate non-zero; if the real
# voltage is wildly different, Total-Consumption accounting will still be close
# enough and the drift is bounded by the efficiency factor anyway.
FALLBACK_BATTERY_VOLTAGE = 52.0


def _mode_name(mode):
    return _MODE_NAMES.get(mode, 'Mode(%d)' % mode)


# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------

class Config:
    def __init__(self, path):
        cp = configparser.ConfigParser()
        cp.read(path)
        s = cp['DEFAULT']

        self.serial_port = s.get('SerialPort', '/dev/ttyUSB3')
        # Physical wiring: which phase the Soyosource feeds into. Used for
        # /Settings/System/AcPhase and the /Ac/Out/<phase>/* paths.
        self.phase = s.get('Phase', 'L1').upper()
        # Which grid-meter reading(s) the control loop follows.
        #   L1/L2/L3 — track one phase only.
        #   ALL      — track the sum across L1+L2+L3 (good for phase-netted
        #              utility billing: one single-phase inverter on L1 can
        #              offset loads on all three phases).
        self.track_phase = s.get('TrackPhase', 'ALL').upper()
        self.device_instance = int(s.get('DeviceInstance', '41'))
        self.custom_name = s.get('CustomName', 'Soyosource GTN')
        self.ac_position = int(s.get('AcPosition', '1'))

        # D-Bus service type. Three options for a Soyosource:
        #   "pvinverter" — registers as com.victronenergy.pvinverter.<...> at
        #                  Position=0 (grid bus). Correct Total-Consumption
        #                  accounting: our output subtracts from grid import
        #                  in ConsumptionOnInput, no double-count. Default and
        #                  recommended. Labelled as "PV" on the dashboard
        #                  (cosmetic — we're battery-sourced).
        #   "inverter"   — registers as com.victronenergy.inverter.<...>.
        #                  GUIv2 shows the Mode switch (On/Eco/Off) on the
        #                  device page, but our output gets counted as
        #                  "Essential Loads", double-counting Total Consumption.
        #   "vebus"      — registers as com.victronenergy.vebus.<...>. Would
        #                  give both correct accounting AND the Mode switch,
        #                  but triggers DVCC/ESS/BMS delegates that assume a
        #                  real Multiplus and crash. See CLAUDE.md.
        self.service_type = s.get('ServiceType', 'pvinverter').strip().lower()

        self.update_interval_s = float(s.get('UpdateIntervalSeconds', '1.0'))
        self.send_interval_s = float(s.get('SendIntervalSeconds', '0.5'))

        self.min_power_demand = int(s.get('MinPowerDemand', '0'))
        self.max_power_demand = int(s.get('MaxPowerDemand', '900'))
        # What reading we steer the tracked grid power towards.
        #   Positive — aim to always import that many watts (safety margin).
        #   Negative — aim to always export that many watts (overshoot by a
        #              small amount, e.g. -10 W).
        #   Zero     — aim for exact zero-export (risks brief export spikes).
        self.target_grid_w = int(s.get('TargetGridW', '-10'))
        self.damping = float(s.get('Damping', '0.7'))
        self.eco_power_demand = int(s.get('EcoPowerDemand', '360'))

        # If grid reading is older than this, we send 0W for safety.
        self.stale_timeout_s = float(s.get('GridStaleTimeoutSeconds', '10'))

        self.log_level = s.get('Logging', 'INFO')


# -----------------------------------------------------------------------------
# Serial
# -----------------------------------------------------------------------------

SERIAL_STARTER_LOCK_DIR = '/var/lock/serial-starter'


def _tty_for(port_path):
    """Resolve a /dev/serial/by-id/... (or /dev/ttyUSB<N>) to the tty basename."""
    real = os.path.realpath(port_path)
    return os.path.basename(real)


def acquire_serial_starter_lock(port_path):
    """
    Claim the port the same way Victron's own scanners do.

    `/opt/victronenergy/serial-starter/functions.sh` defines `lock_tty` as:

        ln -s $$ /var/lock/serial-starter/<tty>   # returns 0 on success

    and `serial-starter.sh`'s main loop begins each tty iteration with
    `lock_tty $TTY || continue`. The symlink is effectively a mutex. While
    it exists, serial-starter skips the tty — no probe cycle, no
    `svc -o dbus-cgwacs/dbus-serialbattery/gps-dbus/...`. Successful
    scanners keep it for their entire lifetime (their run-service.sh sets
    `trap cleanup EXIT` which unlinks it on death).

    We do exactly the same: take the lock on startup, release on clean
    shutdown, and re-acquire if something stripped it (e.g. a race with an
    already-in-flight scanner's trap).
    """
    try:
        tty = _tty_for(port_path)
        if not tty.startswith('ttyUSB'):
            return None
        os.makedirs(SERIAL_STARTER_LOCK_DIR, exist_ok=True)
        lock_path = os.path.join(SERIAL_STARTER_LOCK_DIR, tty)

        pid_str = str(os.getpid())
        try:
            os.symlink(pid_str, lock_path)
            log.info("Acquired serial-starter lock %s -> %s", lock_path, pid_str)
        except FileExistsError:
            try:
                current = os.readlink(lock_path)
            except OSError:
                current = '?'
            log.info("serial-starter lock %s held by PID %s — taking over",
                     lock_path, current)
            os.remove(lock_path)
            os.symlink(pid_str, lock_path)
        return lock_path
    except OSError as e:
        log.warning("Could not acquire serial-starter lock for %s: %s", port_path, e)
        return None


def ensure_serial_starter_lock(lock_path):
    """
    Re-create the lock if a scanner's `trap cleanup EXIT` removed it.

    An in-flight scanner (started before we took the lock) will run its
    unlock_tty on exit, which removes whatever symlink is there — including
    ours. Called from the TX tick; idempotent and cheap (a single
    os.symlink syscall per tick).
    """
    if not lock_path:
        return
    try:
        if not os.path.lexists(lock_path):
            os.symlink(str(os.getpid()), lock_path)
            log.info("Re-acquired serial-starter lock %s (a scanner's cleanup removed it)",
                     lock_path)
    except OSError as e:
        log.debug("Could not re-acquire serial-starter lock %s: %s", lock_path, e)


def release_serial_starter_lock(lock_path):
    """Called on clean shutdown so serial-starter can resume normal management."""
    if not lock_path:
        return
    try:
        # Only remove if we still own it (symlink target == our PID).
        if os.readlink(lock_path) == str(os.getpid()):
            os.remove(lock_path)
            log.info("Released serial-starter lock %s", lock_path)
    except OSError:
        pass


class SerialLink:
    """
    Thin wrapper around pyserial with reconnect-on-error.

    The VenusOS USB hub can glitch under load. We swallow transient errors and
    reopen the port on the next write.
    """

    def __init__(self, port):
        self.port = port
        self._ser = None
        # Same locking convention Victron's own scanners use. Keeps
        # serial-starter from launching probe services on our tty while
        # we're running. See acquire_serial_starter_lock() docstring.
        self.lock_path = acquire_serial_starter_lock(port)

    def refresh_lock(self):
        """Re-acquire the serial-starter lock if something removed it. Cheap."""
        ensure_serial_starter_lock(self.lock_path)

    def release_lock(self):
        release_serial_starter_lock(self.lock_path)

    def _open(self):
        # exclusive=True asks the kernel to enforce TIOCEXCL — once we have
        # the port open, other processes that try to open() it will fail with
        # EBUSY. Critical on VenusOS, which auto-launches a swarm of probe
        # services (dbus-serialbattery, gps-dbus, dbus-cgwacs, etc.) for every
        # ttyUSB*. Without this lock, those probes briefly open the port,
        # write nonsense, and corrupt our half-duplex bus.
        self._ser = serial.Serial(
            self.port,
            soyosource.BAUD_RATE,
            bytesize=soyosource.BYTE_SIZE,
            parity=soyosource.PARITY,
            stopbits=soyosource.STOP_BITS,
            timeout=0.1,
            exclusive=True,
        )

        # Reset lingering state from whoever had the port last. Specifically
        # defends against a serial-starter probe that slipped in during our
        # restart gap and left the tty in a wedged state (seen in the wild:
        # writes kept "succeeding" at the Python/pyserial layer but bytes
        # never reached the wire; only a full VenusOS reboot recovered). None
        # of these are strictly necessary if we had a clean port, but they're
        # all cheap and idempotent.
        try:
            self._ser.break_condition = False      # clear TIOCSBRK if held
            self._ser.reset_input_buffer()          # flush kernel RX buffer
            self._ser.reset_output_buffer()         # flush kernel TX buffer
            self._ser.dtr = True                    # known line state
            self._ser.rts = True
        except (serial.SerialException, OSError) as e:
            log.warning("Port state reset failed (non-fatal): %s", e)

        log.info("Opened serial port %s (exclusive lock held, state reset)", self.port)

    def write(self, data: bytes) -> bool:
        try:
            if self._ser is None:
                self._open()
            self._ser.write(data)
            self._ser.flush()
            return True
        except (serial.SerialException, OSError) as e:
            log.warning("Serial write failed: %s — will reopen next cycle", e)
            self.close()
            return False

    def read_available(self, max_bytes=64) -> bytes:
        try:
            if self._ser is None:
                return b''
            return self._ser.read(max_bytes)
        except (serial.SerialException, OSError) as e:
            log.warning("Serial read failed: %s — will reopen next cycle", e)
            self.close()
            return b''

    def close(self):
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None


# -----------------------------------------------------------------------------
# Grid power subscription
# -----------------------------------------------------------------------------

class GridReader:
    """
    Polls com.victronenergy.system for the tracked grid power.

    track_phase selects which path(s) to read:
      L1 / L2 / L3 — a single /Ac/Grid/<P>/Power path.
      ALL          — sum of /Ac/Grid/L1/Power + L2 + L3. Useful when the
                     utility nets all three phases for billing, so a
                     single-phase inverter on L1 can still offset loads
                     on L2 and L3.

    We poll instead of subscribing because PropertiesChanged signals on
    systemcalc paths aren't reliable across VenusOS versions, and silent
    failure here would be dangerous.
    """

    SYSTEM_SERVICE = 'com.victronenergy.system'

    PHASE_PATHS = {
        'L1':  ('/Ac/Grid/L1/Power',),
        'L2':  ('/Ac/Grid/L2/Power',),
        'L3':  ('/Ac/Grid/L3/Power',),
        'ALL': ('/Ac/Grid/L1/Power', '/Ac/Grid/L2/Power', '/Ac/Grid/L3/Power'),
    }

    # Threshold below which two consecutive readings are considered "the same"
    # (meter sensor noise). Prevents float jitter from masquerading as a fresh
    # reading when the meter hasn't actually updated.
    NOISE_THRESHOLD_W = 1.0

    def __init__(self, bus, track_phase):
        self.bus = bus
        self.track_phase = track_phase.upper()
        if self.track_phase not in self.PHASE_PATHS:
            raise ValueError("Unknown TrackPhase %r (expected L1/L2/L3/ALL)" % track_phase)
        self.paths = self.PHASE_PATHS[self.track_phase]

        self.last_power = None
        self.last_update = 0.0
        self._baseline = None
        self._last_error_logged = None

        # Cache proxies with follow_name_owner_changes=True so a systemcalc
        # restart (which changes the unique connection ID :1.X) doesn't strand
        # our reads on a dead connection.
        self._proxies = {}
        for p in self.paths:
            self._get_proxy(p)

        self.poll()  # initial read

    def _get_proxy(self, path):
        try:
            proxy = self.bus.get_object(
                self.SYSTEM_SERVICE, path,
                follow_name_owner_changes=True)
        except dbus.DBusException as e:
            log.error("Cannot reach %s%s: %s", self.SYSTEM_SERVICE, path, e)
            proxy = None
        self._proxies[path] = proxy
        return proxy

    def _read_one(self, path):
        """Read a single path. Returns float on success, None if unavailable.
        Empty dbus.Array (systemcalc 'no value' sentinel) and missing phases on
        single-phase meters both map to None."""
        proxy = self._proxies.get(path) or self._get_proxy(path)
        if proxy is None:
            return None
        try:
            raw = proxy.GetValue(dbus_interface='com.victronenergy.BusItem')
        except dbus.DBusException:
            self._proxies[path] = None  # force re-resolve next tick
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    def poll(self):
        """
        Read the tracked grid power (single path or sum of three).

        Returns True if the reading changed meaningfully (> NOISE_THRESHOLD_W)
        since the last update, False otherwise. The main loop uses this to
        avoid chasing stale readings while the upstream meter is slow to
        refresh.

        If NO paths return a numeric value, last_power is left untouched and
        is_stale() will trip after GridStaleTimeoutSeconds — that's our safety
        net. In ALL mode, a subset of paths returning None (e.g. L2/L3 missing
        on a single-phase meter) is fine — we sum whatever we got.
        """
        vals = [self._read_one(p) for p in self.paths]
        good = [v for v in vals if v is not None]
        if not good:
            self._log_error_once('read',
                "No grid power reading on %s — will retry" % (','.join(self.paths)))
            return False

        val = sum(good)

        # Log recovery on first success after a failure run.
        if self._last_error_logged is not None:
            log.info("Grid power read recovered: %.1f W (sum of %d phase%s)",
                     val, len(good), '' if len(good) == 1 else 's')
            self._last_error_logged = None

        self.last_power = val
        self.last_update = time.time()

        if self._baseline is None or abs(val - self._baseline) >= self.NOISE_THRESHOLD_W:
            self._baseline = val
            return True
        return False

    def is_stale(self, timeout_s):
        return (time.time() - self.last_update) > timeout_s

    def _log_error_once(self, kind, message):
        """Log a warning only when the error category changes, to keep
        noise down during long systemcalc outages."""
        if self._last_error_logged != kind:
            log.warning(message)
            self._last_error_logged = kind


# -----------------------------------------------------------------------------
# D-Bus inverter service
# -----------------------------------------------------------------------------
#
# We register as `com.victronenergy.inverter` (a simple DC→AC inverter), not
# `pvinverter`. The Soyosource is a grid-tie inverter that pulls from a battery
# and feeds AC into the household — that maps cleanly onto the "Inverter/
# Charger" tile in GUIv2 (it doesn't actually charge, it just inverts).
#
# Path conventions follow `dbus-systemcalc-py/scripts/dummyinverter.py` and
# the service-type definition in `dbus_systemcalc.py` (`com.victronenergy.
# inverter` block around line 137).
#
# /Mode and /State semantics:
#   /Mode  — writable. MODE_ON (2) = normal grid-following,
#            MODE_ECO (5) = fixed cfg.eco_power_demand watts,
#            MODE_OFF (4) = no RS-485 frames at all.
#            Writing /Mode from GUIv2's Inverter-mode dialog flips the runtime
#            behaviour immediately. The value is NOT persisted — a service
#            restart comes up in MODE_ON.
#   /State = 9 (inverting) when last_demand > 0, 0 (off) otherwise.
#
# DC-side fields (/Dc/0/Voltage, /Dc/0/Current) are populated only if the
# inverter answers status queries. Some Soyosource mainboards don't respond,
# in which case those paths stay empty.
#
# Tuning values (MaxPowerDemand, MinPowerDemand, BufferW, Damping,
# EcoPowerDemand) are loaded from config.ini at startup and are NOT exposed
# on D-Bus — change them in config.ini and restart the service.

def _register_with_retry(svc, service_name):
    """Register the service, retrying for ~20s if a previous instance still holds the bus name."""
    last_err = None
    for attempt in range(1, 21):
        try:
            svc.register()
            log.info("Registered D-Bus service %s", service_name)
            return svc
        except dbus.exceptions.NameExistsException as e:
            last_err = e
            if attempt == 1:
                log.warning("Bus name %s already taken — waiting for previous owner to release it", service_name)
            time.sleep(1)
    log.error("Could not register %s after %d attempts: %s", service_name, attempt, last_err)
    raise last_err


PHASE_TO_INT = {'L1': 0, 'L2': 1, 'L3': 2}


def _add_management_paths(svc, cfg, version_suffix='', firmware_version='unknown'):
    svc.add_path('/Mgmt/ProcessName', __file__)
    svc.add_path('/Mgmt/ProcessVersion',
                 'dbus-soyosource 0.1%s on Python %s' % (version_suffix, platform.python_version()))
    svc.add_path('/Mgmt/Connection', 'RS-485 on %s' % cfg.serial_port)
    svc.add_path('/DeviceInstance', cfg.device_instance)
    svc.add_path('/ProductId', 0xA144)
    svc.add_path('/ProductName', 'Soyosource GTN')
    svc.add_path('/CustomName', cfg.custom_name, writeable=True)
    # vebus needs int here (systemcalc's dvcc delegate compares against
    # VEBUS_FIRMWARE_REQUIRED, a string 'unknown' < int raises TypeError and
    # crashes systemcalc). For pvinverter/inverter service types the default
    # string is fine.
    svc.add_path('/FirmwareVersion', firmware_version)
    svc.add_path('/HardwareVersion', 'unknown')
    svc.add_path('/Serial', 'soyosource-%d' % cfg.device_instance)
    svc.add_path('/Connected', 1)

    # Which phase this inverter is wired to. Read by InverterData.qml and the
    # GUIv2 device pages; values are 0/1/2 for L1/L2/L3. Read-only here —
    # changing the phase needs both a config edit AND physical re-wiring.
    phase_int = PHASE_TO_INT.get(cfg.phase.upper(), 0)
    svc.add_path('/Settings/System/AcPhase', phase_int,
                 gettextcallback=lambda p, v: 'L%d' % (v + 1))


def _add_mode_path(svc, mode_callback):
    """Writable /Mode path. mode_callback(new_mode_int) -> bool accepts/rejects the write."""
    svc.add_path(
        '/Mode', MODE_ON, writeable=True,
        onchangecallback=lambda p, v: mode_callback(v),
    )


def create_vebus_service(bus, cfg, mode_callback):
    """
    Register as com.victronenergy.vebus.<...> — "Multi emulation".

    Why: it's the only service type that simultaneously gives correct
    Total-Consumption math (via /Ac/ActiveIn/<L>/P, which systemcalc subtracts
    from ConsumptionOnInput) AND the GUIv2 Inverter/Charger tile with a native
    Mode dialog AND avoids the "Solar yield" mis-labelling of pvinverter.

    How it works: we claim the Soyosource is feeding power OUT our AC-input
    terminal (negative /Ac/ActiveIn/L1/P), i.e. acting exactly like a
    Multiplus in ESS grid-feedback mode. /Ac/Out/<L>/P stays 0 because our
    "inverter output" is the same wire as our "inverter input" — no separate
    essential-loads bus exists.

    Landmine mitigations:
    - /FirmwareVersion published as int (469) — satisfies the dvcc delegate's
      VEBUS_FIRMWARE_REQUIRED comparison that crashed us last time.
    - /Hub4/* paths exposed as writable no-ops so ESS (hub-4) doesn't error
      when it tries to write grid setpoints to our non-existent Multi.
    - /Bms/AllowTo{Charge,Discharge} = None → BMS delegate treats us as
      "no vebus BMS", defers to the real BMS on the battery service.
    - ProductId kept at 0xA144 (our own) — doesn't match the Multi-BMS
      integration table.
    """
    ensure_acinput1_is_grid(bus)

    service_name = 'com.victronenergy.vebus.soyosource_%d' % cfg.device_instance
    svc = VeDbusService(service_name, bus=bus, register=False)

    _add_management_paths(svc, cfg, ' (vebus)', firmware_version=469)

    _add_mode_path(svc, mode_callback)

    # State: 0=off, 9=inverting. Mirrored on VebusMainState (same values).
    svc.add_path('/State', 0)
    svc.add_path('/VebusMainState', 0)
    svc.add_path('/VebusChargeState', 0)    # we never charge
    svc.add_path('/IsInverterCharger', 1)

    # AC Input: 0 = Input 1 live (grid). The _publish loop writes per-phase
    # power as -last_demand on our wired phase — negative = pushing OUT of
    # the AC-input terminal back to grid. systemcalc computes
    # ConsumptionOnInput[Lx] = Grid[Lx] - ActiveIn[Lx], so a negative
    # ActiveIn increases ConsumptionOnInput by our production (exactly the
    # value that was hiding when grid went negative under pvinverter).
    svc.add_path('/Ac/ActiveIn/ActiveInput', 0)
    svc.add_path('/Ac/NumberOfAcInputs', 1)

    for p in ('L1', 'L2', 'L3'):
        on_phase = (p == cfg.phase)
        # AC Input — populated on our wired phase, zero on the others (we
        # don't see those phases at the adapter).
        svc.add_path('/Ac/ActiveIn/%s/P' % p, 0.0 if on_phase else None,
                     gettextcallback=lambda p, v: '%.0fW' % v)
        svc.add_path('/Ac/ActiveIn/%s/I' % p, 0.0 if on_phase else None,
                     gettextcallback=lambda p, v: '%.2fA' % v)
        svc.add_path('/Ac/ActiveIn/%s/V' % p, 230.0 if on_phase else None,
                     gettextcallback=lambda p, v: '%.0fV' % v)
        svc.add_path('/Ac/ActiveIn/%s/F' % p, 50.0 if on_phase else None,
                     gettextcallback=lambda p, v: '%.1fHz' % v)
        # AC Output — always 0 on all phases. No essential-loads bus exists.
        svc.add_path('/Ac/Out/%s/P' % p, 0.0 if on_phase else None,
                     gettextcallback=lambda p, v: '%.0fW' % v)
        svc.add_path('/Ac/Out/%s/V' % p, 230.0 if on_phase else None,
                     gettextcallback=lambda p, v: '%.0fV' % v)
        svc.add_path('/Ac/Out/%s/I' % p, 0.0 if on_phase else None,
                     gettextcallback=lambda p, v: '%.2fA' % v)
        svc.add_path('/Ac/Out/%s/F' % p, 50.0 if on_phase else None,
                     gettextcallback=lambda p, v: '%.1fHz' % v)

    # DC side (only populated if the inverter answers status queries).
    svc.add_path('/Dc/0/Voltage', None, gettextcallback=lambda p, v: '%.1fV' % v)
    svc.add_path('/Dc/0/Current', None, gettextcallback=lambda p, v: '%.1fA' % v)
    svc.add_path('/Dc/0/Power', None, gettextcallback=lambda p, v: '%.0fW' % v)
    svc.add_path('/Soc', None, gettextcallback=lambda p, v: '%.0f%%' % v)

    # Energy counters.
    svc.add_path('/Energy/InverterToAcIn1', 0.0,
                 gettextcallback=lambda p, v: '%.2fkWh' % v)
    svc.add_path('/Energy/AcIn1ToInverter', 0.0,
                 gettextcallback=lambda p, v: '%.2fkWh' % v)
    svc.add_path('/Energy/InverterToAcOut', 0.0,
                 gettextcallback=lambda p, v: '%.2fkWh' % v)
    svc.add_path('/Energy/AcOutToInverter', 0.0,
                 gettextcallback=lambda p, v: '%.2fkWh' % v)

    svc.add_path('/Temperature', None, gettextcallback=lambda p, v: '%.1fC' % v)

    # BMS stubs: None → systemcalc decides "no vebus BMS, defer to battery".
    svc.add_path('/Bms/AllowToCharge', None)
    svc.add_path('/Bms/AllowToDischarge', None)

    # Alarms: all zero.
    for a in ('LowVoltage', 'HighVoltage', 'LowTemperature', 'HighTemperature',
              'Overload', 'Ripple', 'LowVoltageAcOut', 'HighVoltageAcOut'):
        svc.add_path('/Alarms/%s' % a, 0)

    _add_hub4_stubs(svc)

    return _register_with_retry(svc, service_name)


def _add_hub4_stubs(svc):
    """
    Expose /Hub4/* as writable no-ops so ESS (hub-4 grid-setpoint mode) can
    write to them without errors. Our control loop ignores the setpoints —
    we drive demand from the grid meter directly. ESS's only effect is
    noise in the log if we want to inspect it.

    If the user ever enables a real Multi + ESS, they'd switch us back to
    pvinverter first (see CLAUDE.md).
    """
    def accept(path, value):
        log.debug("Hub4 write to %s = %r (ignored)", path, value)
        return True
    for path in ('/Hub4/L1/AcPowerSetpoint', '/Hub4/L2/AcPowerSetpoint',
                 '/Hub4/L3/AcPowerSetpoint', '/Hub4/DisableCharge',
                 '/Hub4/DisableFeedIn', '/Hub4/Sustain',
                 '/Hub4/DoNotFeedInOvervoltage',
                 '/Hub4/BatteryOvervoltageProtectionActivated'):
        svc.add_path(path, 0, writeable=True,
                     onchangecallback=lambda p, v: accept(p, v))
    # /Hub4/AssistantId: None → systemcalc's SystemState treats us as "no ESS
    # assistant installed on this Multi", avoids the ExternalControl branch.
    svc.add_path('/Hub4/AssistantId', None)


def create_inverter_service(bus, cfg, mode_callback):
    """
    Register as com.victronenergy.inverter.<...>. Semantically clean for a
    DC→AC inverter, but GUIv2 only shows the state ("Inverting") on the tile,
    not the power value. Use ServiceType=vebus instead if you want the wattage
    visible in the overview tile.
    """
    service_name = 'com.victronenergy.inverter.soyosource_%d' % cfg.device_instance
    svc = VeDbusService(service_name, bus=bus, register=False)

    _add_management_paths(svc, cfg, ' (inverter)')

    _add_mode_path(svc, mode_callback)
    svc.add_path('/State', 0)
    svc.add_path('/IsInverterCharger', 0)

    for a in ('LowVoltage', 'HighVoltage', 'LowTemperature', 'HighTemperature',
              'Overload', 'Ripple', 'LowVoltageAcOut', 'HighVoltageAcOut'):
        svc.add_path('/Alarms/%s' % a, 0)

    for p in ('L1', 'L2', 'L3'):
        on_phase = (p == cfg.phase)
        svc.add_path('/Ac/Out/%s/V' % p, 230.0 if on_phase else None,
                     gettextcallback=lambda p, v: '%.0fV' % v)
        svc.add_path('/Ac/Out/%s/I' % p, 0.0 if on_phase else None,
                     gettextcallback=lambda p, v: '%.2fA' % v)
        svc.add_path('/Ac/Out/%s/P' % p, 0.0 if on_phase else None,
                     gettextcallback=lambda p, v: '%.0fW' % v)
        svc.add_path('/Ac/Out/%s/F' % p, 50.0 if on_phase else None,
                     gettextcallback=lambda p, v: '%.1fHz' % v)

    svc.add_path('/Dc/0/Voltage', None, gettextcallback=lambda p, v: '%.1fV' % v)
    svc.add_path('/Dc/0/Current', None, gettextcallback=lambda p, v: '%.1fA' % v)
    svc.add_path('/Dc/0/Power', None, gettextcallback=lambda p, v: '%.0fW' % v)
    svc.add_path('/Energy/InverterToAcOut', 0.0,
                 gettextcallback=lambda p, v: '%.2fkWh' % v)
    svc.add_path('/Temperature', None, gettextcallback=lambda p, v: '%.1fC' % v)

    return _register_with_retry(svc, service_name)


def ensure_acinput1_is_grid(bus):
    """
    systemcalc's pvinverter delegate maps Position=0 to either /Ac/PvOnGrid or
    /Ac/PvOnGenset by looking up /Settings/SystemSetup/AcInput1 in
    localsettings (1=Grid, 2=Genset, 3=Shore). On systems that never had a
    Multiplus, that setting doesn't exist — so the mapping returns None and
    our production is silently skipped from all consumption aggregation.

    We create the setting on first startup (idempotent — AddSetting is a
    no-op if the path already exists). Value 1 = Grid, which is correct for
    the vast majority of Soyosource installations.
    """
    try:
        settings = bus.get_object('com.victronenergy.settings', '/Settings',
                                  follow_name_owner_changes=True)
        # AddSetting(group, name, default_value, itemType, minimum, maximum)
        # Returns 0 on success (path created or already existed).
        ret = settings.AddSetting(
            'SystemSetup', 'AcInput1', dbus.Int32(1), 'i',
            dbus.Int32(0), dbus.Int32(0),
            dbus_interface='com.victronenergy.Settings')
        log.info("Ensured /Settings/SystemSetup/AcInput1 exists (ret=%s) — "
                 "required for pvinverter Position=0 aggregation", ret)
    except dbus.DBusException as e:
        log.warning("Could not ensure /Settings/SystemSetup/AcInput1 exists: %s — "
                    "if Total Consumption on VRM is missing L1 load, create it "
                    "manually with value 1 (Grid).", e)


def create_pvinverter_service(bus, cfg, mode_callback):
    """
    Register as com.victronenergy.pvinverter.<...> at Position=0 (AC input 1 /
    grid bus). This is the correct service type for our topology: the
    Soyosource's output feeds back onto the same AC bus the grid meter sits
    on — exactly what a Fronius/Enphase AC-coupled solar inverter does, except
    our DC source is a battery rather than panels.

    Why not `inverter`: that type assumes a separate AC-output loads bus (like
    a Multiplus's AC-Out terminal). systemcalc then double-counts our output
    as "Essential Loads" in Total Consumption — wrong for a grid-tie inverter.

    Why not `vebus`: correct semantics, but triggers DVCC/ESS/BMS delegates
    that assume a real Multiplus and crash on the differences. See CLAUDE.md.

    Trade-off: Soyosource shows up under "PV"/"Solar" aggregations on the
    dashboard despite being battery-sourced. Cosmetic; the accounting is
    right. Our `/Mode` path stays writable for On/Eco/Off control via D-Bus /
    MQTT / Node-RED (GUIv2 doesn't render a mode switch on pvinverter tiles).
    """
    ensure_acinput1_is_grid(bus)

    service_name = 'com.victronenergy.pvinverter.soyosource_%d' % cfg.device_instance
    svc = VeDbusService(service_name, bus=bus, register=False)

    _add_management_paths(svc, cfg, ' (pvinverter)')

    # Position: 0 = AC input 1 (grid bus). systemcalc uses this to decide
    # which ConsumptionOnInput[Lx] to credit our production into.
    svc.add_path('/Position', 0)

    _add_mode_path(svc, mode_callback)

    # StatusCode follows Fronius convention: 7 = Running, 8 = Standby.
    svc.add_path('/StatusCode', 8)
    svc.add_path('/ErrorCode', 0)

    # Total and per-phase production.
    svc.add_path('/Ac/Power', 0.0, gettextcallback=lambda p, v: '%.0fW' % v)
    svc.add_path('/Ac/MaxPower', cfg.max_power_demand,
                 gettextcallback=lambda p, v: '%.0fW' % v)
    svc.add_path('/Ac/Energy/Forward', 0.0,
                 gettextcallback=lambda p, v: '%.2fkWh' % v)

    for p in ('L1', 'L2', 'L3'):
        on_phase = (p == cfg.phase)
        svc.add_path('/Ac/%s/Power' % p, 0.0 if on_phase else None,
                     gettextcallback=lambda p, v: '%.0fW' % v)
        svc.add_path('/Ac/%s/Voltage' % p, 230.0 if on_phase else None,
                     gettextcallback=lambda p, v: '%.0fV' % v)
        svc.add_path('/Ac/%s/Current' % p, 0.0 if on_phase else None,
                     gettextcallback=lambda p, v: '%.2fA' % v)
        svc.add_path('/Ac/%s/Energy/Forward' % p, 0.0 if on_phase else None,
                     gettextcallback=lambda p, v: '%.2fkWh' % v)

    return _register_with_retry(svc, service_name)


def create_service(bus, cfg, mode_callback):
    t = cfg.service_type
    if t == 'pvinverter':
        return create_pvinverter_service(bus, cfg, mode_callback)
    if t == 'vebus':
        return create_vebus_service(bus, cfg, mode_callback)
    if t == 'inverter':
        return create_inverter_service(bus, cfg, mode_callback)
    log.warning("Unknown ServiceType %r — defaulting to pvinverter", t)
    return create_pvinverter_service(bus, cfg, mode_callback)


# -----------------------------------------------------------------------------
# Main control loop
# -----------------------------------------------------------------------------

class SoyosourceService:
    def __init__(self, cfg):
        self.cfg = cfg

        DBusGMainLoop(set_as_default=True)
        self.bus = dbus.SessionBus() if 'DBUS_SESSION_BUS_ADDRESS' in os.environ else dbus.SystemBus()

        self.grid = GridReader(self.bus, cfg.track_phase)
        self.serial = SerialLink(cfg.serial_port)

        # Runtime mode (see /Mode semantics comment above). Ephemeral — resets
        # to MODE_ON on every service restart.
        self.mode = MODE_ON

        self.svc = create_service(self.bus, cfg, mode_callback=self._on_mode_write)

        self.last_demand = 0            # last demand we sent (watts)
        self.energy_kwh = 0.0           # running forward energy
        self.last_energy_ts = time.time()

        # Lazy-cached proxy for /Dc/Battery/Voltage on com.victronenergy.system.
        # Resolved on first read and refreshed automatically on systemcalc
        # restarts via follow_name_owner_changes=True. See _read_battery_voltage.
        self._battery_voltage_proxy = None

        # Diagnostics: 60s heartbeat + drift detector. Demand-change logs don't
        # show the steady-state wedge pattern (frame count, grid convergence),
        # so we emit a compact status line every minute and a WARNING when
        # commanded demand stops moving the grid reading toward target.
        self.tx_count = 0
        self.last_heartbeat_ts = time.time()
        self.drift_ticks = 0
        self.drift_warned = False

        self.running = True

        # Schedule loops
        GLib.timeout_add(int(cfg.send_interval_s * 1000), self._tx_tick)
        GLib.timeout_add(int(cfg.update_interval_s * 1000), self._update_tick)

        # Graceful shutdown
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)

    # ------------------------------------------------------------------- Mode
    def _on_mode_write(self, new_mode):
        """Accept a /Mode write from GUIv2 (or any D-Bus client). Returns True to accept."""
        try:
            new_mode = int(new_mode)
        except (TypeError, ValueError):
            log.warning("Rejecting /Mode write: non-integer %r", new_mode)
            return False
        if new_mode not in VALID_MODES:
            log.warning("Rejecting /Mode write: %d not in %s", new_mode, VALID_MODES)
            return False
        if new_mode == self.mode:
            return True

        old_mode = self.mode
        self.mode = new_mode
        log.info("Mode changed: %s -> %s",
                 _mode_name(old_mode), _mode_name(new_mode))

        # On transition into Off, send a short burst of 0W frames so the
        # inverter ramps down immediately rather than waiting for its own
        # ~10s auto-off timeout.
        if new_mode == MODE_OFF:
            self._send_zero_burst()
            self.last_demand = 0
        return True

    def _send_zero_burst(self, count=5, gap_s=0.1):
        """Send N 0W frames back-to-back. Used on shutdown and Off-transition."""
        zero = soyosource.build_demand_frame(0)
        for _ in range(count):
            self.serial.write(zero)
            time.sleep(gap_s)

    # ------------------------------------------------------------------ DC estimation
    def _read_battery_voltage(self):
        """
        Read /Dc/Battery/Voltage from com.victronenergy.system.

        systemcalc publishes this path by copying from the elected battery
        service (JKBMS, BMV, SmartShunt, etc.). Cached proxy uses
        follow_name_owner_changes=True so a systemcalc restart doesn't strand
        us on a dead connection ID.

        Returns float on success, None if unreachable or not yet published.
        """
        if self._battery_voltage_proxy is None:
            try:
                self._battery_voltage_proxy = self.bus.get_object(
                    'com.victronenergy.system', '/Dc/Battery/Voltage',
                    follow_name_owner_changes=True)
            except dbus.DBusException:
                return None
        try:
            raw = self._battery_voltage_proxy.GetValue(
                dbus_interface='com.victronenergy.BusItem')
            return float(raw)
        except (dbus.DBusException, TypeError, ValueError):
            # Either systemcalc isn't reachable (transient) or the path isn't
            # published yet (no battery monitor). Caller handles None.
            return None

    def _estimate_dc(self, p_ac):
        """
        Estimate /Dc/0/{Voltage,Current,Power} from commanded AC output.

        Returns (voltage, current, power) with systemcalc's sign convention:
          voltage > 0 (V)
          current < 0 when inverting (current flowing OUT of DC bus into us)
          power   < 0 when inverting

        Why negative current: systemcalc's comment at dbus_systemcalc.py line
        ~812 spells it out — "VE.Bus: Positive: current flowing from the Multi
        to the dc system or battery". We're pushing power the other way, so
        current is negative from the DC bus's perspective. Same convention for
        non-vebus inverters (`inverter_power += V*I`, compared against the
        AC-side fallback `inverter_power -= V_ac*I_ac` which is always
        negative).

        When p_ac == 0 we return zero current/power regardless of voltage
        availability so an idle inverter doesn't pollute DcSystemPower.

        If /Dc/Battery/Voltage isn't published (no battery monitor, or
        systemcalc down), we fall back to FALLBACK_BATTERY_VOLTAGE. Worst
        case the current magnitude is off by the voltage ratio — the sign
        and the ballpark are still right, which is what DcSystemPower
        accounting needs.
        """
        v = self._read_battery_voltage()
        if v is None or v <= 0:
            v = FALLBACK_BATTERY_VOLTAGE
        if p_ac <= 0:
            return (v, 0.0, 0.0)
        # DC input has to be slightly higher than AC output due to conversion
        # losses — so current drawn from battery is p_ac/efficiency/V.
        p_dc = p_ac / INVERTER_EFFICIENCY
        i = -p_dc / v
        return (v, i, -p_dc)

    # ------------------------------------------------------------------ TX loop
    def _tx_tick(self):
        """Send the current demand to the inverter. Runs every send_interval_s."""
        if not self.running:
            return False

        # Safety net: if an in-flight scanner's cleanup removed our
        # /var/lock/serial-starter/<tty> symlink, re-create it before
        # serial-starter's next 2-second iteration runs a new probe.
        # We always refresh the lock even in Off mode — otherwise serial-starter
        # would probe our tty and another service could claim it.
        self.serial.refresh_lock()

        # In Off mode we deliberately send nothing. The inverter auto-shuts
        # down after ~10s without frames.
        if self.mode == MODE_OFF:
            return True

        target = self.last_demand

        if self.mode == MODE_ON:
            if self.grid.last_power is None or self.grid.is_stale(self.cfg.stale_timeout_s):
                # Safety: no fresh grid data, force 0W
                if target != 0:
                    log.warning("Grid data stale — forcing demand to 0W")
                target = 0

        frame = soyosource.build_demand_frame(target)
        ok = self.serial.write(frame)
        if ok:
            self.tx_count += 1
            log.debug("Sent demand %dW: %s", target, frame.hex(' '))
        return True

    # -------------------------------------------------------------- Update loop
    def _update_tick(self):
        """Recalculate demand and update D-Bus status."""
        if not self.running:
            return False

        now = time.time()

        # We still poll the grid meter in every mode — Eco/Off don't use the
        # value for the control loop, but the poll keeps last_update fresh for
        # diagnostics and the On→... transition makes the value immediately
        # available.
        changed = self.grid.poll()
        grid = self.grid.last_power

        if self.mode == MODE_OFF:
            new_demand = 0
        elif self.mode == MODE_ECO:
            new_demand = self.cfg.eco_power_demand
        else:  # MODE_ON
            if grid is None or self.grid.is_stale(self.cfg.stale_timeout_s):
                new_demand = 0
            elif changed:
                new_demand = soyosource.calculate_demand(
                    grid_power=grid,
                    last_demand=self.last_demand,
                    buffer_w=self.cfg.target_grid_w,
                    min_demand=self.cfg.min_power_demand,
                    max_demand=self.cfg.max_power_demand,
                    damping=self.cfg.damping,
                )
            else:
                new_demand = self.last_demand

        if new_demand != self.last_demand:
            grid_str = '%.1fW' % grid if grid is not None else 'n/a'
            log.info("mode=%s grid=%s demand %d -> %d",
                     _mode_name(self.mode), grid_str, self.last_demand, new_demand)
        self.last_demand = new_demand

        self._heartbeat(now, grid)
        self._check_drift(grid)

        # Integrate commanded energy (rough — inverter reports ~98% of command)
        dt = now - self.last_energy_ts
        self.energy_kwh += (self.last_demand * dt) / 3600000.0
        self.last_energy_ts = now

        # Try to parse any status response the inverter may have sent
        buf = self.serial.read_available()
        status = None
        if buf and len(buf) >= soyosource.STATUS_FRAME_LEN:
            # Look for header
            idx = buf.find(soyosource.STATUS_HEADER)
            if idx >= 0 and len(buf) - idx >= soyosource.STATUS_FRAME_LEN:
                status = soyosource.parse_status_response(
                    buf[idx:idx + soyosource.STATUS_FRAME_LEN]
                )

        self._publish(status)
        return True

    # ----------------------------------------------------------- Instrumentation
    HEARTBEAT_INTERVAL_S = 60.0

    # Drift = commanding real demand but grid isn't pulling toward target.
    # - Needs at least DRIFT_DEMAND_MIN_W commanded to make the signal meaningful
    #   (tiny commands legitimately don't move a noisy grid meter).
    # - WARN_TOLERANCE_W is the gap-above-target that counts as drift; kept wide
    #   because household load spikes routinely push grid 100-200 W above target
    #   even when the inverter is fine.
    # - CLEAR_TOLERANCE_W is a tighter band used for hysteresis: the gap has to
    #   drop well below the warn threshold before we announce recovery. Without
    #   this, a fluctuating grid crossing WARN_TOLERANCE_W flaps the warning on
    #   and off every few seconds.
    # - THRESHOLD_TICKS is how many consecutive update ticks above
    #   WARN_TOLERANCE_W must accumulate before warning. At
    #   UpdateIntervalSeconds=1s the default is 30 s of drift.
    DRIFT_DEMAND_MIN_W = 100
    DRIFT_WARN_TOLERANCE_W = 300
    DRIFT_CLEAR_TOLERANCE_W = 100
    DRIFT_THRESHOLD_TICKS = 30

    def _heartbeat(self, now, grid):
        if now - self.last_heartbeat_ts < self.HEARTBEAT_INTERVAL_S:
            return
        elapsed = now - self.last_heartbeat_ts
        grid_str = '%.1fW' % grid if grid is not None else 'n/a'
        if self.grid.last_update:
            age_str = '%.1fs' % max(0.0, now - self.grid.last_update)
        else:
            age_str = 'never'
        log.info("heartbeat: mode=%s demand=%dW grid=%s grid_age=%s tx=%d/%.0fs",
                 _mode_name(self.mode), self.last_demand, grid_str,
                 age_str, self.tx_count, elapsed)
        self.tx_count = 0
        self.last_heartbeat_ts = now

    def _check_drift(self, grid):
        """Detect wedge: commanded demand > DRIFT_DEMAND_MIN_W for
        DRIFT_THRESHOLD_TICKS but grid stays > target + DRIFT_WARN_TOLERANCE_W.
        Hysteresis: warn above WARN band, clear only below tighter CLEAR band;
        inside the deadband we keep whatever state we had."""
        if (self.mode != MODE_ON
                or grid is None
                or self.grid.is_stale(self.cfg.stale_timeout_s)):
            # Lost the signal we'd base drift on. Reset silently.
            self.drift_ticks = 0
            self.drift_warned = False
            return

        if self.last_demand < self.DRIFT_DEMAND_MIN_W:
            # Not commanding enough to expect observable grid movement.
            self.drift_ticks = 0
            self.drift_warned = False
            return

        gap = grid - self.cfg.target_grid_w

        if gap > self.DRIFT_WARN_TOLERANCE_W:
            self.drift_ticks += 1
            if self.drift_ticks == self.DRIFT_THRESHOLD_TICKS and not self.drift_warned:
                log.warning(
                    "DRIFT: commanded %dW for %d ticks, grid=%.1fW still %.1fW above "
                    "target=%dW. Inverter may be wedged (physical production not "
                    "matching command).",
                    self.last_demand, self.drift_ticks, grid, gap,
                    self.cfg.target_grid_w,
                )
                self.drift_warned = True
        elif gap < self.DRIFT_CLEAR_TOLERANCE_W:
            if self.drift_warned:
                log.info("DRIFT cleared: demand=%dW grid=%.1fW (back within tolerance)",
                         self.last_demand, grid)
            self.drift_ticks = 0
            self.drift_warned = False
        # else: deadband — keep current ticks/warned state, no log

    # ------------------------------------------------------------ D-Bus publish
    def _publish(self, status):
        p = self.last_demand
        phase = self.cfg.phase

        # Use measured AC voltage if the inverter answered a status query,
        # otherwise fall back to nominal 230 V.
        v = 230.0
        if status and status['ac_voltage']:
            v = float(status['ac_voltage'])
        current = (p / v) if v else 0.0

        if self.cfg.service_type == 'pvinverter':
            self._publish_pvinverter(p, phase, v, current)
        elif self.cfg.service_type == 'vebus':
            self._publish_vebus(p, phase, v, current, status)
        else:  # inverter
            self._publish_inverter(p, phase, v, current, status)

    def _publish_pvinverter(self, p, phase, v, current):
        # Standard pvinverter paths: /Ac/Power + /Ac/<L>/{Power,Voltage,
        # Current,Energy/Forward}. No /Dc/*, no /State, no Multi-style
        # /Ac/Out/* — systemcalc treats this as AC-coupled production on the
        # grid bus and subtracts it from ConsumptionOnInput.
        self.svc['/Ac/Power'] = p
        self.svc['/Ac/Energy/Forward'] = self.energy_kwh
        self.svc['/Ac/%s/Power' % phase] = p
        self.svc['/Ac/%s/Voltage' % phase] = v
        self.svc['/Ac/%s/Current' % phase] = current
        self.svc['/Ac/%s/Energy/Forward' % phase] = self.energy_kwh
        # Fronius-style: 7 = Running, 8 = Standby. GUIv2 renders the tile
        # differently for each.
        self.svc['/StatusCode'] = 7 if p > 0 else 8

    def _publish_vebus(self, p, phase, v, current, status):
        # Negative /Ac/ActiveIn/<L>/P = we're pushing power OUT of the AC
        # input terminal, back to the grid bus. That's ESS-mode semantics,
        # and it's exactly what makes systemcalc's
        #   ConsumptionOnInput[Lx] = Grid[Lx] - ActiveIn[Lx]
        # come out right: subtract a negative = add our production.
        self.svc['/Ac/ActiveIn/%s/P' % phase] = -p
        self.svc['/Ac/ActiveIn/%s/V' % phase] = v
        self.svc['/Ac/ActiveIn/%s/I' % phase] = -current
        if status and status['ac_frequency']:
            self.svc['/Ac/ActiveIn/%s/F' % phase] = float(status['ac_frequency'])

        # AC Output stays zero — no essential-loads bus.
        self.svc['/Ac/Out/%s/P' % phase] = 0.0
        self.svc['/Ac/Out/%s/V' % phase] = v
        self.svc['/Ac/Out/%s/I' % phase] = 0.0

        # State: 9 = inverting (producing); 0 = off. Mirror on VebusMainState.
        state = 9 if p > 0 else 0
        self.svc['/State'] = state
        self.svc['/VebusMainState'] = state

        # Energy: integrate "inverter→AC-in1" (power flowing from our DC side
        # out our AC input terminal to grid).
        self.svc['/Energy/InverterToAcIn1'] = self.energy_kwh

        # DC side. Two sources:
        #   status present (response to status query) — use inverter's own V/I.
        #     Soyosource reports battery_current as positive magnitude, so we
        #     negate when inverting to match systemcalc's "positive = current
        #     flowing from Multi to DC" convention.
        #   status absent (2022 purple mainboards) — estimate from commanded
        #     AC power and the system-wide battery voltage. Essential for
        #     correct Total-Consumption accounting: without this, systemcalc
        #     attributes the whole battery discharge to "DC Loads".
        if status and status['battery_voltage']:
            v_dc = float(status['battery_voltage'])
            i_mag = float(status['battery_current'])  # positive magnitude
            i_dc = -i_mag if p > 0 else 0.0
            self.svc['/Dc/0/Voltage'] = v_dc
            self.svc['/Dc/0/Current'] = i_dc
            self.svc['/Dc/0/Power'] = v_dc * i_dc
            self.svc['/Temperature'] = status['temperature']
        else:
            v_dc, i_dc, p_dc = self._estimate_dc(p)
            if v_dc is not None:
                self.svc['/Dc/0/Voltage'] = v_dc
                self.svc['/Dc/0/Current'] = i_dc
                self.svc['/Dc/0/Power'] = p_dc

    def _publish_inverter(self, p, phase, v, current, status):
        if status and status['ac_voltage']:
            self.svc['/Ac/Out/%s/V' % phase] = v
        if status and status['ac_frequency']:
            self.svc['/Ac/Out/%s/F' % phase] = float(status['ac_frequency'])

        self.svc['/Ac/Out/%s/P' % phase] = p
        self.svc['/Ac/Out/%s/I' % phase] = current

        # State: inverting iff producing power
        self.svc['/State'] = 9 if p > 0 else 0

        self.svc['/Energy/InverterToAcOut'] = self.energy_kwh

        # Same DC estimation as the vebus branch — systemcalc treats non-vebus
        # `inverter` services with the parallel formula
        #     inverter_power += V_dc * I_dc
        # (and falls back to -V_ac * I_ac if DC is missing). Sign convention
        # matches: I_dc negative when inverting. Without this, our DC draw
        # goes unaccounted and Total Consumption gets inflated with phantom
        # DC loads.
        if status and status['battery_voltage']:
            v_dc = float(status['battery_voltage'])
            i_mag = float(status['battery_current'])
            i_dc = -i_mag if p > 0 else 0.0
            self.svc['/Dc/0/Voltage'] = v_dc
            self.svc['/Dc/0/Current'] = i_dc
            self.svc['/Dc/0/Power'] = v_dc * i_dc
            self.svc['/Temperature'] = status['temperature']
        else:
            v_dc, i_dc, p_dc = self._estimate_dc(p)
            if v_dc is not None:
                self.svc['/Dc/0/Voltage'] = v_dc
                self.svc['/Dc/0/Current'] = i_dc
                self.svc['/Dc/0/Power'] = p_dc

    # -------------------------------------------------------- Signal / shutdown
    def _on_signal(self, signum, frame):
        log.info("Signal %d received — sending 0W and shutting down", signum)
        self.running = False
        try:
            self._send_zero_burst()
        finally:
            self.serial.close()
            # Release the serial-starter lock so the system can resume
            # normal port management (probes etc.) if/when we're uninstalled.
            self.serial.release_lock()
            sys.exit(0)


# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------

def main():
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.ini')
    cfg = Config(cfg_path)

    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    )

    log.info("Starting dbus-soyosource — phase=%s track=%s port=%s max=%dW target=%dW",
             cfg.phase, cfg.track_phase, cfg.serial_port,
             cfg.max_power_demand, cfg.target_grid_w)

    SoyosourceService(cfg)

    loop = GLib.MainLoop()
    loop.run()


if __name__ == '__main__':
    main()
