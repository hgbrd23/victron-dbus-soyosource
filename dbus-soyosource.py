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

        # D-Bus service type. Two viable options for a Soyosource:
        #   "vebus"    — registers as com.victronenergy.vebus.<...>. GUIv2's
        #                "Inverter/Charger" tile shows both state AND power.
        #                This is what Multiplus/Quattro use. Some VenusOS code
        #                (ESS/Hub) is gated on vebus presence; for a system
        #                that already had no Multiplus this is fine.
        #   "inverter" — registers as com.victronenergy.inverter.<...>. GUIv2
        #                tile shows state ("Inverting") only, no power value.
        #                Semantically cleaner if you don't want any vebus
        #                side-effects.
        self.service_type = s.get('ServiceType', 'vebus').strip().lower()

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


def _add_management_paths(svc, cfg, version_suffix=''):
    svc.add_path('/Mgmt/ProcessName', __file__)
    svc.add_path('/Mgmt/ProcessVersion',
                 'dbus-soyosource 0.1%s on Python %s' % (version_suffix, platform.python_version()))
    svc.add_path('/Mgmt/Connection', 'RS-485 on %s' % cfg.serial_port)
    svc.add_path('/DeviceInstance', cfg.device_instance)
    svc.add_path('/ProductId', 0xA144)
    svc.add_path('/ProductName', 'Soyosource GTN')
    svc.add_path('/CustomName', cfg.custom_name, writeable=True)
    svc.add_path('/FirmwareVersion', 'unknown')
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
    Register as com.victronenergy.vebus.<...> so GUIv2 displays the power
    value (not just the state) in the Inverter/Charger tile.

    A Soyosource doesn't have a real AC input — it's a one-terminal grid-tie
    inverter — so we publish /Ac/ActiveIn/* as zeros. Loads on the AC output
    bus (which is the household grid in our case) get reported as
    "Essential Loads" by systemcalc; that's a known consequence of pretending
    to be a vebus device.
    """
    service_name = 'com.victronenergy.vebus.soyosource_%d' % cfg.device_instance
    svc = VeDbusService(service_name, bus=bus, register=False)

    _add_management_paths(svc, cfg, ' (vebus)')

    _add_mode_path(svc, mode_callback)
    # State: 0=off, 1=low power, 2=fault, 3=bulk, 4=absorption, 5=float,
    #        6=storage, 7=equalize, 8=passthru, 9=inverting, 10=power assist,
    #        11=power supply.
    svc.add_path('/State', 0)

    # Active AC input: 0/1 = AC input 1/2, 240 = disconnected. We have no real
    # AC input; 240 keeps systemcalc from showing imaginary grid passthrough.
    svc.add_path('/Ac/ActiveIn/ActiveInput', 240)

    for p in ('L1', 'L2', 'L3'):
        on_phase = (p == cfg.phase)
        # AC input — always 0 (no real input)
        svc.add_path('/Ac/ActiveIn/%s/P' % p, 0.0,
                     gettextcallback=lambda p, v: '%.0fW' % v)
        svc.add_path('/Ac/ActiveIn/%s/I' % p, 0.0,
                     gettextcallback=lambda p, v: '%.2fA' % v)
        # AC output — populated only on the configured phase
        svc.add_path('/Ac/Out/%s/P' % p, 0.0 if on_phase else None,
                     gettextcallback=lambda p, v: '%.0fW' % v)
        svc.add_path('/Ac/Out/%s/V' % p, 230.0 if on_phase else None,
                     gettextcallback=lambda p, v: '%.0fV' % v)
        svc.add_path('/Ac/Out/%s/I' % p, 0.0 if on_phase else None,
                     gettextcallback=lambda p, v: '%.2fA' % v)
        svc.add_path('/Ac/Out/%s/F' % p, 50.0 if on_phase else None,
                     gettextcallback=lambda p, v: '%.1fHz' % v)

    # DC side. Empty until the inverter answers status queries
    # (most Soyosource mainboards don't).
    svc.add_path('/Dc/0/Voltage', None, gettextcallback=lambda p, v: '%.1fV' % v)
    svc.add_path('/Dc/0/Current', None, gettextcallback=lambda p, v: '%.1fA' % v)
    svc.add_path('/Dc/0/Power', None, gettextcallback=lambda p, v: '%.0fW' % v)
    svc.add_path('/Soc', None, gettextcallback=lambda p, v: '%.0f%%' % v)

    # Energy
    svc.add_path('/Energy/InverterToAcOut', 0.0,
                 gettextcallback=lambda p, v: '%.2fkWh' % v)
    svc.add_path('/Energy/AcOutToInverter', 0.0,
                 gettextcallback=lambda p, v: '%.2fkWh' % v)

    # Optional telemetry
    svc.add_path('/Temperature', None, gettextcallback=lambda p, v: '%.1fC' % v)

    return _register_with_retry(svc, service_name)


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


def create_service(bus, cfg, mode_callback):
    if cfg.service_type == 'vebus':
        return create_vebus_service(bus, cfg, mode_callback)
    elif cfg.service_type == 'inverter':
        return create_inverter_service(bus, cfg, mode_callback)
    else:
        log.warning("Unknown ServiceType %r — defaulting to inverter", cfg.service_type)
        return create_inverter_service(bus, cfg, mode_callback)


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

    # ------------------------------------------------------------ D-Bus publish
    def _publish(self, status):
        p = self.last_demand
        phase = self.cfg.phase

        # Use measured AC voltage if the inverter answered a status query,
        # otherwise fall back to nominal 230 V.
        v = 230.0
        if status and status['ac_voltage']:
            v = float(status['ac_voltage'])
            self.svc['/Ac/Out/%s/V' % phase] = v
        if status and status['ac_frequency']:
            self.svc['/Ac/Out/%s/F' % phase] = float(status['ac_frequency'])

        self.svc['/Ac/Out/%s/P' % phase] = p
        self.svc['/Ac/Out/%s/I' % phase] = (p / v) if v else 0.0

        # State: inverting iff producing power
        self.svc['/State'] = 9 if p > 0 else 0

        self.svc['/Energy/InverterToAcOut'] = self.energy_kwh

        if status:
            v_dc = status['battery_voltage']
            i_dc = status['battery_current']
            self.svc['/Dc/0/Voltage'] = v_dc
            self.svc['/Dc/0/Current'] = i_dc
            self.svc['/Dc/0/Power'] = v_dc * i_dc
            self.svc['/Temperature'] = status['temperature']

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
