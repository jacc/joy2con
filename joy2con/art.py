# SPDX-License-Identifier: GPL-3.0-or-later
"""Original line-art drawings of the Switch 2 Joy-Cons.

Flat diagram style: one stroke weight on a plain fill, no shading, so the
drawing reads as a diagram of the controller rather than an imitation photo.
The controller's own accent colour (from its factory data) marks the
attaching edge and the stick collar, as on the real Joy-Con 2.

Each Joy-Con is drawn in two layers so the app can put pressed-button
highlights between them: fill_svg() (body and shoulder fill) and lines_svg()
(outlines, symbols, accents); shoulder_svg() draws one shoulder button's
shape for its highlight. BUTTONS gives each other control's position in the
shared 160 x 440 space; STICK each stick's centre, travel and cap radius;
LEDS the player LED positions on the attaching edge.
"""

from __future__ import annotations

W, H = 160, 440

# name -> (shape, cx, cy, a, b): "c" circle of radius a, "r" rounded rect a x b.
BUTTONS = {
    "left": {
        "MINUS": ("r", 62, 74, 20, 8),
        "LSTICK": ("c", 76, 140, 24, 0),
        "UP": ("c", 76, 222, 14, 0), "DOWN": ("c", 76, 294, 14, 0),
        "LEFT": ("c", 40, 258, 14, 0), "RIGHT": ("c", 112, 258, 14, 0),
        "CAPTURE": ("r", 110, 346, 22, 22),
        "SL_L": ("r", 142, 120, 8, 46), "SR_L": ("r", 142, 336, 8, 46),
    },
    "right": {
        "PLUS": ("r", 48, 74, 20, 20),
        "X": ("c", 90, 92, 14, 0), "A": ("c", 122, 124, 14, 0),
        "B": ("c", 90, 156, 14, 0), "Y": ("c", 58, 124, 14, 0),
        "RSTICK": ("c", 96, 240, 24, 0),
        "HOME": ("r", 58, 322, 22, 22), "C": ("r", 102, 352, 22, 22),
        "SL_R": ("r", 18, 120, 8, 46), "SR_R": ("r", 18, 336, 8, 46),
    },
}
STICK = {"left": (76, 140, 12, 24), "right": (96, 240, 12, 24)}   # centre x, y, travel, cap radius
LEDS = {"left": (142, [214, 228, 242, 256]), "right": (18, [214, 228, 242, 256])}
STROKE = 1.7

# Shoulder buttons: bands wrapping the curved top end, tapering down the outer
# side. The front band is L/R; the thinner one behind it is ZL/ZR.
SHOULDERS = {"left": ("L", "ZL"), "right": ("R", "ZR")}
_BAND = ((7.0, 0.50, 0.88), (5.0, 0.36, 0.64))   # thickness, full until, gone at (fractions of the edge)


def _hex(c: str | None, fallback: str) -> str:
    c = (c or "").strip()
    return c if len(c) == 7 and c.startswith("#") else fallback


def body_path(side: str, dy: float = 0) -> str:
    """Silhouette: semicircular outer ends, a straight attaching edge."""
    def y(v):
        return v - dy
    if side == "left":   # attaching edge on the right (x = 146)
        return (f"M146,{y(34)} L146,{y(426)} Q146,{y(434)} 138,{y(434)} L80,{y(434)} "
                f"C40,{y(434)} 14,{y(404)} 14,{y(366)} L14,{y(102)} C14,{y(64)} 40,{y(26)} 80,{y(26)} "
                f"L138,{y(26)} Q146,{y(26)} 146,{y(34)} Z")
    return (f"M14,{y(34)} L14,{y(426)} Q14,{y(434)} 22,{y(434)} L80,{y(434)} "
            f"C120,{y(434)} 146,{y(404)} 146,{y(366)} L146,{y(102)} C146,{y(64)} 120,{y(26)} 80,{y(26)} "
            f"L22,{y(26)} Q14,{y(26)} 14,{y(34)} Z")


def _top_edge(side: str, n: int = 90) -> list[tuple[float, float, float, float]]:
    """Points along the body's top edge, from the attaching edge round to the
    outer side, evenly spaced: (x, y, offset direction x, offset direction y).

    Points on the rounded corner at the attaching edge move straight up, so a
    band ends flush with that edge; elsewhere they move along the outward normal."""
    import math

    def quad(p0, c, p1, t):
        return tuple((1 - t) ** 2 * a + 2 * (1 - t) * t * b + t * t * d for a, b, d in zip(p0, c, p1))

    def cubic(p0, c0, c1, p1, t):
        u = 1 - t
        return tuple(u ** 3 * a + 3 * u * u * t * b + 3 * u * t * t * c + t ** 3 * d for a, b, c, d in zip(p0, c0, c1, p1))

    raw = []   # (x, y, is_corner)
    for i in range(20):
        raw.append((*quad((146, 34), (146, 26), (138, 26), i / 20), True))
    for i in range(20):
        raw.append((138 - 58 * i / 20, 26.0, False))
    for i in range(60):
        raw.append((*cubic((80, 26), (40, 26), (14, 64), (14, 102), i / 60), False))
    for i in range(21):
        raw.append((14.0, 102 + 48 * i / 20, False))
    if side == "right":
        raw = [(160 - x, y, c) for x, y, c in raw]
    # resample by arc length
    dist = [0.0]
    for (x0, y0, _), (x1, y1, _) in zip(raw, raw[1:]):
        dist.append(dist[-1] + math.hypot(x1 - x0, y1 - y0))
    out, j = [], 0
    for k in range(n + 1):
        target = dist[-1] * k / n
        while j < len(raw) - 2 and dist[j + 1] < target:
            j += 1
        f = (target - dist[j]) / max(1e-9, dist[j + 1] - dist[j])
        x = raw[j][0] + (raw[j + 1][0] - raw[j][0]) * f
        y = raw[j][1] + (raw[j + 1][1] - raw[j][1]) * f
        tx, ty = raw[j + 1][0] - raw[j][0], raw[j + 1][1] - raw[j][1]
        ln = math.hypot(tx, ty) or 1.0
        tx, ty = tx / ln, ty / ln
        if raw[j][2]:
            nx, ny = 0.0, -1.0
        else:
            nx, ny = (-ty, tx) if side == "left" else (ty, -tx)
        out.append((x, y, nx, ny))
    return out


def _thickness(k: float, full: float, until: float, gone: float) -> float:
    if k <= until:
        return full
    if k >= gone:
        return 0.0
    f = (k - until) / (gone - until)
    return full * (1 - f * f * (3 - 2 * f))


def shoulder_path(side: str, name: str) -> str:
    """Outline of one shoulder button's visible band."""
    edge = _top_edge(side)
    n = len(edge) - 1
    front, back = _BAND
    inner, outer = [], []
    for k, (x, y, nx, ny) in enumerate(edge):
        f = k / n
        w0 = _thickness(f, *front)
        lo, hi = (0.0, w0) if name in ("L", "R") else (w0, w0 + _thickness(f, *back))
        inner.append((x + nx * lo, y + ny * lo))
        outer.append((x + nx * hi, y + ny * hi))
    pts = outer + inner[::-1]
    return "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts) + " Z"


def shoulder_svg(side: str, name: str, color: str | None) -> str:
    color = _hex(color, "#ffffff")
    return _svg([f'<path d="{shoulder_path(side, name)}" fill="{color}"/>'])


def _svg(body: list[str]) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}">\n'
            + "\n".join(body) + "\n</svg>")


def fill_svg(side: str, fill: str | None) -> str:
    fill = _hex(fill, "#2a2c30")
    g = [f'<path d="{shoulder_path(side, name)}" fill="{fill}"/>' for name in SHOULDERS[side]]
    g.append(f'<path d="{body_path(side)}" fill="{fill}"/>')
    return _svg(g)


def outline_svg(side: str, color: str | None) -> str:
    """The silhouette (body and shoulders) traced in a heavier stroke, shown
    over the drawing while the pointer is on it."""
    color = _hex(color, "#ffffff")
    stroke = f'fill="none" stroke="{color}" stroke-width="{STROKE * 1.9}" stroke-linejoin="round"'
    g = [f'<path d="{shoulder_path(side, name)}" {stroke}/>' for name in reversed(SHOULDERS[side])]
    g.append(f'<path d="{body_path(side)}" {stroke}/>')
    return _svg(g)


def lines_svg(side: str, line: str | None, accent: str | None) -> str:
    line = _hex(line, "#c8cacf")
    accent = _hex(accent, "#9be1e6" if side == "left" else "#ff8c5f")
    stroke = f'fill="none" stroke="{line}" stroke-width="{STROKE}" stroke-linejoin="round"'
    g = []
    for name in reversed(SHOULDERS[side]):
        g.append(f'<path d="{shoulder_path(side, name)}" {stroke}/>')
    # attaching edge: the accent strip with the player LED windows
    sx = 138 if side == "left" else 14
    g.append(f'<rect x="{sx}" y="36" width="8" height="388" rx="3" fill="{accent}" fill-opacity="0.9"/>')
    lx, ys = LEDS[side]
    for y in ys:
        g.append(f'<rect x="{lx - 2}" y="{y - 3}" width="4" height="6" rx="1" fill="#000" fill-opacity="0.45"/>')
    g.append(f'<path d="{body_path(side)}" {stroke}/>')

    def circle(name, text=None):
        _, cx, cy, r, _ = BUTTONS[side][name]
        g.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" {stroke}/>')
        if text:
            g.append(f'<text x="{cx}" y="{cy + 4.3}" font-family="sans-serif" font-size="12.5" '
                     f'fill="{line}" text-anchor="middle">{text}</text>')
        return cx, cy

    def square(name):
        _, cx, cy, w, _ = BUTTONS[side][name]
        g.append(f'<rect x="{cx - w / 2}" y="{cy - w / 2}" width="{w}" height="{w}" rx="5" {stroke}/>')
        return cx, cy

    # stick collar in the accent colour; the moving cap is drawn by the app
    scx, scy, _, cap = STICK[side]
    g.append(f'<circle cx="{scx}" cy="{scy}" r="{cap + 7}" fill="none" stroke="{accent}" stroke-width="2.6"/>')
    g.append(f'<circle cx="{scx}" cy="{scy}" r="{cap + 10.5}" {stroke} stroke-opacity="0.35"/>')

    if side == "left":
        _, cx, cy, _, _ = BUTTONS["left"]["MINUS"]
        g.append(f'<rect x="{cx - 7}" y="{cy - 1.5}" width="14" height="3" rx="1.5" fill="{line}"/>')
        tri = {"UP": "M0,-5 L4.6,3 L-4.6,3 Z", "DOWN": "M0,5 L4.6,-3 L-4.6,-3 Z",
               "LEFT": "M-5,0 L3,-4.6 L3,4.6 Z", "RIGHT": "M5,0 L-3,-4.6 L-3,4.6 Z"}
        for name, d in tri.items():
            cx, cy = circle(name)
            g.append(f'<path transform="translate({cx},{cy})" d="{d}" fill="{line}"/>')
        cx, cy = square("CAPTURE")
        g.append(f'<circle cx="{cx}" cy="{cy}" r="5" {stroke}/>')
    else:
        _, cx, cy, _, _ = BUTTONS["right"]["PLUS"]
        g.append(f'<path transform="translate({cx},{cy})" '
                 f'd="M-7,-1.5 h5.5 v-5.5 h3 v5.5 h5.5 v3 h-5.5 v5.5 h-3 v-5.5 h-5.5 Z" fill="{line}"/>')
        for name in ("X", "A", "B", "Y"):
            circle(name, name)
        cx, cy = square("HOME")
        g.append(f'<path transform="translate({cx},{cy})" d="M0,-6 L6,0 L4,0 L4,5 L-4,5 L-4,0 L-6,0 Z" {stroke}/>')
        cx, cy = square("C")
        g.append(f'<path transform="translate({cx},{cy})" d="M4.2,-3.8 A5.6,5.6 0 1 0 4.2,3.8" '
                 f'{stroke} stroke-linecap="round"/>')
    return _svg(g)
