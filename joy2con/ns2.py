# SPDX-License-Identifier: GPL-3.0-or-later
"""Switch 2 Joy-Con protocol over Bluetooth LE.

JoyCon2 owns one controller: it connects over a raw ATT socket, runs the
setup the console performs (pairing, player LEDs, feature flags), reads the
factory calibration from the controller's flash, and turns every input report
into a calibrated State. Reports are handled on the socket's reader thread and
handed to `on_state` immediately, so nothing queues between radio and consumer.

Protocol facts used here (GATT layout, command framing, report 0x05 layout,
flash addresses, pairing) are documented in research/switch2_controller_research.
"""

from __future__ import annotations

import logging
import math
import os
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from Crypto.Cipher import AES

from .att import ATTClient

log = logging.getLogger("joy2con.ns2")

PID_LEFT = 0x2067
PID_RIGHT = 0x2066
PID_PRO = 0x2069
SIDE_BY_PID = {PID_LEFT: "left", PID_RIGHT: "right", PID_PRO: "pro"}

UUID_INPUT = "ab7de9be-89fe-49ad-828f-118f09df7fd2"      # input report 0x05
UUID_COMMAND = "649d4ac9-8eb7-4e6c-af44-1ea54fe5f005"    # command requests
UUID_RESPONSE = "c765a961-d9d8-4d36-a20a-5315b111836a"   # command responses
UUID_UNKNOWN_W = "00c5af5d-1964-4e30-8f51-1956f96bd282"  # written 01 00 by the console first
UUID_RATE_DESC = "679d5510-5a24-4dee-9557-95df80486ecb"  # written 85 00 by the console
UUID_RUMBLE = {
    "left": "289326cb-a471-485d-a8f4-240c14f18241",
    "right": "fa19b0fb-cd1f-46a7-84a1-bbb09e00c149",
    "pro": "cc483f51-9258-427d-a939-630c31f72b05",
}

# Feature flags: buttons, sticks, IMU, mouse, rumble. The console uses 0x37 for Joy-Cons.
FEATURES = 0x37

# The controller's half of the pairing key exchange is fixed (B1). The long-term
# key is A1 ^ B1, so choosing A1 = wanted ^ B1 makes the key whatever we want.
PAIRING_B1 = bytes.fromhex("5cf6ee792cdf05e1ba2b6325c41a5f10")

# Report 0x05 button bits, as (byte offset, mask) -> name. Bytes 4-6 use the
# same layout as the original Switch's full report.
BUTTONS = {
    "Y": (4, 0x01), "X": (4, 0x02), "B": (4, 0x04), "A": (4, 0x08),
    "SR_R": (4, 0x10), "SL_R": (4, 0x20), "R": (4, 0x40), "ZR": (4, 0x80),
    "MINUS": (5, 0x01), "PLUS": (5, 0x02), "RSTICK": (5, 0x04), "LSTICK": (5, 0x08),
    "HOME": (5, 0x10), "CAPTURE": (5, 0x20), "C": (5, 0x40),
    "DOWN": (6, 0x01), "UP": (6, 0x02), "RIGHT": (6, 0x04), "LEFT": (6, 0x08),
    "SR_L": (6, 0x10), "SL_L": (6, 0x20), "L": (6, 0x40), "ZL": (6, 0x80),
}

ACCEL_LSB_PER_G = 4096.0


def u12_pair(b: bytes) -> tuple[int, int]:
    return b[0] | (b[1] & 0x0F) << 8, b[1] >> 4 | b[2] << 4


@dataclass
class AxisCal:
    neutral: int = 2048
    max: int = 1500   # distance from neutral to the positive end
    min: int = 1500   # distance from neutral to the negative end

    def normalize(self, raw: int) -> float:
        d = raw - self.neutral
        return d / (self.max if d >= 0 else self.min)


@dataclass
class StickCal:
    x: AxisCal = field(default_factory=AxisCal)
    y: AxisCal = field(default_factory=AxisCal)

    @staticmethod
    def parse(b: bytes) -> "StickCal | None":
        (nx, ny), (xmax, ymax), (xmin, ymin) = u12_pair(b[0:3]), u12_pair(b[3:6]), u12_pair(b[6:9])
        if not (nx and ny and xmax and ymax and xmin and ymin) or 0xFFF in (nx, ny, xmax):
            return None
        return StickCal(AxisCal(nx, xmax, xmin), AxisCal(ny, ymax, ymin))


@dataclass
class State:
    """One decoded input report. Sticks are -1..1 (calibrated, Y up), gyro in
    rad/s and accel in g, both bias-corrected and in the controller's own frame."""
    t: float = 0.0                 # host receive time (monotonic seconds)
    counter: int = 0
    b4: int = 0                    # raw button bytes 4..7
    b5: int = 0
    b6: int = 0
    b7: int = 0
    stick: tuple[float, float] = (0.0, 0.0)
    stick_raw: tuple[int, int] = (2048, 2048)
    accel: tuple[float, float, float] = (0.0, 0.0, 1.0)
    gyro: tuple[float, float, float] = (0.0, 0.0, 0.0)
    imu_ts: int = 0
    imu_valid: bool = False
    mouse: tuple[int, int, int, int] = (0, 0, 0, 0)
    battery_mv: int = 0
    charge: int = 0

    def pressed(self) -> list[str]:
        raw = {4: self.b4, 5: self.b5, 6: self.b6}
        return [n for n, (o, m) in BUTTONS.items() if raw[o] & m]


class GyroCalibrator:
    """Refines the gyro bias while the controller rests.

    Factory bias leaves a small residual drift. When the gyro and accelerometer
    have both been steady for WINDOW seconds, the mean gyro reading over that
    window is the remaining bias. Thresholds are tight so slow deliberate turns
    are never mistaken for rest.
    """
    WINDOW = 1.2
    MAX_RATE = math.radians(2.5)     # per-axis mean must stay under this
    MAX_SPREAD = math.radians(0.6)   # per-axis standard deviation limit
    MAX_ACCEL_SPREAD = 0.012         # g

    def __init__(self) -> None:
        self.bias = [0.0, 0.0, 0.0]
        self.enabled = True
        self.calibrated_at = 0.0
        self._reset()

    def _reset(self) -> None:
        self._n = 0
        self._start = 0.0
        self._sum = [0.0] * 3
        self._sq = [0.0] * 3
        self._asum = [0.0] * 3
        self._asq = [0.0] * 3

    def feed(self, t: float, g: tuple, a: tuple) -> None:
        """g: gyro (rad/s) corrected by the factory bias only."""
        if not self.enabled:
            return
        raw = g
        if any(abs(raw[i] - self.bias[i]) > 4 * self.MAX_RATE for i in range(3)):
            self._reset()
            return
        if self._n == 0:
            self._start = t
        self._n += 1
        for i in range(3):
            self._sum[i] += raw[i]
            self._sq[i] += raw[i] * raw[i]
            self._asum[i] += a[i]
            self._asq[i] += a[i] * a[i]
        if t - self._start < self.WINDOW or self._n < 40:
            return
        n = self._n
        mean = [s / n for s in self._sum]
        spread = [math.sqrt(max(0.0, self._sq[i] / n - mean[i] ** 2)) for i in range(3)]
        aspread = [math.sqrt(max(0.0, self._asq[i] / n - (self._asum[i] / n) ** 2)) for i in range(3)]
        if (all(abs(m) < self.MAX_RATE for m in mean) and all(s < self.MAX_SPREAD for s in spread)
                and all(s < self.MAX_ACCEL_SPREAD for s in aspread)):
            self.bias = mean
            self.calibrated_at = t
        self._reset()


class Orientation:
    """Mahony filter: gyro integration corrected towards gravity. For display only."""

    def __init__(self) -> None:
        self.q = [1.0, 0.0, 0.0, 0.0]
        self._t = None

    def update(self, t: float, g: tuple, a: tuple) -> None:
        if self._t is None:
            self._t = t
            return
        dt = min(max(t - self._t, 0.0), 0.05)
        self._t = t
        q0, q1, q2, q3 = self.q
        gx, gy, gz = g
        an = math.sqrt(a[0] ** 2 + a[1] ** 2 + a[2] ** 2)
        if 0.6 < an < 1.4:
            ax, ay, az = a[0] / an, a[1] / an, a[2] / an
            vx = 2 * (q1 * q3 - q0 * q2)
            vy = 2 * (q0 * q1 + q2 * q3)
            vz = q0 * q0 - q1 * q1 - q2 * q2 + q3 * q3
            kp = 1.5
            gx += kp * (ay * vz - az * vy)
            gy += kp * (az * vx - ax * vz)
            gz += kp * (ax * vy - ay * vx)
        h = 0.5 * dt
        q0, q1, q2, q3 = (q0 + (-q1 * gx - q2 * gy - q3 * gz) * h,
                          q1 + (q0 * gx + q2 * gz - q3 * gy) * h,
                          q2 + (q0 * gy - q1 * gz + q3 * gx) * h,
                          q3 + (q0 * gz + q1 * gy - q2 * gx) * h)
        n = math.sqrt(q0 * q0 + q1 * q1 + q2 * q2 + q3 * q3)
        self.q = [q0 / n, q1 / n, q2 / n, q3 / n]

    def reset(self) -> None:
        self.q = [1.0, 0.0, 0.0, 0.0]


class CommandError(Exception):
    pass


class JoyCon2:
    """One connected Switch 2 controller."""

    def __init__(self, mac: str, side: str, host_mac: str, pairing_key: bytes,
                 on_state: Callable[["JoyCon2", State], None],
                 on_disconnect: Callable[["JoyCon2"], None], address_type: str = "public") -> None:
        self.mac = mac.upper()
        self.address_type = address_type
        self.side = side
        self.host_mac = host_mac.upper()
        self.pairing_key = pairing_key
        self.on_state = on_state
        self.on_disconnect = on_disconnect
        self.client = ATTClient(self.mac)
        self.chars: dict[str, object] = {}
        self.state = State()
        self.cal = StickCal()
        self.gyro_bias_factory = (0.0, 0.0, 0.0)
        self.gyro_scale = 34.8 / 32767.0   # rad/s per LSB; see _check_imu_timebase
        self.gyro_cal = GyroCalibrator()
        self.orientation = Orientation()
        self.info: dict = {}
        self.connected = False
        self.ready = False
        self.reports = 0
        self.rate_hz = 0.0
        self.interval_ms = 0.0       # mean time between reports, host clock
        self.jitter_ms = 0.0
        self.gaps = 0                # reports that arrived over 2.5x the mean interval late
        self.worst_gap_ms = 0.0      # longest wait in the last ~2 seconds
        self._gap_window: list[float] = []
        self._last_t = 0.0
        self._last_counter = None
        self._pending: dict[tuple[int, int], list] = {}
        self._pending_lock = threading.Lock()
        self._rumble_seq = 0
        self._imu_base: tuple[float, int] | None = None
        self._imu_checked = False

    # ------------------------------------------------------------------ setup

    def start(self) -> None:
        """Connect and initialise. Blocking; run on a worker thread."""
        t0 = time.monotonic()
        self.client.on_notify = self._on_notify
        self.client.on_disconnect = self._on_link_lost
        # Use the address type the controller advertised with. Creating the
        # connection can stall while another program runs device discovery,
        # so allow it a generous timeout rather than trying other types.
        from .att import BDADDR_LE_PUBLIC, BDADDR_LE_RANDOM
        kind = BDADDR_LE_RANDOM if self.address_type == "random" else BDADDR_LE_PUBLIC
        self.client.connect(timeout=20.0, types=(kind,))
        self.connected = True
        for svc in self.client.discover():
            for ch in svc.chars:
                self.chars[ch.uuid] = ch
        for uuid in (UUID_INPUT, UUID_COMMAND, UUID_RESPONSE):
            if uuid not in self.chars:
                raise RuntimeError(f"characteristic {uuid} missing; not a Switch 2 controller?")
        log.info("%s %s: connected, MTU %d, discovery %.0f ms", self.side, self.mac,
                 self.client.mtu, (time.monotonic() - t0) * 1000)

        if UUID_UNKNOWN_W in self.chars:
            try:
                self.client.write(self.chars[UUID_UNKNOWN_W].value_handle, b"\x01\x00")
            except Exception as e:  # harmless if refused
                log.debug("write 0x0005 refused: %s", e)
        self.client.enable_notify(self.chars[UUID_RESPONSE].cccd)

        self._read_identity()
        try:
            self._ensure_paired()
        except Exception as e:
            log.warning("%s: pairing failed (%s); this session still works, "
                        "but the Joy-Con will need its sync button next time", self.side, e)
        self.command(0x0C, 0x02, bytes([FEATURES, 0, 0, 0]))
        self.command(0x0C, 0x04, bytes([FEATURES, 0, 0, 0]))
        rate_desc = self.chars[UUID_INPUT].descriptors.get(UUID_RATE_DESC)
        if rate_desc:
            try:
                self.client.write(rate_desc, b"\x85\x00")
            except Exception as e:
                log.debug("report-rate descriptor write refused: %s", e)
        self.client.enable_notify(self.chars[UUID_INPUT].cccd)
        self.ready = True
        log.info("%s %s: ready in %.0f ms", self.side, self.mac, (time.monotonic() - t0) * 1000)

    def _read_identity(self) -> None:
        fw = self.command(0x10, 0x01)
        self.info["firmware"] = f"{fw[0]}.{fw[1]}.{fw[2]}" if len(fw) >= 3 else "?"
        factory = self.read_flash(0x13000, 0x40)
        self.info["serial"] = factory[2:0x12].split(b"\0")[0].decode(errors="replace")
        pid = struct.unpack_from("<H", factory, 0x14)[0]
        if pid in SIDE_BY_PID:
            self.side = SIDE_BY_PID[pid]
        self.info["pid"] = f"{pid:04x}"
        rgb = lambda o: "#" + factory[o:o + 3].hex()
        self.info["colors"] = {"body": rgb(0x19), "buttons": rgb(0x1C),
                               "highlight": rgb(0x1F), "grip": rgb(0x22)}
        # Gyro bias (float32 x/y/z, rad/s) follows a 4-byte field at 0x13040.
        g = self.read_flash(0x13040, 0x10)
        self.gyro_bias_factory = struct.unpack_from("<3f", g, 4)
        if not all(math.isfinite(v) and abs(v) < 0.5 for v in self.gyro_bias_factory):
            self.gyro_bias_factory = (0.0, 0.0, 0.0)
        # The controller's own stick is the "primary" calibration at 0x130A8;
        # a user calibration at 0x1FC040 (magic b2 a1) takes precedence.
        cal = StickCal.parse(self.read_flash(0x130A8, 9))
        user = self.read_flash(0x1FC040, 11)
        if user[:2] == b"\xb2\xa1":
            cal = StickCal.parse(user[2:11]) or cal
        if cal:
            self.cal = cal
        self.info["stick_cal"] = {"x": vars(self.cal.x), "y": vars(self.cal.y)}

    def _pairing_entries(self) -> list[str]:
        """Host addresses stored in the controller's pairing table (0x1FA000)."""
        raw = self.read_flash(0x1FA000, 0x40)
        count = raw[0] if raw[0] != 0xFF else 0
        out = []
        for i in range(min(count, 2)):
            a = raw[0x08 + i * 0x28:0x0E + i * 0x28]
            if len(a) == 6:
                out.append(":".join(f"{x:02X}" for x in a))
        return out

    def _ensure_paired(self) -> None:
        ours = self.host_mac
        ours_rev = ":".join(reversed(ours.split(":")))
        entries = self._pairing_entries()
        self.info["paired_hosts"] = entries
        if ours in entries or ours_rev in entries:
            self.info["paired"] = True
            return
        log.info("%s: pairing with this PC (%s)", self.side, ours)
        host = bytes(int(x, 16) for x in reversed(ours.split(":")))   # little-endian
        host2 = bytes([(host[0] - 1) & 0xFF]) + host[1:]             # console sends addr and addr-1
        self.command(0x15, 0x01, b"\x00\x02" + host + host2)
        a1 = bytes(k ^ b for k, b in zip(self.pairing_key, PAIRING_B1))
        resp = self.command(0x15, 0x04, b"\x00" + a1)
        b1 = resp[1:17]
        ltk = bytes(x ^ y for x, y in zip(a1, b1))
        a2 = os.urandom(16)
        resp = self.command(0x15, 0x02, b"\x00" + a2)
        expect = AES.new(ltk[::-1], AES.MODE_ECB).encrypt(a2[::-1])
        if resp[1:17] != expect:
            raise CommandError("controller's key confirmation did not match")
        self.command(0x15, 0x03, b"\x00")
        self.info["paired"] = True
        self.info["paired_hosts"] = self._pairing_entries()
        self.info["ltk"] = ltk.hex()
        log.info("%s: paired", self.side)

    # --------------------------------------------------------------- commands

    def command(self, cmd: int, sub: int, data: bytes = b"", timeout: float = 1.5) -> bytes:
        """Send a command on the command characteristic and wait for its response."""
        key = (cmd, sub)
        slot: list = [threading.Event(), None]
        with self._pending_lock:
            self._pending[key] = slot
        pkt = bytes([cmd, 0x91, 0x01, sub, 0x00, len(data), 0x00, 0x00]) + data
        try:
            for attempt in range(2):
                self.client.write_cmd(self.chars[UUID_COMMAND].value_handle, pkt)
                if slot[0].wait(timeout):
                    return slot[1]
            raise CommandError(f"no response to command {cmd:02x}/{sub:02x}")
        finally:
            with self._pending_lock:
                self._pending.pop(key, None)

    def read_flash(self, addr: int, length: int) -> bytes:
        out = b""
        while len(out) < length:
            n = min(0x4F, length - len(out))
            resp = self.command(0x02, 0x04, bytes([n, 0x7E, 0, 0]) + struct.pack("<I", addr + len(out)))
            out += resp[8:8 + n]
        return out

    def set_leds(self, mask: int, flash: bool = False) -> None:
        self.command(0x09, 0x07, bytes([mask & 0x0F, 0, 0, 0, 0, 0, 0, 0]))
        self.command(0x09, 0x08, bytes([1 if flash else 0, 0, 0, 0]))

    def play_sample(self, sample: int) -> None:
        """Built-in vibration samples: 3 = connect click, 2 = search tone."""
        self.command(0x0A, 0x02, bytes([sample, 0, 0, 0]))

    def rumble(self, hi_freq: int, hi_amp: int, lo_freq: int, lo_amp: int) -> None:
        """Send one HD rumble packet. Amplitudes and frequencies are 10-bit."""
        ch = self.chars.get(UUID_RUMBLE.get(self.side, ""))
        if not ch or not self.connected:
            return
        f = hi_freq & 0x3FF | (hi_amp & 0x3FF) << 10 | (lo_freq & 0x3FF) << 20 | (lo_amp & 0x3FF) << 30
        frame = f.to_bytes(5, "little")
        pkt = bytes([0x00, 0x50 | self._rumble_seq]) + frame * 3
        self._rumble_seq = (self._rumble_seq + 1) & 0x0F
        try:
            self.client.write_cmd(ch.value_handle, pkt + bytes(42 - len(pkt)))
        except OSError:
            pass

    def disconnect(self) -> None:
        self.connected = False
        self.client.close()

    # ---------------------------------------------------------------- reports

    def _on_link_lost(self) -> None:
        self.connected = False
        self.ready = False
        self.on_disconnect(self)

    def _on_notify(self, handle: int, data: bytes) -> None:
        inp = self.chars.get(UUID_INPUT)
        if inp is not None and handle == inp.value_handle:
            self._on_report(data)
            return
        resp = self.chars.get(UUID_RESPONSE)
        if resp is not None and handle == resp.value_handle and len(data) >= 8:
            with self._pending_lock:
                slot = self._pending.get((data[0], data[3]))
            if slot is not None:
                slot[1] = data[8:]
                slot[0].set()

    def _on_report(self, r: bytes) -> None:
        if len(r) < 0x3C:
            return
        t = time.monotonic()
        s = State()
        s.t = t
        s.counter = struct.unpack_from("<I", r, 0)[0]
        s.b4, s.b5, s.b6, s.b7 = r[4], r[5], r[6], r[7]
        if self.side == "right":
            raw = u12_pair(r[13:16])
        else:
            raw = u12_pair(r[10:13])
        s.stick_raw = raw
        s.stick = (max(-1.25, min(1.25, self.cal.x.normalize(raw[0]))),
                   max(-1.25, min(1.25, self.cal.y.normalize(raw[1]))))
        s.mouse = struct.unpack_from("<HHHH", r, 0x10)
        s.battery_mv = struct.unpack_from("<H", r, 0x1F)[0]
        s.charge = r[0x21]
        s.imu_ts = struct.unpack_from("<I", r, 0x2A)[0]
        if s.imu_ts:
            ax, ay, az, gx, gy, gz = struct.unpack_from("<6h", r, 0x30)
            self._check_imu_timebase(t, s.imu_ts)
            k = self.gyro_scale
            fb = self.gyro_bias_factory
            g = (gx * k - fb[0], gy * k - fb[1], gz * k - fb[2])
            self.gyro_cal.feed(t, g, (ax / ACCEL_LSB_PER_G, ay / ACCEL_LSB_PER_G, az / ACCEL_LSB_PER_G))
            rb = self.gyro_cal.bias
            s.gyro = (g[0] - rb[0], g[1] - rb[1], g[2] - rb[2])
            s.accel = (ax / ACCEL_LSB_PER_G, ay / ACCEL_LSB_PER_G, az / ACCEL_LSB_PER_G)
            s.imu_valid = True
        # Link statistics from arrival times. (The first report field is a
        # controller clock on some Joy-Cons and something else on others, so it
        # can't be relied on to count lost reports.)
        if self._last_t:
            dt = (t - self._last_t) * 1000
            if self.interval_ms == 0:
                self.interval_ms = dt
            if dt > 2.5 * self.interval_ms:
                self.gaps += 1
            else:
                self.interval_ms += (dt - self.interval_ms) * 0.02
            self.jitter_ms += (abs(dt - self.interval_ms) - self.jitter_ms) * 0.02
            self.rate_hz = 1000.0 / self.interval_ms if self.interval_ms > 0 else 0.0
            self._gap_window.append(dt)
            if len(self._gap_window) > 260:
                del self._gap_window[:130]
            self.worst_gap_ms = max(self._gap_window[-260:])
        self._last_counter = s.counter
        self._last_t = t
        self.reports += 1
        self.state = s
        self.on_state(self, s)
        if s.imu_valid:
            self.orientation.update(t, s.gyro, s.accel)

    def _check_imu_timebase(self, t: float, ts: int) -> None:
        """Pick the gyro full-scale from the IMU timestamp rate.

        The IMU timestamp normally counts microseconds, and the gyro then reads
        34.8 rad/s full-scale. Some controllers run their IMU timer at another
        rate, and with it a 40 rad/s full-scale. This is the same distinction
        SDL's Switch 2 driver makes."""
        if self._imu_checked:
            return
        if self._imu_base is None:
            self._imu_base = (t, ts)
            return
        dt = t - self._imu_base[0]
        if dt < 1.0:
            return
        rate = ((ts - self._imu_base[1]) & 0xFFFFFFFF) / dt
        self._imu_checked = True
        microseconds = 0.9e6 < rate < 1.1e6
        self.gyro_scale = (34.8 if microseconds else 40.0) / 32767.0
        self.info["imu_timer_hz"] = round(rate)
        log.info("%s: IMU timer %.0f Hz -> gyro full-scale %s rad/s", self.side, rate,
                 "34.8" if microseconds else "40.0")
