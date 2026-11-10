# SPDX-License-Identifier: GPL-3.0-or-later
"""Emulation of an original Switch controller over uhid.

The emulated device speaks the original Switch Bluetooth HID protocol: full
input reports (0x30), subcommand replies (0x21), simple reports (0x3F), and it
answers the subcommands a host sends (device info, SPI flash reads, report
mode, IMU and rumble enable, player LEDs...). Linux's hid-nintendo driver,
SDL's HIDAPI driver, Steam and Wine all support these controllers, so the
Switch 2 Joy-Cons become usable everywhere with gyro, rumble and LEDs.

Values are canonicalised before they reach the emulated device: sticks are
calibrated by us and published with a fixed calibration (centre 2048,
reach 1600), and motion is bias-corrected, rotated into the original
controller's axes and published with the default IMU calibration. The device
therefore looks identical whichever Joy-Con halves are attached.
"""

from __future__ import annotations

import logging
import math
import struct
import threading
from typing import Callable

from .uhid import BUS_BLUETOOTH, UHIDDevice

log = logging.getLogger("joy2con.switch1")

KINDS = {
    # kind: product id, device name, device-info type, connection-info nibble
    "pro": (0x2009, "Pro Controller", 0x03, 0x00),
    "jcl": (0x2006, "Joy-Con (L)", 0x01, 0x0E),
    "jcr": (0x2007, "Joy-Con (R)", 0x02, 0x0E),
}

STICK_CENTER = 2048
STICK_REACH = 1600
GYRO_DPS_PER_LSB = 936.0 / 13371.0     # default gyro calibration of the original controllers
RAD_TO_LSB = (180.0 / math.pi) / GYRO_DPS_PER_LSB
ACCEL_LSB_PER_G = 4096.0

# Bluetooth HID report descriptor with the report IDs and sizes of the original
# controllers: vendor-defined 48-byte input reports 0x21/0x30 and 361-byte
# 0x31-0x33, the gamepad-style simple report 0x3F, and 48-byte output reports
# 0x01/0x10/0x11/0x12.
DESCRIPTOR = bytes.fromhex("".join([
    "05 01 09 05 a1 01",                    # Generic Desktop / Game Pad, Collection (Application)
    "06 01 ff 15 00 26 ff 00 75 08",        # vendor page, bytes 0..255
    "85 21 09 21 95 30 81 02",              # input 0x21, 48 bytes
    "85 30 09 30 95 30 81 02",              # input 0x30, 48 bytes
    "85 31 09 31 96 69 01 81 02",           # input 0x31, 361 bytes
    "85 32 09 32 96 69 01 81 02",           # input 0x32
    "85 33 09 33 96 69 01 81 02",           # input 0x33
    "85 3f 05 09 19 01 29 10 15 00 25 01 75 01 95 10 81 02",  # input 0x3F: 16 buttons
    "05 01 09 39 15 00 25 07 75 04 95 01 81 42",              # hat switch with null state
    "75 04 95 01 81 01",                    # padding
    "09 30 09 31 09 33 09 34 16 00 00 27 ff ff 00 00 75 10 95 04 81 02",  # X Y Rx Ry
    "06 01 ff 15 00 26 ff 00 75 08",
    "85 01 09 01 95 30 91 02",              # output 0x01 (rumble + subcommand)
    "85 10 09 10 95 30 91 02",              # output 0x10 (rumble only)
    "85 11 09 11 95 30 91 02",              # output 0x11 (NFC/IR MCU)
    "85 12 09 12 95 30 91 02",              # output 0x12
    "c0",
]).replace(" ", ""))


def _amplitude_table() -> list[float]:
    """Linear amplitude (0..1) for each of the 101 HD-rumble amplitude codes.

    The codes follow three geometric segments; anchor values are the known
    amplitudes at the segment ends, interpolated log-linearly between them."""
    anchors = [(1, 514), (2, 775), (15, 7372), (16, 7698), (31, 14744), (32, 15067), (100, 65535)]
    table = [0.0] * 101
    for (i0, a0), (i1, a1) in zip(anchors, anchors[1:]):
        for i in range(i0, i1 + 1):
            f = (i - i0) / (i1 - i0)
            table[i] = math.exp(math.log(a0) + f * (math.log(a1) - math.log(a0))) / 65535.0
    return table


AMPLITUDE = _amplitude_table()


def decode_rumble(b: bytes) -> tuple[float, float, float, float]:
    """Original-Switch HD rumble block (4 bytes) -> (hi Hz, hi amp, lo Hz, lo amp)."""
    hf = b[0] | (b[1] & 0x01) << 8
    hi_idx = min(100, b[1] >> 1)
    lf = b[2] & 0x7F
    lo_idx = min(100, max(0, ((b[3] - 0x40) << 1) | (b[2] >> 7)))
    hi_hz = 10.0 * 2 ** ((hf / 4 + 0x60) / 32)
    lo_hz = 10.0 * 2 ** ((lf + 0x40) / 32)
    return hi_hz, AMPLITUDE[hi_idx], lo_hz, AMPLITUDE[lo_idx]


def pack12(a: int, b: int) -> bytes:
    a &= 0xFFF
    b &= 0xFFF
    return bytes([a & 0xFF, (a >> 8) | (b & 0x0F) << 4, b >> 4])


def stick_bytes(x: float, y: float) -> bytes:
    """Calibrated stick (-1..1) -> packed 12-bit values in the published calibration."""
    xi = int(round(STICK_CENTER + max(-1.27, min(1.27, x)) * STICK_REACH))
    yi = int(round(STICK_CENTER + max(-1.27, min(1.27, y)) * STICK_REACH))
    return pack12(max(0, min(4095, xi)), max(0, min(4095, yi)))


def battery_nibble(mv: int, charging: bool) -> int:
    """Battery byte high nibble: level 0/2/4/6/8 plus charging bit."""
    pct = battery_percent(mv)
    level = 8 if pct > 70 else 6 if pct > 45 else 4 if pct > 20 else 2 if pct > 5 else 0
    return level | (1 if charging else 0)


# Charge left in a single lithium-ion cell at a given voltage under the
# Joy-Con's light load: (mV, percent), interpolated linearly in between.
_DISCHARGE = ((3300, 0), (3450, 5), (3600, 10), (3680, 20), (3730, 30), (3770, 40),
              (3810, 50), (3860, 60), (3920, 70), (3990, 80), (4070, 90), (4150, 100))


def battery_percent(mv: int) -> int:
    if mv <= 0:
        return 0
    pts = _DISCHARGE
    if mv <= pts[0][0]:
        return 0
    for (v0, p0), (v1, p1) in zip(pts, pts[1:]):
        if mv <= v1:
            return round(p0 + (p1 - p0) * (mv - v0) / (v1 - v0))
    return 100


class Inputs:
    """What the emulated controller currently reports."""

    def __init__(self) -> None:
        self.right = 0          # button byte 0 (right side)
        self.shared = 0         # button byte 1
        self.left = 0           # button byte 2 (left side)
        self.lstick = (0.0, 0.0)
        self.rstick = (0.0, 0.0)
        self.accel = (0, 0, 0)  # original-controller axes, LSB
        self.gyro = (0, 0, 0)
        self.battery = 0x8


class SwitchController:
    """One emulated original-Switch controller."""

    def __init__(self, kind: str, mac: str, serial: str, colors: dict,
                 on_rumble: Callable[[str, tuple], None],
                 on_leds: Callable[[int, int], None],
                 on_power_off: Callable[[], None]) -> None:
        self.kind = kind
        pid, name, self.ctype, self.con_info = KINDS[kind]
        self.name = name
        self.mac = mac.upper()
        self.on_rumble = on_rumble
        self.on_leds = on_leds
        self.on_power_off = on_power_off
        self.inputs = Inputs()
        self.mode = 0x3F
        self.imu_enabled = False
        self.vibration_enabled = False
        self.player_leds = 0
        self.timer = 0
        self.lock = threading.Lock()
        self.rumble_state = {"left": (0.0, 0.0, 0.0, 0.0), "right": (0.0, 0.0, 0.0, 0.0)}
        self.subcommands = 0
        self.reports = 0
        self.spi = self._build_spi(serial, colors)
        self.phys = f"joy2con/{kind}/{self.mac.lower()}"
        self.dev = UHIDDevice(
            name=name, phys=self.phys, uniq=self.mac.lower(), bus=BUS_BLUETOOTH,
            vendor=0x057E, product=pid, version=0x0001, descriptor=DESCRIPTOR,
            on_output=self._on_output)
        log.info("created emulated %s (%s)", name, self.phys)

    # ------------------------------------------------------------ SPI flash

    def _build_spi(self, serial: str, colors: dict) -> bytearray:
        spi = bytearray(b"\xff" * 0x80000)
        ser = serial.encode()[:16]
        spi[0x6000:0x6010] = ser + bytes(16 - len(ser))
        spi[0x6012] = self.ctype
        spi[0x6013] = 0xA0
        spi[0x601B] = 0x02 if self.kind == "pro" else 0x01
        # Default IMU calibration: zero offsets, 1 g = 4096 (sens 0x4000 over a
        # 4 g span), gyro sens 13371 (0.07 dps per LSB).
        spi[0x6020:0x6038] = struct.pack("<12h", 0, 0, 0, 0x4000, 0x4000, 0x4000,
                                         0, 0, 0, 0x343B, 0x343B, 0x343B)
        c, r = STICK_CENTER, STICK_REACH
        # Left stick: [above centre][centre][below centre]; right: [centre][below][above].
        spi[0x603D:0x6046] = pack12(r, r) + pack12(c, c) + pack12(r, r)
        spi[0x6046:0x604F] = pack12(c, c) + pack12(r, r) + pack12(r, r)
        rgb = lambda h: bytes.fromhex(h.lstrip("#")) if h else b"\x32\x32\x32"
        body = rgb(colors.get("body"))
        spi[0x6050:0x6053] = body
        spi[0x6053:0x6056] = rgb(colors.get("buttons"))
        spi[0x6056:0x6059] = rgb(colors.get("grip_left") or colors.get("body"))
        spi[0x6059:0x605C] = rgb(colors.get("grip_right") or colors.get("body"))
        # Six-axis horizontal offsets and stick parameters (dead zone, range
        # ratio) with the values original controllers ship with.
        spi[0x6080:0x6086] = bytes.fromhex("50fd0000c60f")
        params = bytes.fromhex("0f30619630f3d41454411554c7799c333663")
        spi[0x6086:0x6098] = params
        spi[0x6098:0x60AA] = params
        return spi

    def set_colors(self, colors: dict) -> None:
        rgb = lambda h: bytes.fromhex(h.lstrip("#"))
        with self.lock:
            if colors.get("body"):
                self.spi[0x6050:0x6053] = rgb(colors["body"])
            if colors.get("buttons"):
                self.spi[0x6053:0x6056] = rgb(colors["buttons"])
            if colors.get("grip_left"):
                self.spi[0x6056:0x6059] = rgb(colors["grip_left"])
            if colors.get("grip_right"):
                self.spi[0x6059:0x605C] = rgb(colors["grip_right"])

    # ---------------------------------------------------------------- input

    def _header(self) -> bytes:
        i = self.inputs
        self.timer = (self.timer + 1) & 0xFF
        bat = (i.battery << 4) | self.con_info
        return (bytes([self.timer, bat, i.right, i.shared, i.left])
                + stick_bytes(*i.lstick) + stick_bytes(*i.rstick) + b"\x09")

    def push(self) -> None:
        """Send the current inputs as one input report in the active mode."""
        with self.lock:
            if self.mode == 0x3F:
                report = self._simple_report()
            else:
                i = self.inputs
                if self.imu_enabled:
                    sample = struct.pack("<6h", *i.accel, *i.gyro)
                else:
                    sample = bytes(12)
                # Three samples per report, oldest first; each report carries the
                # newest reading so no motion data waits for the next report.
                report = b"\x30" + self._header() + sample * 3
                if self.mode in (0x31, 0x32, 0x33):
                    report = bytes([self.mode]) + report[1:] + bytes(362 - len(report))
            self.reports += 1
        self.dev.send_input(report)

    def _simple_report(self) -> bytes:
        i = self.inputs
        b = 0
        # Simple-report button order: B A Y X L R ZL ZR - + LS RS Home Capture
        bits = [(i.right, 0x04), (i.right, 0x08), (i.right, 0x01), (i.right, 0x02),
                (i.left, 0x40), (i.right, 0x40), (i.left, 0x80), (i.right, 0x80),
                (i.shared, 0x01), (i.shared, 0x02), (i.shared, 0x08), (i.shared, 0x04),
                (i.shared, 0x10), (i.shared, 0x20)]
        for n, (byte, mask) in enumerate(bits):
            if byte & mask:
                b |= 1 << n
        up, down = i.left & 0x02, i.left & 0x01
        right, left = i.left & 0x04, i.left & 0x08
        hats = {(1, 0, 0, 0): 0, (1, 1, 0, 0): 1, (0, 1, 0, 0): 2, (0, 1, 1, 0): 3,
                (0, 0, 1, 0): 4, (0, 0, 1, 1): 5, (0, 0, 0, 1): 6, (1, 0, 0, 1): 7}
        hat = hats.get((bool(up), bool(right), bool(down), bool(left)), 8)
        ax = lambda v, inv=False: max(0, min(0xFFFF, int(0x8000 + (-v if inv else v) * 0x7FFF)))
        return (b"\x3f" + struct.pack("<HB", b, hat)
                + struct.pack("<4H", ax(i.lstick[0]), ax(i.lstick[1], True),
                              ax(i.rstick[0]), ax(i.rstick[1], True)))

    # --------------------------------------------------------------- output

    def _on_output(self, data: bytes) -> None:
        if not data:
            return
        rid = data[0]
        if rid in (0x01, 0x10) and len(data) >= 10:
            self._rumble(data[2:10])
        if rid == 0x01 and len(data) >= 11:
            self._subcommand(data[10], data[11:])

    def _rumble(self, block: bytes) -> None:
        for side, chunk in (("left", block[0:4]), ("right", block[4:8])):
            state = decode_rumble(chunk)
            if state != self.rumble_state[side]:
                self.rumble_state[side] = state
                self.on_rumble(side, state)

    def _reply(self, ack: int, sub: int, data: bytes = b"") -> None:
        with self.lock:
            report = b"\x21" + self._header() + bytes([ack, sub]) + data
        report = report[:49] + bytes(max(0, 49 - len(report)))
        self.dev.send_input(report)

    def _subcommand(self, sub: int, args: bytes) -> None:
        self.subcommands += 1
        a0 = args[0] if args else 0
        if sub == 0x02:      # device info
            mac = bytes(int(x, 16) for x in self.mac.split(":"))
            self._reply(0x82, sub, bytes([0x04, 0x21, self.ctype, 0x02]) + mac + b"\x01\x01")
        elif sub == 0x03:    # input report mode
            with self.lock:
                self.mode = a0
            log.debug("%s: report mode %02x", self.name, a0)
            self._reply(0x80, sub)
        elif sub == 0x04:    # trigger buttons elapsed time
            self._reply(0x83, sub, bytes(14))
        elif sub == 0x10:    # SPI flash read
            addr, size = struct.unpack_from("<IB", args.ljust(5, b"\0"))
            size = min(size, 0x1D)
            chunk = bytes(self.spi[addr:addr + size]) if addr + size <= len(self.spi) else bytes(size)
            self._reply(0x90, sub, struct.pack("<IB", addr, size) + chunk)
        elif sub == 0x11:    # SPI flash write (user calibration areas)
            addr, size = struct.unpack_from("<IB", args.ljust(5, b"\0"))
            if 0x8000 <= addr < 0x9000:
                with self.lock:
                    self.spi[addr:addr + size] = args[5:5 + size]
            self._reply(0x80, sub, b"\x00")
        elif sub == 0x12:    # SPI sector erase
            addr = struct.unpack_from("<I", args.ljust(4, b"\0"))[0]
            if 0x8000 <= addr < 0x9000:
                with self.lock:
                    base = addr & ~0xFFF
                    self.spi[base:base + 0x1000] = b"\xff" * 0x1000
            self._reply(0x80, sub, b"\x00")
        elif sub == 0x21:    # NFC/IR MCU configuration
            self._reply(0xA0, sub, bytes([0x01, 0x00, 0xFF, 0x00, 0x08, 0x00, 0x1B, 0x01]) + bytes(26))
        elif sub == 0x30:    # player lights: low nibble on, high nibble flashing
            self.player_leds = a0
            self.on_leds(a0 & 0x0F, a0 >> 4)
            self._reply(0x80, sub)
        elif sub == 0x31:    # get player lights
            self._reply(0xB0, sub, bytes([self.player_leds]))
        elif sub == 0x40:    # IMU enable
            with self.lock:
                self.imu_enabled = bool(a0)
            self._reply(0x80, sub)
        elif sub == 0x43:    # IMU register read
            count = args[1] if len(args) > 1 else 1
            self._reply(0xC0, sub, bytes([a0, count]) + bytes(count))
        elif sub == 0x48:    # vibration enable
            self.vibration_enabled = bool(a0)
            self._reply(0x80, sub)
        elif sub == 0x50:    # regulated voltage
            self._reply(0xD0, sub, struct.pack("<H", 0x0618))
        elif sub == 0x06 and a0 == 0x00:   # HCI state: power off
            self._reply(0x80, sub)
            self.on_power_off()
        else:                # everything else is acknowledged without data
            self._reply(0x80, sub)

    def close(self) -> None:
        self.dev.close()
        log.info("removed emulated %s", self.name)
