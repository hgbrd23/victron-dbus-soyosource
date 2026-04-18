"""
Soyosource GTN RS-485 virtual meter protocol.

Pure protocol module: frame building, parsing, and checksum.
No serial I/O — that's the caller's responsibility.

Reference: https://github.com/syssi/esphome-soyosource-gtn-virtual-meter
"""

# Protocol constants
DEVICE_ADDR = 0x24
CMD_POWER_DEMAND = 0x56

DEMAND_FRAME_LEN = 8
QUERY_FRAME_LEN = 8
STATUS_FRAME_LEN = 15

# Response header (inverter -> host)
STATUS_HEADER = bytes([0x23, 0x01, 0x01, 0x00])

# Serial parameters
BAUD_RATE = 4800
BYTE_SIZE = 8
PARITY = 'N'
STOP_BITS = 1
INTER_FRAME_GAP_MS = 50


def calculate_checksum(msb: int, lsb: int) -> int:
    """Checksum for the power demand frame. Three bytes sum to 264 (0x108)."""
    return (264 - msb - lsb) & 0xFF


def build_demand_frame(watts: int) -> bytes:
    """
    Build an 8-byte power demand frame.

    watts is the requested AC output power. Must fit in 16 bits (0..65535).
    """
    if watts < 0:
        raise ValueError("watts must be >= 0 (got %d)" % watts)
    if watts > 0xFFFF:
        raise ValueError("watts must fit in 16 bits (got %d)" % watts)

    msb = (watts >> 8) & 0xFF
    lsb = watts & 0xFF
    chk = calculate_checksum(msb, lsb)

    return bytes([
        DEVICE_ADDR,
        CMD_POWER_DEMAND,
        0x00,
        0x21,
        msb,
        lsb,
        0x80,
        chk,
    ])


def build_status_query() -> bytes:
    """Build the 8-byte status query frame."""
    return bytes([DEVICE_ADDR, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])


def parse_demand_frame(data: bytes):
    """
    Parse a demand frame (for bus sniffing / diagnostics).

    Returns dict with 'watts' and 'checksum_ok', or None if not a demand frame.
    """
    if len(data) != DEMAND_FRAME_LEN:
        return None
    if data[0] != DEVICE_ADDR or data[1] != CMD_POWER_DEMAND:
        return None
    watts = (data[4] << 8) | data[5]
    chk_expected = calculate_checksum(data[4], data[5])
    return {
        'watts': watts,
        'checksum_ok': data[7] == chk_expected,
    }


def parse_status_response(data: bytes):
    """
    Parse a 15-byte status response from the inverter.

    Returns a dict with fields:
        operation_status: int (0 = normal)
        battery_voltage: float (V)
        battery_current: float (A)
        ac_voltage:      int   (V)
        ac_frequency:    float (Hz)
        temperature:     float (degC)

    Returns None if the frame is malformed.
    """
    if len(data) != STATUS_FRAME_LEN:
        return None
    if data[:4] != STATUS_HEADER:
        return None

    op = data[4]
    bat_v = ((data[5] << 8) | data[6]) * 0.1
    bat_i = ((data[7] << 8) | data[8]) * 0.1
    ac_v = (data[9] << 8) | data[10]
    ac_hz = data[11] * 0.5
    temp_raw = (data[12] << 8) | data[13]
    temp = (temp_raw - 300) * 0.1

    return {
        'operation_status': op,
        'battery_voltage': bat_v,
        'battery_current': bat_i,
        'ac_voltage': ac_v,
        'ac_frequency': ac_hz,
        'temperature': temp,
    }


def calculate_demand(grid_power: float,
                     last_demand: int,
                     buffer_w: int,
                     min_demand: int,
                     max_demand: int,
                     damping: float = 1.0) -> int:
    """
    Calculate the new power demand based on grid power.

    Uses the "NEGATIVE_MEASUREMENTS_REQUIRED" mode from the ESPHome project,
    which is the right choice for VenusOS (it provides signed grid power).

    grid_power:  current grid power in watts (positive = import, negative = export)
    last_demand: previously sent demand in watts
    buffer_w:    safety margin — we aim to import this many watts (to avoid export)
    min_demand:  minimum demand to send when active
    max_demand:  hard upper limit
    damping:     0 < damping <= 1. Fraction of the error to apply per tick.
                 1.0 = full correction (fastest, may overshoot).
                 0.5 = correct half the error each tick (smoother).

    Returns the new demand clamped to [0, max_demand].
    """
    # How much are we importing beyond our target buffer?
    # Positive = importing more than buffer, need to produce more
    # Negative = exporting or importing less than buffer, need to produce less
    excess_import = grid_power - buffer_w

    new_demand = last_demand + int(round(excess_import * damping))

    # Clamp
    if new_demand < 0:
        new_demand = 0
    if new_demand > 0 and new_demand < min_demand:
        new_demand = min_demand
    if new_demand > max_demand:
        new_demand = max_demand

    return new_demand
