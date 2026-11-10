#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Renders the images in docs/:

- banner-dark.png, banner-light.png: README banners on a transparent
  background, for GitHub's dark and light themes
- social-preview.png: the card GitHub shows when the repository is linked
- screenshot-main.png, screenshot-joycon.png: the app, rendered off-screen
  against tools/demo_daemon.py (needs KDE's platform theme for the styling)

    python3 tools/docs_images.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QPainter, QPen  # noqa: E402
from PySide6.QtSvg import QSvgRenderer  # noqa: E402

from joy2con import art  # noqa: E402

THEMES = {
    "dark": {"line": "#c9d1d9", "fill_alpha": 0.07, "text": "#f0f6fc", "muted": "#9198a1",
             "accent": {"left": "#9be1e6", "right": "#ff8c5f"}},
    "light": {"line": "#1f2328", "fill_alpha": 0.05, "text": "#1f2328", "muted": "#59636e",
              "accent": {"left": "#1aa3b2", "right": "#ec5f2f"}},
}
SCALE = 2   # pixels per layout unit, for sharp images on high-density screens


def draw_joycon(p: QPainter, side: str, cx: float, cy: float, height: float, angle: float, theme: dict) -> None:
    k = height / art.H
    width = art.W * k
    p.save()
    p.translate(cx, cy)
    p.rotate(angle)
    p.translate(-width / 2, -height / 2)
    box = QRectF(0, 0, width, height)
    p.setOpacity(theme["fill_alpha"])
    QSvgRenderer(art.fill_svg(side, theme["line"]).encode()).render(p, box)
    p.setOpacity(1)
    QSvgRenderer(art.lines_svg(side, theme["line"], theme["accent"][side]).encode()).render(p, box)
    sx, sy, _, cap = art.STICK[side]   # stick cap at rest
    pen = QPen(QColor(theme["line"]))
    pen.setWidthF(art.STROKE * k)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawEllipse(QPointF(sx * k, sy * k), cap * k, cap * k)
    faint = QColor(theme["line"])
    faint.setAlphaF(0.5)
    pen.setColor(faint)
    pen.setWidthF(art.STROKE * 0.8 * k)
    p.setPen(pen)
    p.drawEllipse(QPointF(sx * k, sy * k), cap * 0.58 * k, cap * 0.58 * k)
    p.restore()


def wordmark(p: QPainter, rect: QRectF, size: float, theme: dict) -> None:
    font = QFont("Inter Display")
    font.setPixelSize(round(size))
    font.setWeight(QFont.Weight.Bold)
    font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 97)
    p.setFont(font)
    p.setPen(QColor(theme["text"]))
    p.drawText(rect, Qt.AlignmentFlag.AlignCenter, "joy2con")


def caption(p: QPainter, rect: QRectF, size: float, text: str, color: str) -> None:
    font = QFont("Inter")
    font.setPixelSize(round(size))
    p.setFont(font)
    p.setPen(QColor(color))
    p.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)


def canvas(w: int, h: int, background: str | None) -> tuple[QImage, QPainter]:
    img = QImage(w * SCALE, h * SCALE, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(QColor(background) if background else Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    p.scale(SCALE, SCALE)
    return img, p


def banner(theme_name: str) -> None:
    theme = THEMES[theme_name]
    w, h = 1280, 400
    img, p = canvas(w, h, None)
    draw_joycon(p, "left", 285, 200, 340, -9, theme)
    draw_joycon(p, "right", w - 285, 200, 340, 9, theme)
    wordmark(p, QRectF(0, 105, w, 150), 132, theme)
    caption(p, QRectF(0, 250, w, 50), 30, "Switch 2 Joy-Cons on Linux", theme["muted"])
    p.end()
    img.save(str(DOCS / f"banner-{theme_name}.png"))


def social_preview() -> None:
    theme = THEMES["dark"]
    w, h = 1280, 640
    img, p = canvas(w, h, "#0d1117")
    draw_joycon(p, "left", 215, 320, 500, -9, theme)
    draw_joycon(p, "right", w - 215, 320, 500, 9, theme)
    wordmark(p, QRectF(0, 195, w, 170), 150, theme)
    caption(p, QRectF(0, 360, w, 50), 34, "Switch 2 Joy-Cons on Linux", theme["muted"])
    caption(p, QRectF(0, 420, w, 40), 24, "Wireless, with motion controls, rumble and a KDE app", theme["muted"])
    p.end()
    img.save(str(DOCS / "social-preview.png"))


def screenshots() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        sock = os.path.join(tmp, "demo.sock")
        demo = subprocess.Popen([sys.executable, str(ROOT / "tools" / "demo_daemon.py"), sock])
        try:
            for _ in range(50):
                if os.path.exists(sock):
                    break
                time.sleep(0.1)
            env = dict(os.environ, JOY2CON_SOCKET=sock, QT_QPA_PLATFORM="offscreen",
                       QT_QPA_PLATFORMTHEME="kde", QT_SCALE_FACTOR=str(SCALE), JOY2CON_SHOT_MS="4500")
            shots = (("screenshot-main.png", []), ("screenshot-joycon.png", ["--page", "right"]))
            for name, extra in shots:
                subprocess.run([sys.executable, "-m", "joy2con", "--screenshot", str(DOCS / name),
                                "--size", "560x900", *extra], cwd=ROOT, env=env, check=True)
        finally:
            demo.terminate()


if __name__ == "__main__":
    DOCS.mkdir(exist_ok=True)
    app = QGuiApplication([sys.argv[0], "-platform", "offscreen"])
    banner("dark")
    banner("light")
    social_preview()
    screenshots()
    print("wrote", ", ".join(sorted(f.name for f in DOCS.glob("*.png"))))
