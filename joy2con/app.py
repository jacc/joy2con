# SPDX-License-Identifier: GPL-3.0-or-later
"""joy2con desktop app: a Kirigami window and tray icon for the joy2con daemon.

The app is only a client. It reads state from the daemon's Unix socket
(daemon.Server) and sends actions back; closing the window leaves the driver
running, and the tray icon keeps battery levels in view.
"""

from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

from PySide6.QtCore import (Property, Q_ARG, QMetaObject, QObject, QProcess, QSize, QTimer, QUrl, Qt, Signal, Slot)
from PySide6.QtGui import QAction, QIcon, QImage, QPainter
from PySide6.QtNetwork import QLocalSocket
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuick import QQuickImageProvider
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from . import art
from .daemon import socket_path

HERE = Path(__file__).parent
ICON = HERE / "data" / "joy2con.svg"

def _angles(q):
    """Degrees of tilt about the controller's own axes from its orientation:
    (forward/back about x, sideways about y, turn about z)."""
    w, x, y, z = q
    about_x = math.degrees(math.atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y)))
    about_y = math.degrees(math.asin(max(-1.0, min(1.0, 2 * (w * y - z * x)))))
    about_z = math.degrees(math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z)))
    return [about_x, about_y, about_z]


class JoyConImages(QQuickImageProvider):
    """image://joycon/<side>/fill/<rrggbb>, image://joycon/<side>/lines/<line rrggbb>/<accent rrggbb>
    image://joycon/<side>/shoulder/<name>/<rrggbb> and image://joycon/<side>/outline/<rrggbb>."""

    def __init__(self) -> None:
        super().__init__(QQuickImageProvider.ImageType.Image)

    def requestImage(self, ident: str, size: QSize, requested: QSize) -> QImage:
        parts = ident.split("/")
        side, layer = parts[0], parts[1] if len(parts) > 1 else "lines"
        hexes = ["#" + v for v in parts[2:]]
        if layer == "fill":
            svg = art.fill_svg(side, hexes[0] if hexes else None)
        elif layer == "outline":
            svg = art.outline_svg(side, hexes[0] if hexes else None)
        elif layer == "shoulder":
            name = parts[2] if len(parts) > 2 else ""
            svg = art.shoulder_svg(side, name, "#" + parts[3] if len(parts) > 3 else None)
        else:
            svg = art.lines_svg(side, hexes[0] if hexes else None, hexes[1] if len(hexes) > 1 else None)
        if requested.width() > 0 and requested.height() > 0:
            target = requested
        else:
            target = QSize(art.W * 2, art.H * 2)
        img = QImage(target, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(Qt.GlobalColor.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        QSvgRenderer(svg.encode()).render(p)
        p.end()
        return img


class Backend(QObject):
    stateChanged = Signal()
    connectedChanged = Signal()
    eventsChanged = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._state: dict = {}
        self._events: list = []
        self._connected = False
        self._buf = b""
        self.sock = QLocalSocket(self)
        self.sock.readyRead.connect(self._read)
        self.sock.connected.connect(lambda: self._set_connected(True))
        self.sock.disconnected.connect(lambda: self._set_connected(False))
        self.sock.errorOccurred.connect(lambda _e: self._set_connected(False))
        self.retry = QTimer(self, interval=1000)
        self.retry.timeout.connect(self._try_connect)
        self.retry.start()
        self._try_connect()

    def _try_connect(self) -> None:
        if self.sock.state() == QLocalSocket.LocalSocketState.UnconnectedState:
            self.sock.connectToServer(str(socket_path()))

    def _set_connected(self, on: bool) -> None:
        if on != self._connected:
            self._connected = on
            try:
                self.connectedChanged.emit()
            except RuntimeError:   # the app is shutting down
                pass

    def _read(self) -> None:
        self._buf += bytes(self.sock.readAll())
        *lines, self._buf = self._buf.split(b"\n")
        latest = None
        for line in lines:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("events"):
                self._events = (list(reversed(msg["events"])) + self._events)[:200]
                self.eventsChanged.emit()
            if msg.get("type") == "state":
                latest = msg["state"]
        if latest is not None:
            for jc in latest.get("joycons", {}).values():
                jc["angles"] = _angles(jc["quat"])
            self._state = latest
            self.stateChanged.emit()

    @Property("QVariantMap", notify=stateChanged)
    def state(self):
        return self._state

    @Property(bool, notify=connectedChanged)
    def connected(self):
        return self._connected

    @Property("QVariantList", notify=eventsChanged)
    def events(self):
        return self._events

    @Slot(str, "QVariantMap")
    def action(self, name: str, args) -> None:
        if self._connected:
            self.sock.write((json.dumps({"action": name, "args": dict(args or {})}) + "\n").encode())

    @Slot(str, result="QVariantList")
    def layout(self, side: str):
        return [{"name": n, "shape": s, "cx": cx, "cy": cy, "a": a, "b": b}
                for n, (s, cx, cy, a, b) in art.BUTTONS.get(side, {}).items()]

    @Slot(str, result="QVariantList")
    def stick(self, side: str):
        return list(art.STICK.get(side, (0, 0, 0, 0)))

    @Slot(str, result="QVariantList")
    def shoulders(self, side: str):
        return list(art.SHOULDERS.get(side, ()))

    @Slot()
    def startDriver(self) -> None:
        """Start the daemon: through its systemd user service when installed,
        otherwise as a detached process of this Python."""
        if QProcess.execute("systemctl", ["--user", "start", "joy2con.service"]) != 0:
            QProcess.startDetached(sys.executable, ["-m", "joy2con", "daemon"])

    @Slot(str, result="QVariantList")
    def leds(self, side: str):
        x, ys = art.LEDS.get(side, (0, []))
        return [x, ys]


class Tray(QSystemTrayIcon):
    def __init__(self, backend: Backend, window_getter) -> None:
        super().__init__(QIcon(str(ICON)))
        self.backend = backend
        self.window_getter = window_getter
        menu = QMenu()
        show = QAction("Show joy2con", menu)
        show.triggered.connect(self.show_window)
        quit_ = QAction("Quit app (driver keeps running)", menu)
        quit_.triggered.connect(QApplication.quit)
        menu.addAction(show)
        menu.addSeparator()
        menu.addAction(quit_)
        self.setContextMenu(menu)
        self._menu = menu
        self.activated.connect(self._activated)
        backend.stateChanged.connect(self._update)
        backend.connectedChanged.connect(self._update)
        self._last_tip = None
        self._update()

    def _activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            w = self.window_getter()
            if w is not None and w.isVisible() and w.isActive():
                w.hide()
            else:
                self.show_window()

    def show_window(self) -> None:
        w = self.window_getter()
        if w is not None:
            w.show()
            w.raise_()
            w.requestActivate()

    def _update(self) -> None:
        if not self.backend.connected:
            tip = "joy2con: driver not running"
        else:
            jcs = self.backend.state.get("joycons", {})
            parts = [f"{s.capitalize()} {j.get('battery_pct', 0)}%" for s, j in sorted(jcs.items())]
            tip = "Joy-Cons: " + (", ".join(parts) if parts else "none connected")
        if tip != self._last_tip:
            self._last_tip = tip
            self.setToolTip(tip)


def main(argv: list[str] | None = None) -> int:
    os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "org.kde.desktop")
    QQuickStyle.setStyle(os.environ["QT_QUICK_CONTROLS_STYLE"])
    app = QApplication(sys.argv[:1] + list(argv or []))
    app.setApplicationName("joy2con")
    app.setDesktopFileName("joy2con")
    app.setWindowIcon(QIcon(str(ICON)))
    app.setQuitOnLastWindowClosed(False)
    backend = Backend()
    engine = QQmlApplicationEngine()
    engine.addImageProvider("joycon", JoyConImages())
    engine.rootContext().setContextProperty("backend", backend)
    engine.load(QUrl.fromLocalFile(str(HERE / "qml" / "Main.qml")))
    if not engine.rootObjects():
        return 1
    window = engine.rootObjects()[0]
    tray = Tray(backend, lambda: window)
    if QSystemTrayIcon.isSystemTrayAvailable():
        tray.show()
    argv = list(argv or [])
    if "--size" in argv:   # WIDTHxHEIGHT in logical pixels, for screenshots
        w, h = (int(v) for v in argv[argv.index("--size") + 1].split("x"))
        window.resize(w, h)
    if "--hidden" not in argv:
        window.show()
    if "--screenshot" in argv:   # for checking the layout: save the window after it settles
        out = argv[argv.index("--screenshot") + 1]
        if "--pointer" in argv:   # move a synthetic pointer to x,y first, to check hover states
            from PySide6.QtCore import QPoint
            from PySide6.QtTest import QTest
            x, y = (int(v) for v in argv[argv.index("--pointer") + 1].split(","))
            QTimer.singleShot(1500, lambda: QTest.mouseMove(window, QPoint(x, y)))
        if "--page" in argv:
            side = argv[argv.index("--page") + 1]
            if side == "diagnostics":
                QTimer.singleShot(1000, lambda: QMetaObject.invokeMethod(window, "openDiagnostics"))
            else:
                QTimer.singleShot(1000, lambda: QMetaObject.invokeMethod(window, "openJoyCon", Q_ARG("QVariant", side)))
        QTimer.singleShot(int(os.environ.get("JOY2CON_SHOT_MS", "4000")),
                          lambda: (window.grabWindow().save(out), app.quit()))
    return app.exec()
