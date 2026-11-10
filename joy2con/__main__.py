# SPDX-License-Identifier: GPL-3.0-or-later
"""joy2con command line.

  joy2con            open the app (window and tray icon)
  joy2con daemon     run the driver (normally started by systemd --user)
  joy2con setup      one-time Bluetooth configuration (asks for your password)
  joy2con status     print what the driver sees
"""

import json
import socket
import subprocess
import sys
from pathlib import Path


def status() -> int:
    from .daemon import socket_path
    try:
        s = socket.socket(socket.AF_UNIX)
        s.connect(str(socket_path()))
    except OSError:
        print("joy2con driver is not running (systemctl --user start joy2con)")
        return 1
    f = s.makefile("rb")
    for line in f:
        msg = json.loads(line)
        if msg.get("type") == "state":
            st = msg["state"]
            print(f"mode: {st['mode']}, scanning: {st['scanning']}")
            for side, j in st["joycons"].items():
                print(f"  {side}: {j['mac']} {j['rate_hz']:.0f} Hz, battery {j['battery_pct']}%, "
                      f"{'paired' if j['info'].get('paired') else 'not paired'}")
            for e in st["emulated"].values():
                users = ", ".join(c["name"] for c in e["consumers"]) or "nobody"
                print(f"  emulated {e['name']}: used by {users}")
            return 0
    return 1


def main() -> int:
    args = sys.argv[1:]
    cmd = args[0] if args and not args[0].startswith("-") else "app"
    rest = args[1:] if args and not args[0].startswith("-") else args
    if cmd == "daemon":
        from . import daemon
        daemon.main(rest)
        return 0
    if cmd == "setup":
        script = Path(__file__).parent / "data" / "setup-bluetooth.sh"
        return subprocess.call(["pkexec", "bash", str(script)])
    if cmd == "status":
        return status()
    if cmd in ("app", "ui"):
        from . import app
        return app.main(rest)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
