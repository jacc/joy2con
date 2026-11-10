# SPDX-License-Identifier: GPL-3.0-or-later
"""joy2con daemon: Switch 2 Joy-Cons as original Switch controllers on Linux.

Run:  joy2con daemon [--verbose]

The daemon watches for Joy-Con 2 advertisements, connects to each controller
(pairing it to this PC the first time, so later a button press reconnects it),
and feeds the halves into emulated original Switch controllers (switch1.py):
one Pro Controller for the pair, or one Joy-Con device per half. A local web
client API on a Unix socket (see Server) feeds the joy2con app.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import glob
import hashlib
import json
import logging
import math
import os
import signal
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from bleak import BleakScanner
from bleak.args.bluez import BlueZScannerArgs, OrPattern
from bleak.assigned_numbers import AdvertisementDataType

from . import ns2, switch1
from .ns2 import JoyCon2
from .switch1 import SwitchController

log = logging.getLogger("joy2con")

CONFIG_PATH = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "joy2con" / "config.json"
NINTENDO = 0x0553
GRACE_S = 4.0           # keep an emulated device this long after its Joy-Cons drop
RUMBLE_PERIOD_S = 0.010  # HD rumble packets are re-sent at this period while active
RUMBLE_MAX = 450         # of 1023; full scale is uncomfortably strong (kernel uses the same cap)


def adapter_address() -> str:
    out = subprocess.run(["busctl", "get-property", "org.bluez", "/org/bluez/hci0",
                          "org.bluez.Adapter1", "Address"], capture_output=True, text=True, check=True)
    return out.stdout.split('"')[1].upper()


def freq_code(hz: float) -> int:
    """Original-Switch rumble frequency -> Switch 2 10-bit frequency code.

    The Switch 2 codes 0x112 and 0x187 are the ones SDL and the kernel use for
    an ERM-style rumble, matching the original Switch defaults of 160 Hz and
    320 Hz; frequencies in between and beyond are placed on the same log scale
    (117 codes per octave)."""
    if hz <= 0:
        return 0x112
    return max(0, min(1023, int(round(0x112 + 117 * math.log2(hz / 160.0)))))


class Rumbler:
    """Streams HD rumble packets to one Joy-Con while the host asks for rumble."""

    def __init__(self, jc: JoyCon2) -> None:
        self.jc = jc
        self.state = (320.0, 0.0, 160.0, 0.0)
        self.until = 0.0          # for timed test rumbles from the UI
        self.cv = threading.Condition()
        self.alive = True
        threading.Thread(target=self._run, name=f"rumble-{jc.side}", daemon=True).start()

    def set(self, state: tuple, duration: float | None = None) -> None:
        with self.cv:
            self.state = state
            self.until = time.monotonic() + duration if duration else 0.0
            self.cv.notify()

    @property
    def level(self) -> float:
        return max(self.state[1], self.state[3])

    def _run(self) -> None:
        sent_zero = True
        while self.alive:
            with self.cv:
                if self.until and time.monotonic() > self.until:
                    self.state, self.until = (320.0, 0.0, 160.0, 0.0), 0.0
                hi_hz, hi_amp, lo_hz, lo_amp = self.state
                active = hi_amp > 0 or lo_amp > 0
                if not active and sent_zero:
                    self.cv.wait()
                    continue
            self.jc.rumble(freq_code(hi_hz), int(hi_amp * RUMBLE_MAX), freq_code(lo_hz), int(lo_amp * RUMBLE_MAX))
            sent_zero = not active
            time.sleep(RUMBLE_PERIOD_S)

    def stop(self) -> None:
        self.alive = False
        with self.cv:
            self.cv.notify()


class Manager:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.config = self._load_config()
        self.host = adapter_address()
        self.key = hashlib.sha256(b"joy2con pairing key " + self.host.encode()).digest()[:16]
        self.joycons: dict[str, JoyCon2] = {}       # side -> connected controller
        self.rumblers: dict[str, Rumbler] = {}
        self.emulated: dict[str, SwitchController] = {}   # "pro" or "left"/"right"
        self.retire: dict[str, float] = {}           # emulated key -> monotonic time to remove
        self.connecting: set[str] = set()
        self.ignore_until: dict[str, float] = {}     # mac -> time; after a user disconnect
        self.takeover: set[str] = set()              # macs paired elsewhere the user chose to take
        self.seen: dict[str, dict] = {}              # advertising controllers
        self.events = collections.deque(maxlen=200)
        self.event_seq = 0
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="cmd")
        self.scan_wake = asyncio.Event()
        self.scanning = "off"
        self.consumers: dict[str, list] = {}
        self.nodes: dict[str, dict] = {}
        self.foreign_discovery = False   # another program is running Bluetooth discovery
        self.event(f"joy2con started; Bluetooth adapter {self.host}")

    # ---------------------------------------------------------------- config

    def _load_config(self) -> dict:
        try:
            cfg = json.loads(CONFIG_PATH.read_text())
        except (OSError, ValueError):
            cfg = {}
        cfg.setdefault("mode", "pro")
        cfg.setdefault("controllers", {})
        cfg.setdefault("gyro_autocal", True)
        cfg.setdefault("imu_source", "right")
        return cfg

    def save_config(self) -> None:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.config, indent=2))
        tmp.replace(CONFIG_PATH)

    def event(self, text: str, level: str = "info") -> None:
        getattr(log, "warning" if level == "warn" else "info")(text)
        self.event_seq += 1
        self.events.append({"seq": self.event_seq, "t": time.time(), "text": text, "level": level})

    # ------------------------------------------------------------ scanning

    def wanted_sides(self) -> set[str]:
        return {"left", "right"} - set(self.joycons)

    def _on_advertisement(self, device, adv) -> None:
        data = adv.manufacturer_data.get(NINTENDO)
        if not data or len(data) < 16:
            return
        pid = int.from_bytes(data[5:7], "little")
        side = ns2.SIDE_BY_PID.get(pid)
        if side not in ("left", "right"):
            return
        mac = device.address.upper()
        host = ":".join(f"{b:02X}" for b in reversed(data[10:16]))
        if host == "00:00:00:00:00:00":
            kind = "sync"
        elif host == self.host:
            kind = "reconnect"
        else:
            kind = "other-host"
        self.seen[mac] = {"mac": mac, "side": side, "kind": kind, "host": host,
                          "rssi": adv.rssi, "t": time.time(),
                          "known": mac in self.config["controllers"]}
        if side in self.joycons or mac in self.connecting:
            return
        if time.monotonic() < self.ignore_until.get(mac, 0):
            return
        if kind == "other-host" and mac not in self.takeover:
            return
        self.connecting.add(mac)
        self.scan_wake.set()   # stop scanning now; see _connect
        addr_type = ((getattr(device, "details", None) or {}).get("props") or {}).get("AddressType", "public")
        self.loop.create_task(self._connect(mac, side, kind, addr_type))

    async def scan_loop(self) -> None:
        passive = True
        failures = 0
        active_since = 0.0
        while True:
            if not passive and time.monotonic() - active_since > 60:
                passive = True   # retry passive scanning; it may have failed only transiently
            if not self.wanted_sides():
                self.scanning = "off"
                self.scan_wake.clear()
                await self.scan_wake.wait()
                continue
            if self.connecting:
                await asyncio.sleep(0.2)
                continue
            try:
                if passive:
                    scanner = BleakScanner(self._on_advertisement, scanning_mode="passive",
                                           bluez=BlueZScannerArgs(or_patterns=[OrPattern(
                                               0, AdvertisementDataType.MANUFACTURER_SPECIFIC_DATA,
                                               b"\x53\x05")]))
                else:
                    scanner = BleakScanner(self._on_advertisement)
                await scanner.start()
                self.scanning = "passive" if passive else "active"
                self.scan_wake.clear()
                # Scan until a connection attempt starts or the set of wanted sides changes.
                while self.wanted_sides() and not self.connecting:
                    try:
                        await asyncio.wait_for(self.scan_wake.wait(), 0.5)
                        self.scan_wake.clear()
                    except asyncio.TimeoutError:
                        pass
                    for mac, s in list(self.seen.items()):
                        if time.time() - s["t"] > 15:
                            del self.seen[mac]
                await scanner.stop()
                failures = 0
            except Exception as e:
                failures += 1
                self.event(f"{'passive' if passive else 'active'} scan error: {e}", "warn")
                if passive and failures >= 3:
                    self.event("falling back to active scanning for a minute", "warn")
                    passive, active_since, failures = False, time.monotonic(), 0
                await asyncio.sleep(2)
            self.scanning = "off"

    async def discovery_watch(self) -> None:
        """Warn while another program runs device discovery.

        Discovery (e.g. a desktop "add device" dialog) holds the radio: new
        connections can't be created and connected controllers lose reports.
        Our own passive scan doesn't set Adapter1.Discovering, so when it is
        set and we aren't actively scanning, someone else is."""
        while True:
            await asyncio.sleep(2)
            try:
                out = await asyncio.to_thread(subprocess.run, ["busctl", "get-property", "org.bluez",
                                              "/org/bluez/hci0", "org.bluez.Adapter1", "Discovering"],
                                              capture_output=True, text=True, timeout=3)
                on = out.stdout.strip() == "b true" and self.scanning != "active"
            except Exception:
                continue
            if on != self.foreign_discovery:
                self.foreign_discovery = on
                if on:
                    self.event("another program is searching for Bluetooth devices (e.g. the desktop's "
                               "'add device' dialog); Joy-Cons can't connect and lose input until it stops", "warn")
                else:
                    self.event("Bluetooth device search by another program stopped")

    # ---------------------------------------------------------- connections

    async def _connect(self, mac: str, side: str, kind: str, addr_type: str = "public") -> None:
        # An LE connection can't be created while discovery holds the
        # controller ("device busy"), so wait for the scanner to stop.
        for _ in range(40):
            if self.scanning == "off":
                break
            await asyncio.sleep(0.05)
        what = {"sync": "in sync mode", "reconnect": "reconnecting", "other-host": "taking over"}[kind]
        self.event(f"{side} Joy-Con {mac} {what}; connecting")
        jc = JoyCon2(mac, side, self.host, self.key, self._on_state, self._on_link_lost, addr_type)
        jc.gyro_cal.enabled = self.config["gyro_autocal"]
        try:
            await asyncio.to_thread(jc.start)
        except Exception as e:
            self.event(f"{side} Joy-Con {mac}: connection failed: {e}", "warn")
            jc.disconnect()
            self.connecting.discard(mac)
            self.ignore_until[mac] = time.monotonic() + 1.0
            self.scan_wake.set()
            return
        self.connecting.discard(mac)
        self.takeover.discard(mac)
        side = jc.side
        with self.lock:
            old = self.joycons.get(side)
            if old is not None and old is not jc:
                old.disconnect()
            self.joycons[side] = jc
            self.rumblers[side] = Rumbler(jc)
        known = self.config["controllers"].setdefault(mac, {})
        known.update(side=side, serial=jc.info.get("serial"), colors=jc.info.get("colors"),
                     firmware=jc.info.get("firmware"), paired=bool(jc.info.get("paired")),
                     last_seen=time.time())
        self.save_config()
        self.seen.pop(mac, None)
        self.event(f"{side} Joy-Con connected ({jc.info.get('serial')}, firmware {jc.info.get('firmware')}"
                   f"{', paired to this PC' if jc.info.get('paired') else ''})")
        self._ensure_emulated()
        self.pool.submit(self._greet, jc)
        self.scan_wake.set()

    def _greet(self, jc: JoyCon2) -> None:
        """Connection feedback like the console's: player LEDs and a click."""
        try:
            emu = self._emulator_for(jc.side)
            leds = emu.player_leds & 0x0F if emu and emu.player_leds else 0x01
            jc.set_leds(leds)
            jc.play_sample(3)
        except Exception as e:
            log.debug("greeting failed: %s", e)

    def _on_link_lost(self, jc: JoyCon2) -> None:
        self.loop.call_soon_threadsafe(self._handle_lost, jc)

    def _handle_lost(self, jc: JoyCon2) -> None:
        with self.lock:
            if self.joycons.get(jc.side) is not jc:
                return
            del self.joycons[jc.side]
            r = self.rumblers.pop(jc.side, None)
            if r:
                r.stop()
            self._release(jc.side)
        self.event(f"{jc.side} Joy-Con disconnected; press any button on it to reconnect"
                   if jc.info.get("paired") else f"{jc.side} Joy-Con disconnected")
        self._schedule_retire()
        self.scan_wake.set()

    def disconnect(self, side: str) -> None:
        jc = self.joycons.get(side)
        if jc:
            self.ignore_until[jc.mac] = time.monotonic() + 8
            jc.disconnect()
            # A link we close ourselves reports no loss, so remove it here.
            self._handle_lost(jc)

    # ------------------------------------------------------- emulated devices

    def _emulated_keys(self) -> set[str]:
        if self.config["mode"] == "pro":
            return {"pro"} if self.joycons else set()
        return set(self.joycons)

    def _pro_mac(self) -> str:
        h = hashlib.sha256(b"joy2con pro " + self.host.encode()).digest()
        return "98:B6:E9:" + ":".join(f"{b:02X}" for b in h[:3])  # Nintendo-style prefix, stable per PC

    def _ensure_emulated(self) -> None:
        with self.lock:
            want = self._emulated_keys()
            for key in want:
                self.retire.pop(key, None)
                if key in self.emulated:
                    self._apply_colors(key)
                    continue
                if key == "pro":
                    kind, mac = "pro", self._pro_mac()
                    any_jc = self.joycons.get("right") or self.joycons.get("left")
                    serial = any_jc.info.get("serial", "") if any_jc else ""
                else:
                    jc = self.joycons[key]
                    kind, mac, serial = ("jcl" if key == "left" else "jcr"), jc.mac, jc.info.get("serial", "")
                try:
                    emu = SwitchController(
                        kind, mac, serial, self._colors(key),
                        on_rumble=lambda block, st, k=key: self._host_rumble(k, block, st),
                        on_leds=lambda on, fl, k=key: self._host_leds(k, on, fl),
                        on_power_off=lambda k=key: self.loop.call_soon_threadsafe(self._host_power_off, k))
                except OSError as e:
                    self.event(f"could not create emulated controller: {e}", "warn")
                    continue
                self.emulated[key] = emu
                self.event(f"emulated {emu.name} created")
                self.loop.call_later(3.0, self._refresh_nodes)

    def _colors(self, key: str) -> dict:
        def c(side, part):
            jc = self.joycons.get(side)
            info = jc.info.get("colors") if jc else None
            if not info:
                for v in self.config["controllers"].values():
                    if v.get("side") == side and v.get("colors"):
                        info = v["colors"]
            return (info or {}).get(part)
        if key == "pro":
            return {"body": c("right", "body") or c("left", "body"),
                    "buttons": c("right", "buttons") or c("left", "buttons"),
                    "grip_left": c("left", "body"), "grip_right": c("right", "body")}
        return {"body": c(key, "body"), "buttons": c(key, "buttons")}

    def _apply_colors(self, key: str) -> None:
        emu = self.emulated.get(key)
        if emu:
            emu.set_colors({k: v for k, v in self._colors(key).items() if v})

    def _schedule_retire(self) -> None:
        with self.lock:
            want = self._emulated_keys()
            for key in list(self.emulated):
                if key not in want and key not in self.retire:
                    self.retire[key] = time.monotonic() + GRACE_S
        self.loop.call_later(GRACE_S + 0.1, self._retire_due)

    def _retire_due(self) -> None:
        with self.lock:
            now = time.monotonic()
            for key, when in list(self.retire.items()):
                if now >= when and key not in self._emulated_keys():
                    emu = self.emulated.pop(key, None)
                    del self.retire[key]
                    if emu:
                        emu.close()
                        self.event(f"emulated {emu.name} removed")

    def set_mode(self, mode: str) -> None:
        if mode not in ("pro", "joycons") or mode == self.config["mode"]:
            return
        with self.lock:
            for emu in self.emulated.values():
                emu.close()
            self.emulated.clear()
            self.retire.clear()
            self.config["mode"] = mode
            self.save_config()
        self.event("mode: " + ("one Pro Controller for both Joy-Cons" if mode == "pro" else "separate Joy-Con devices"))
        self._ensure_emulated()

    def _emulator_for(self, side: str) -> SwitchController | None:
        return self.emulated.get("pro") or self.emulated.get(side)

    # --------------------------------------------------------- input routing

    def _on_state(self, jc: JoyCon2, s: ns2.State) -> None:
        """Runs on the Joy-Con's socket thread for every report."""
        mode = self.config["mode"]
        if mode == "pro":
            emu = self.emulated.get("pro")
            if emu is None:
                return
            left, right = self.joycons.get("left"), self.joycons.get("right")
            ls = left.state if left and left.ready else None
            rs = right.state if right and right.ready else None
            i = emu.inputs
            i.right = (rs.b4 if rs else 0) | (ls.b4 if ls else 0)
            i.shared = (rs.b5 if rs else 0) | (ls.b5 if ls else 0)
            i.left = (ls.b6 if ls else 0) | (rs.b6 if rs else 0)
            i.lstick = ls.stick if ls else (0.0, 0.0)
            i.rstick = rs.stick if rs else (0.0, 0.0)
            pref = self.config["imu_source"]
            src = (rs if pref == "right" else ls) or rs or ls
            if src is not None and src.imu_valid:
                i.accel, i.gyro = to_switch1("pro", src.accel, src.gyro)
            mvs = [x for x in (ls, rs) if x is not None and x.battery_mv]
            if mvs:
                low = min(mvs, key=lambda x: x.battery_mv)
                i.battery = switch1.battery_nibble(low.battery_mv, low.charge not in (0, 0x20))
            emu.push()
        else:
            emu = self.emulated.get(jc.side)
            if emu is None:
                return
            i = emu.inputs
            i.right, i.shared, i.left = s.b4, s.b5, s.b6
            if jc.side == "left":
                i.lstick, i.rstick = s.stick, (0.0, 0.0)
            else:
                i.lstick, i.rstick = (0.0, 0.0), s.stick
            if s.imu_valid:
                i.accel, i.gyro = to_switch1("jcl" if jc.side == "left" else "jcr", s.accel, s.gyro)
            if s.battery_mv:
                i.battery = switch1.battery_nibble(s.battery_mv, s.charge not in (0, 0x20))
            emu.push()

    def _release(self, side: str) -> None:
        """Neutral inputs for a half that disconnected."""
        if self.config["mode"] == "pro":
            emu = self.emulated.get("pro")
            if emu:
                other = self.joycons.get("left" if side == "right" else "right")
                if other is not None:
                    self._on_state(other, other.state)
                else:
                    emu.inputs = switch1.Inputs()
                    emu.push()

    def _host_rumble(self, key: str, block: str, state: tuple) -> None:
        """A host sent rumble to an emulated controller (uhid thread)."""
        if key == "pro":
            side = block
        elif (key == "left") != (block == "left"):
            return  # the other half's block of a single Joy-Con
        else:
            side = key
        r = self.rumblers.get(side)
        if r:
            r.set(state)

    def _host_leds(self, key: str, on: int, flash: int) -> None:
        sides = ["left", "right"] if key == "pro" else [key]
        for side in sides:
            jc = self.joycons.get(side)
            if jc and jc.ready:
                self.pool.submit(_safe, jc.set_leds, on | flash, bool(flash))

    def _host_power_off(self, key: str) -> None:
        self.event("host asked the controller to power off")
        for side in (["left", "right"] if key == "pro" else [key]):
            self.disconnect(side)

    # ------------------------------------------------------- device nodes

    def _refresh_nodes(self) -> None:
        nodes = {}
        for key, emu in self.emulated.items():
            info = {"hidraw": [], "event": [], "sysfs": None}
            for d in glob.glob("/sys/bus/hid/devices/0005:057E:*"):
                try:
                    ue = Path(d, "uevent").read_text()
                except OSError:
                    continue
                if f"HID_PHYS={emu.phys}" not in ue:
                    continue
                info["sysfs"] = d
                info["hidraw"] = ["/dev/" + os.path.basename(p) for p in glob.glob(d + "/hidraw/hidraw*")]
                for ev in glob.glob(d + "/input/input*/event*"):
                    try:
                        name = Path(os.path.dirname(ev), "name").read_text().strip()
                    except OSError:
                        name = "?"
                    info["event"].append({"node": "/dev/input/" + os.path.basename(ev), "name": name})
                leds = sorted(os.path.basename(p) for p in glob.glob(d + "/leds/*"))
                info["leds"] = leds
                for ps in glob.glob(d + "/power_supply/*"):
                    info["power_supply"] = os.path.basename(ps)
            nodes[key] = info
        self.nodes = nodes

    def refresh_consumers(self) -> None:
        """Which processes have the emulated controllers' nodes open."""
        self._refresh_nodes()
        wanted = {}
        for key, info in self.nodes.items():
            for n in info["hidraw"]:
                wanted[n] = key
            for e in info["event"]:
                wanted[e["node"]] = key
        found: dict[str, dict[int, dict]] = {k: {} for k in self.nodes}
        if wanted:
            me = os.getpid()
            for pid_dir in glob.glob("/proc/[0-9]*"):
                pid = int(os.path.basename(pid_dir))
                if pid == me:
                    continue
                try:
                    fds = os.listdir(pid_dir + "/fd")
                except OSError:
                    continue
                for fd in fds:
                    try:
                        target = os.readlink(f"{pid_dir}/fd/{fd}")
                    except OSError:
                        continue
                    key = wanted.get(target)
                    if key:
                        try:
                            comm = Path(pid_dir, "comm").read_text().strip()
                        except OSError:
                            comm = "?"
                        entry = found[key].setdefault(pid, {"pid": pid, "name": comm, "nodes": []})
                        entry["nodes"].append(target)
        self.consumers = {k: list(v.values()) for k, v in found.items()}

    # -------------------------------------------------------------- status

    def snapshot(self) -> dict:
        out = {"host": self.host, "mode": self.config["mode"], "scanning": self.scanning,
               "foreign_discovery": self.foreign_discovery,
               "gyro_autocal": self.config["gyro_autocal"], "imu_source": self.config["imu_source"],
               "joycons": {}, "emulated": {}, "seen": list(self.seen.values()),
               "known": self.config["controllers"]}
        for side, jc in list(self.joycons.items()):
            s = jc.state
            r = self.rumblers.get(side)
            out["joycons"][side] = {
                "mac": jc.mac, "ready": jc.ready, "info": {k: v for k, v in jc.info.items() if k != "ltk"},
                "buttons": s.pressed(), "stick": s.stick, "stick_raw": s.stick_raw,
                "accel": s.accel, "gyro": [math.degrees(v) for v in s.gyro],
                "quat": jc.orientation.q, "mouse": s.mouse,
                "battery_mv": s.battery_mv, "battery_pct": switch1.battery_percent(s.battery_mv),
                "charge": s.charge, "rate_hz": jc.rate_hz, "interval_ms": jc.interval_ms,
                "jitter_ms": jc.jitter_ms, "gaps": jc.gaps, "reports": jc.reports,
                "worst_gap_ms": jc.worst_gap_ms,
                "gyro_bias": [math.degrees(v) for v in jc.gyro_cal.bias],
                "gyro_cal_age": (time.monotonic() - jc.gyro_cal.calibrated_at) if jc.gyro_cal.calibrated_at else None,
                "rumble": r.level if r else 0.0,
            }
        for key, emu in list(self.emulated.items()):
            out["emulated"][key] = {
                "name": emu.name, "kind": emu.kind, "mac": emu.mac, "mode": f"{emu.mode:02x}",
                "imu": emu.imu_enabled, "vibration": emu.vibration_enabled,
                "player_leds": emu.player_leds, "reports": emu.reports, "subcommands": emu.subcommands,
                "rumble": {k: v[1] + v[3] for k, v in emu.rumble_state.items()},
                "nodes": self.nodes.get(key, {}), "consumers": self.consumers.get(key, []),
                "retiring": key in self.retire,
            }
        return out

    # ------------------------------------------------------------- actions

    def action(self, name: str, args: dict) -> dict:
        side = args.get("side")
        jc = self.joycons.get(side) if side else None
        if name == "rumble" and side in self.rumblers:
            amp = float(args.get("amp", 0.5))
            self.rumblers[side].set((320.0, amp, 160.0, amp), float(args.get("seconds", 0.4)))
        elif name == "sample" and jc:
            self.pool.submit(_safe, jc.play_sample, int(args.get("id", 3)))
        elif name == "leds" and jc:
            self.pool.submit(_safe, jc.set_leds, int(args.get("mask", 1)), bool(args.get("flash")))
        elif name == "disconnect" and side:
            self.disconnect(side)
        elif name == "mode":
            self.set_mode(args.get("mode"))
        elif name == "gyro_autocal":
            self.config["gyro_autocal"] = bool(args.get("enabled"))
            self.save_config()
            for j in self.joycons.values():
                j.gyro_cal.enabled = self.config["gyro_autocal"]
        elif name == "imu_source" and args.get("source") in ("left", "right"):
            self.config["imu_source"] = args["source"]
            self.save_config()
        elif name == "reset_orientation":
            for j in self.joycons.values():
                j.orientation.reset()
        elif name == "takeover" and args.get("mac"):
            self.takeover.add(args["mac"].upper())
            self.scan_wake.set()
        elif name == "forget" and args.get("mac"):
            self.config["controllers"].pop(args["mac"].upper(), None)
            self.save_config()
        else:
            return {"ok": False, "error": f"unknown action or controller: {name}"}
        return {"ok": True}

    def shutdown(self) -> None:
        for r in self.rumblers.values():
            r.stop()
        for jc in list(self.joycons.values()):
            jc.disconnect()
        for emu in self.emulated.values():
            emu.close()


def _safe(fn, *a) -> None:
    try:
        fn(*a)
    except Exception as e:
        log.debug("%s failed: %s", getattr(fn, "__name__", fn), e)


def to_switch1(kind: str, accel_g: tuple, gyro_rad: tuple) -> tuple[tuple, tuple]:
    """Switch 2 controller axes -> original-controller axes and LSB units.

    In SDL's common frame a Switch 2 controller reads (x, z, -y) and an
    original Pro Controller or left Joy-Con reads (-y, z, -x); an original
    right Joy-Con additionally negates the first two components. Solving for
    the original controller's raw axes gives the mappings below."""
    ax, ay, az = accel_g
    gx, gy, gz = gyro_rad
    if kind == "jcr":
        a = (ay, ax, -az)
        g = (gy, gx, -gz)
    else:
        a = (ay, -ax, az)
        g = (gy, -gx, gz)
    clamp = lambda v: max(-32768, min(32767, int(round(v))))
    return (tuple(clamp(v * switch1.ACCEL_LSB_PER_G) for v in a),
            tuple(clamp(v * switch1.RAD_TO_LSB) for v in g))


# ------------------------------------------------------------ client API

def socket_path() -> Path:
    """The daemon's socket; JOY2CON_SOCKET overrides it (tools/demo_daemon.py uses that)."""
    if os.environ.get("JOY2CON_SOCKET"):
        return Path(os.environ["JOY2CON_SOCKET"])
    return Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "joy2con.sock"


class Server:
    """Newline-delimited JSON over a Unix socket, readable only by this user.

    Clients receive {"type": "state", ...} snapshots at 60 Hz (with any new
    log events attached) and send {"action": name, "args": {...}} requests,
    each answered by {"type": "result", ...}."""

    def __init__(self, mgr: Manager) -> None:
        self.mgr = mgr
        self.clients: set[asyncio.StreamWriter] = set()

    async def start(self) -> None:
        path = socket_path()
        try:   # a socket someone answers on means the driver is already running
            _, w = await asyncio.open_unix_connection(str(path))
            w.close()
            raise SystemExit(f"joy2con is already running (socket {path})")
        except (FileNotFoundError, ConnectionRefusedError):
            path.unlink(missing_ok=True)
        old = os.umask(0o177)
        try:
            self.server = await asyncio.start_unix_server(self._client, path=str(path))
        finally:
            os.umask(old)
        self.inode = path.stat().st_ino
        log.info("listening on %s", path)

    def remove_socket(self) -> None:
        """Delete the socket file, unless it now belongs to another instance."""
        path = socket_path()
        try:
            if path.stat().st_ino == self.inode:
                path.unlink()
        except (FileNotFoundError, AttributeError):
            pass

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.clients.add(writer)
        try:
            writer.write((json.dumps({"type": "events", "events": list(self.mgr.events)}) + "\n").encode())
            while line := await reader.readline():
                try:
                    m = json.loads(line)
                    res = self.mgr.action(m.get("action", ""), m.get("args", {}))
                except Exception as e:
                    res = {"ok": False, "error": str(e)}
                writer.write((json.dumps({"type": "result", "result": res}) + "\n").encode())
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self.clients.discard(writer)
            writer.close()

    async def broadcast(self) -> None:
        last_seq = self.mgr.event_seq
        last_consumers = 0.0
        while True:
            await asyncio.sleep(1 / 60)
            if not self.clients:
                continue
            if time.monotonic() - last_consumers > 2:
                last_consumers = time.monotonic()
                await asyncio.to_thread(self.mgr.refresh_consumers)
            msg = {"type": "state", "state": self.mgr.snapshot()}
            if self.mgr.event_seq != last_seq:
                msg["events"] = [e for e in self.mgr.events if e["seq"] > last_seq]
                last_seq = self.mgr.event_seq
            data = (json.dumps(msg) + "\n").encode()
            for w in list(self.clients):
                if w.transport.get_write_buffer_size() > 1 << 20:
                    continue   # a stalled client; skip rather than queue without bound
                w.write(data)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="joy2con daemon", description="Switch 2 Joy-Con driver daemon")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("bleak", "dbus_fast"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    async def run() -> None:
        loop = asyncio.get_running_loop()
        mgr = Manager(loop)
        server = Server(mgr)
        await server.start()
        tasks = [asyncio.create_task(c) for c in (mgr.scan_loop(), mgr.discovery_watch(), server.broadcast())]
        stop = asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await stop.wait()
        log.info("stopping")
        for t in tasks:
            t.cancel()
        mgr.shutdown()
        server.remove_socket()

    asyncio.run(run())
    # Worker threads may still be blocked in a connection attempt; everything
    # that matters (uhid devices, Bluetooth links) is closed, so don't wait.
    logging.shutdown()
    os._exit(0)
