#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Serves sample controller state on a socket, so the app can be developed
and photographed without Joy-Cons:

    python3 tools/demo_daemon.py /tmp/joy2con-demo.sock &
    JOY2CON_SOCKET=/tmp/joy2con-demo.sock python3 -m joy2con

Both Joy-Cons appear connected at full rate, a few buttons are held, the
sticks are pushed and the gyro traces slow waves.
"""

import asyncio
import json
import math
import os
import sys
import time


def joycon(side, mac, serial, buttons, stick, accent, t):
    phase = 0 if side == "left" else 1.7
    gyro = [55 * math.sin(t * 2.1 + phase), 30 * math.sin(t * 1.3 + phase), 70 * math.sin(t * 0.9 + phase)]
    half = math.radians(12 * math.sin(t * 0.5 + phase)) / 2
    return {
        "mac": mac, "side": side, "ready": True, "buttons": buttons, "stick": stick,
        "info": {"colors": {"body": "#323232", "buttons": "#aaaaaa", "highlight": accent},
                 "paired": True, "serial": serial, "firmware": "2.1.4"},
        "battery_mv": 3990, "battery_pct": 82, "charge": 0,
        "rate_hz": 133.3, "interval_ms": 7.5, "jitter_ms": 0.4, "worst_gap_ms": 9, "gaps": 0, "reports": 41870,
        "gyro": gyro, "accel": [0.0, 0.0, 1.0], "quat": [math.cos(half), math.sin(half), 0.0, 0.0],
    }


def state(t):
    left = joycon("left", "BC:89:A6:00:00:01", "HBW00000000001", ["ZL"], [0.45, 0.6], "#9be1e6", t)
    right = joycon("right", "BC:89:A6:00:00:02", "HCW00000000002", ["A"], [0.0, 0.0], "#ff8c5f", t)
    return {
        "mode": "pro", "scanning": "off", "foreign_discovery": False, "gyro_autocal": True, "imu_source": "right",
        "joycons": {"left": left, "right": right},
        "known": {left["mac"]: {"side": "left", "mac": left["mac"], "paired": True},
                  right["mac"]: {"side": "right", "mac": right["mac"], "paired": True}},
        "seen": [],
        "emulated": {"pro": {"name": "Pro Controller", "retiring": False, "player_leds": 1,
                             "consumers": [{"name": "steam", "pid": 2412}], "imu": True, "vibration": True,
                             "mode": "30", "rumble": {"left": 0.0, "right": 0.0},
                             "nodes": {"hidraw": ["hidraw7"], "event": [{"node": "/dev/input/event21", "name": "Pro Controller"}]},
                             "reports": 41870, "subcommands": 27}},
    }


EVENTS = [
    {"seq": 1, "t": time.time() - 40, "level": "info", "text": "joy2con started"},
    {"seq": 2, "t": time.time() - 31, "level": "info", "text": "left Joy-Con connected (paired to this PC)"},
    {"seq": 3, "t": time.time() - 29, "level": "info", "text": "right Joy-Con connected (paired to this PC)"},
    {"seq": 4, "t": time.time() - 29, "level": "info", "text": "emulated Pro Controller created"},
]


async def client(reader, writer):
    t0 = time.monotonic()
    writer.write((json.dumps({"type": "events", "events": EVENTS}) + "\n").encode())
    try:
        while True:
            writer.write((json.dumps({"type": "state", "state": state(time.monotonic() - t0)}) + "\n").encode())
            await writer.drain()
            await asyncio.sleep(1 / 60)
    except (ConnectionError, asyncio.CancelledError):
        pass


async def main(path):
    if os.path.exists(path):
        os.unlink(path)
    server = await asyncio.start_unix_server(client, path=path)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/joy2con-demo.sock"))
