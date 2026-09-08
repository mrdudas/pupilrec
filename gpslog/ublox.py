"""Framing, configuration and parsing for a u-blox GNSS receiver.

Only what this project needs: put the receiver into a known state, then read
UBX-NAV-PVT, which carries a complete navigation solution -- time, position,
velocity, accuracy and fix status -- in one message per epoch. NMEA would need
several sentences stitched together per epoch and carries less.

Measured on the attached UBX-M8030 (protocol 18.00): 18.2 Hz with GPS alone,
10 Hz with GPS+GLONASS+Galileo+BeiDou. Anything below a 55 ms measurement
period is silently clamped to 100 ms.
"""

from __future__ import annotations

import struct

SYNC = b"\xb5\x62"

CLS_NAV, MSG_PVT = 0x01, 0x07
CLS_ACK, MSG_ACK, MSG_NAK = 0x05, 0x01, 0x00
CLS_CFG, MSG_CFG_MSG, MSG_CFG_RATE, MSG_CFG_GNSS = 0x06, 0x01, 0x08, 0x3E

PVT_PAYLOAD_BYTES = 92

FIX_TYPES = {
    0: "none", 1: "dead-reckoning", 2: "2D", 3: "3D",
    4: "GNSS+DR", 5: "time-only",
}

# gnssId, reserved channels, max channels -- the receiver rejects a block set
# that does not cover every constellation it knows about.
GNSS_BLOCKS = ((0, 8, 16), (1, 1, 3), (2, 4, 8), (3, 8, 16),
               (4, 0, 8), (5, 0, 3), (6, 8, 14))
GNSS_GPS, GNSS_SBAS, GNSS_GALILEO, GNSS_BEIDOU, GNSS_IMES, GNSS_QZSS, GNSS_GLONASS = range(7)

# QZSS must follow GPS, per u-blox: enabling one without the other is rejected.
GPS_ONLY = {GNSS_GPS, GNSS_QZSS}
ALL_GNSS = {GNSS_GPS, GNSS_SBAS, GNSS_GALILEO, GNSS_BEIDOU, GNSS_QZSS, GNSS_GLONASS}


def checksum(body: bytes) -> bytes:
    a = b = 0
    for byte in body:
        a = (a + byte) & 0xFF
        b = (b + a) & 0xFF
    return bytes((a, b))


def frame(cls: int, msg: int, payload: bytes = b"") -> bytes:
    body = bytes((cls, msg)) + struct.pack("<H", len(payload)) + payload
    return SYNC + body + checksum(body)


def cfg_rate(measurement_ms: int) -> bytes:
    """Navigation solution period. timeRef 1 = GPS time."""
    return frame(CLS_CFG, MSG_CFG_RATE, struct.pack("<HHH", measurement_ms, 1, 1))


def cfg_message(cls: int, msg: int, rate: int) -> bytes:
    """How often to emit a message on the current port (0 disables it)."""
    return frame(CLS_CFG, MSG_CFG_MSG, bytes((cls, msg, rate)))


def cfg_gnss(enabled: set[int]) -> bytes:
    blocks = b""
    for gnss_id, reserved, maximum in GNSS_BLOCKS:
        flags = (1 if gnss_id in enabled else 0) | (0x01 << 16)   # enable + L1 signal
        blocks += struct.pack("<BBBBI", gnss_id, reserved, maximum, 0, flags)
    header = struct.pack("<BBBB", 0, 0, 0xFF, len(GNSS_BLOCKS))
    return frame(CLS_CFG, MSG_CFG_GNSS, header + blocks)


class Reader:
    """Extracts UBX messages from a byte stream, tolerating partial reads."""

    def __init__(self):
        self._buf = bytearray()

    def feed(self, data: bytes):
        """-> list of (cls, msg, payload). Anything not UBX is skipped."""
        self._buf += data
        out = []
        while True:
            start = self._buf.find(SYNC)
            if start < 0:
                # Keep one byte in case a sync word straddles two reads.
                del self._buf[:max(0, len(self._buf) - 1)]
                break
            if start:
                del self._buf[:start]
            if len(self._buf) < 6:
                break
            cls, msg = self._buf[2], self._buf[3]
            length = struct.unpack_from("<H", self._buf, 4)[0]
            total = 6 + length + 2
            if length > 8192:                      # not a real message
                del self._buf[:2]
                continue
            if len(self._buf) < total:
                break
            body = bytes(self._buf[2:6 + length])
            if bytes(self._buf[6 + length:total]) == checksum(body):
                out.append((cls, msg, bytes(self._buf[6:6 + length])))
                del self._buf[:total]
            else:
                del self._buf[:2]                  # bad checksum: resynchronise
        return out


def parse_pvt(payload: bytes) -> dict | None:
    """UBX-NAV-PVT -> a flat dict in human units, or None if it is not one."""
    if len(payload) < PVT_PAYLOAD_BYTES:
        return None
    (itow, year, month, day, hour, minute, second, valid, t_acc, nano,
     fix_type, flags, flags2, num_sv, lon, lat, height, hmsl, h_acc, v_acc,
     vel_n, vel_e, vel_d, g_speed, head_mot, s_acc, head_acc, p_dop
     ) = struct.unpack_from("<IHBBBBBBIiBBBBiiiiIIiiiiiIIH", payload, 0)

    fix_ok = bool(flags & 0x01)
    return {
        "itow_s": itow / 1000.0,
        "gps_time": (f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:"
                     f"{second:02d}.{max(0, nano) // 1000000:03d}Z"
                     if valid & 0x03 else ""),
        "time_valid": bool(valid & 0x03),
        "fix_type": fix_type,
        "fix": FIX_TYPES.get(fix_type, str(fix_type)),
        "fix_ok": fix_ok,
        "num_sv": num_sv,
        # Position is only meaningful once the receiver reports a valid fix.
        "lat_deg": lat * 1e-7 if fix_ok else None,
        "lon_deg": lon * 1e-7 if fix_ok else None,
        "height_m": height / 1000.0 if fix_ok else None,
        "hmsl_m": hmsl / 1000.0 if fix_ok else None,
        "h_acc_m": h_acc / 1000.0,
        "v_acc_m": v_acc / 1000.0,
        "speed_mps": g_speed / 1000.0 if fix_ok else None,
        "heading_deg": head_mot * 1e-5 if fix_ok else None,
        "vel_n_mps": vel_n / 1000.0 if fix_ok else None,
        "vel_e_mps": vel_e / 1000.0 if fix_ok else None,
        "vel_d_mps": vel_d / 1000.0 if fix_ok else None,
        "pdop": p_dop / 100.0,
    }
