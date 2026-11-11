"""Draw the race silhouettes shown when a character has no picture from Blizzard.

Original, simple shapes (no game artwork): one standing figure per race, built from a shared body with
per-race proportions and features (ears, horns, tusks, tails...). Writes plain SVG files (attributes
only, no style elements, so they suit a strict content security policy) to
src/questersboard/web/static/silhouettes/<race_key>.svg, plus generic.svg for unknown races.

    python3 tools/make_silhouettes.py
"""
from __future__ import annotations

import math
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "src/questersboard/web/static/silhouettes"
W, H, GROUND, CX = 240, 440, 428, 120.0


def f(n: float) -> str:
    return f"{n:.1f}".rstrip("0").rstrip(".")


def poly(pts) -> str:
    return '<polygon points="' + " ".join(f"{f(x)},{f(y)}" for x, y in pts) + '"/>'


def ell(cx, cy, rx, ry, rot=0.0) -> str:
    t = f' transform="rotate({f(rot)} {f(cx)} {f(cy)})"' if rot else ""
    return f'<ellipse cx="{f(cx)}" cy="{f(cy)}" rx="{f(rx)}" ry="{f(ry)}"{t}/>'


def path(d: str) -> str:
    return f'<path d="{d}"/>'


def mirror(pts):
    return [(2 * CX - x, y) for x, y in pts]


def both(pts) -> list[str]:
    return [poly(pts), poly(mirror(pts))]


class Body:
    """A front-facing standing figure. Sizes are fractions of the figure height h."""

    def __init__(self, h=400, head=(0.05, 0.065), neck=0.03, sh=0.12, waist=0.07, hip=0.085, torso=0.27,
                 thigh=0.05, ankle=0.022, arm=0.032, wrist=0.02, reach=0.0, hunch=0.0, hand=0.026,
                 belly=0.0, hoof=False, spread=0.012):
        h = h * (0.84 if h > 300 else 0.95)          # room above for ears, horns and headdresses
        self.h = h
        top = GROUND - h
        self.hrx, self.hry = head[0] * h, head[1] * h
        self.hy = top + self.hry + hunch * h * 0.6
        self.shy = top + 2 * self.hry + neck * h
        self.shw, self.ww, self.hw = sh * h, waist * h, hip * h
        self.wy = self.shy + torso * h
        self.hipy = self.wy + 0.07 * h
        self.thigh, self.ankle, self.arm, self.wrist = thigh * h, ankle * h, arm * h, wrist * h
        self.reach, self.hunch, self.hand, self.belly = reach * h, hunch, hand * h, belly * h
        self.hoof, self.spread = hoof, spread * h
        self.handy = self.hipy + 0.09 * h + self.reach

    def shapes(self) -> list[str]:
        h, s = self.h, []
        P = lambda x, y: f"{f(x)},{f(y)}"                      # noqa: E731
        # head and neck
        s.append(ell(CX, self.hy, self.hrx, self.hry))
        nw = self.hrx * 0.5
        s.append(poly([(CX - nw, self.hy), (CX + nw, self.hy), (CX + nw * 1.2, self.shy + 4), (CX - nw * 1.2, self.shy + 4)]))
        # torso: one smooth outline, shoulders to waist to hips (left half drawn, right mirrored)
        trap, sw, ww, hw = self.hunch * h * 0.5, self.shw, self.ww, self.hw
        pit = self.shy + 0.09 * h

        def half(sg):
            X = lambda x: CX + sg * (x - CX)                 # noqa: E731
            return [(X(CX - nw * 1.2), self.shy - 1), ("Q", X(CX - sw * 0.75), self.shy - 0.012 * h - trap,
                    X(CX - sw), self.shy + 0.035 * h), ("Q", X(CX - sw * 1.02), pit - 0.02 * h, X(CX - sw * 0.86), pit),
                    ("Q", X(CX - sw * 0.82), self.wy - 0.07 * h, X(CX - ww), self.wy),
                    ("Q", X(CX - ww * 1.02), self.wy + 0.035 * h, X(CX - hw), self.hipy)]
        d = "M" + P(CX, self.shy - 2)
        for seg in half(-1) + [None] + list(reversed(half(1))):
            pass
        L, R = half(-1), half(1)
        d = f"M{P(*L[0])}"
        for q in L[1:]:
            d += f" Q{P(q[1], q[2])} {P(q[3], q[4])}"
        d += f" L{P(CX, self.hipy + 0.05 * h)} L{P(R[-1][3], R[-1][4])}"
        pts = [R[0][0:2]] + [(q[3], q[4]) for q in R[1:]]
        for i in range(len(R) - 1, 0, -1):
            q = R[i]
            d += f" Q{P(q[1], q[2])} {P(*pts[i - 1])}"
        s.append(path(d + " Z"))
        if self.belly:
            s.append(ell(CX, self.wy - 0.03 * h, self.belly, self.belly * 0.95))
        # arms: a curved upper arm and forearm hanging slightly away from the body
        ex, ey = CX - sw - 0.006 * h, self.shy + 0.17 * h
        hx = CX - sw - 0.02 * h - self.reach * 0.25
        a, wr = self.arm, self.wrist
        arm = (f"M{P(CX - sw + a * 0.5, self.shy + 0.01 * h)} Q{P(CX - sw - a * 1.3, self.shy + 0.02 * h)} {P(ex - a * 0.95, ey)}"
               f" Q{P(ex - a * 1.05, ey + 0.06 * h)} {P(hx - wr, self.handy)} L{P(hx + wr, self.handy)}"
               f" Q{P(ex + a * 0.95, ey + 0.05 * h)} {P(ex + a * 0.8, ey)} Q{P(ex + a * 1.1, pit)} {P(CX - sw * 0.8, pit)} Z")
        s.append(path(arm))
        s.append(path(arm).replace('d="', 'transform="matrix(-1 0 0 1 240 0)" d="'))
        s += [ell(hx, self.handy + self.hand * 0.8, self.hand * 0.78, self.hand),
              ell(2 * CX - hx, self.handy + self.hand * 0.8, self.hand * 0.78, self.hand)]
        # legs: thigh, knee, calf and ankle, with a clear gap between the legs
        ky = self.hipy + (GROUND - self.hipy) * 0.48
        ay = GROUND - 0.035 * h
        t, k = self.thigh, self.ankle
        lc = CX - max(hw * 0.5, t * 0.75 + self.spread * 0.5)
        leg = (f"M{P(CX - hw, self.hipy - 0.02 * h)} Q{P(CX - hw - t * 0.25, self.hipy + 0.12 * h)} {P(lc - t * 0.62, ky)}"
               f" Q{P(lc - t * 0.8, ky + 0.09 * h)} {P(lc - k, ay)} L{P(lc + k * 0.85, ay)}"
               f" Q{P(lc + t * 0.45, ky + 0.07 * h)} {P(lc + t * 0.42, ky)} Q{P(lc + t * 0.62, self.hipy + 0.1 * h)}"
               f" {P(CX - self.spread * 0.5, self.hipy + 0.07 * h)} L{P(CX, self.hipy)} Z")
        s.append(path(leg))
        s.append(path(leg).replace('d="', 'transform="matrix(-1 0 0 1 240 0)" d="'))
        fx = lc - 0.004 * h
        if self.hoof:
            foot = [(fx - k * 1.1, ay - 3), (fx + k * 1.0, ay - 3), (fx + k * 1.3, GROUND), (fx - k * 1.4, GROUND)]
            s += both(foot)
        else:
            s += [ell(fx - 0.006 * h, GROUND - 0.015 * h, k * 1.8, 0.018 * h),
                  ell(2 * CX - fx + 0.006 * h, GROUND - 0.015 * h, k * 1.8, 0.018 * h)]
        return s

    # feature helpers (all relative to the head)
    def ears(self, length, lift, width=0.35, droop=0.0) -> list[str]:
        """Pointed ears from the sides of the head: length and width are in head widths; lift raises the
        tip (fraction of the length), droop lowers the base."""
        bx, by = CX - self.hrx * 0.85, self.hy + self.hry * (0.05 + droop)
        L = length * self.hrx * 2
        tip = (bx - L * math.cos(math.asin(min(lift, 0.95))), by - L * lift)
        wv = width * self.hrx
        return both([(bx + 2, by - wv), tip, (bx + 2, by + wv)])

    def tusks(self, size, up=0.9) -> list[str]:
        out = []
        for side in (-1, 1):
            x0, y0 = CX + side * self.hrx * 0.45, self.hy + self.hry * 0.7
            out.append(poly([(x0 - 2.2, y0), (x0 + 2.2, y0), (x0 + side * size * 0.45, y0 - size * up)]))
        return out

    def hair_dome(self, grow=1.12, lift=0.1) -> str:
        return ell(CX, self.hy - self.hry * lift, self.hrx * grow, self.hry * (0.9 + lift))


def tail(x0, y0, x1, y1, w0, w1=1.5, bend=30) -> str:
    """A tapering curved tail from (x0, y0) to (x1, y1)."""
    mx, my = (x0 + x1) / 2 + bend, (y0 + y1) / 2
    return path(f"M{f(x0 - w0)},{f(y0)} Q{f(mx - w0)},{f(my)} {f(x1 - w1)},{f(y1)} L{f(x1 + w1)},{f(y1)} "
                f"Q{f(mx + w0)},{f(my)} {f(x0 + w0)},{f(y0)} Z")


def horn(b, side, length, up, curl, width) -> str:
    """A curved horn from the top side of the head."""
    x0, y0 = CX + side * b.hrx * 0.7, b.hy - b.hry * 0.55
    x1, y1 = x0 + side * length, y0 - up
    cx, cy = x0 + side * length * 0.9, y0 + curl
    return path(f"M{f(x0)},{f(y0 - width)} Q{f(cx)},{f(cy - width)} {f(x1)},{f(y1)} Q{f(cx)},{f(cy + width)} "
                f"{f(x0)},{f(y0 + width)} Z")


def spikes(b, n, height, spread, y=None, w=None) -> list[str]:
    """A row of upward spikes over the head (mohawk, crest, flame hair)."""
    y = b.hy - b.hry * 0.8 if y is None else y
    w = (spread * 2 / n) * 0.75 if w is None else w
    out = []
    for i in range(n):
        x = CX - spread + (2 * spread) * (i + 0.5) / n
        hgt = height * (1 - abs((i + 0.5) / n - 0.5) * 0.9)
        out.append(poly([(x - w, y + 4), (x + w, y + 4), (x + (x - CX) * 0.15, y - hgt)]))
    return out


def beard(b, length, width) -> str:
    y0 = b.hy + b.hry * 0.1
    return poly([(CX - b.hrx * width, y0), (CX + b.hrx * width, y0), (CX + b.hrx * width * 0.45, y0 + length),
                 (CX, y0 + length * 1.12), (CX - b.hrx * width * 0.45, y0 + length)])


def feathers(b, side, n=3) -> list[str]:
    out = []
    for i in range(n):
        x0, y0 = CX + side * b.hrx * 0.55, b.hy - b.hry * 0.45 + i * 5
        ang = math.radians(-60 + i * 22)
        L = b.hrx * (1.5 - i * 0.2)
        x1, y1 = x0 + side * L * math.cos(ang), y0 + L * math.sin(ang)
        out.append(ell((x0 + x1) / 2, (y0 + y1) / 2, L / 2, 3.2, math.degrees(ang) * side))
    return out


def wings(b, span, drop) -> list[str]:
    y = b.shy + 6
    pts = [(CX - b.shw * 0.4, y), (CX - b.shw - span, y - drop * 0.9), (CX - b.shw - span * 0.85, y + drop * 0.35),
           (CX - b.shw - span * 0.55, y + drop * 0.15), (CX - b.shw - span * 0.45, y + drop * 0.7),
           (CX - b.shw * 0.9, y + drop * 0.9)]
    return both(pts)


def claws(b) -> list[str]:
    out = []
    hx = CX - b.shw - 0.02 * b.h - b.reach * 0.25
    for side in (-1, 1):
        x = hx if side < 0 else 2 * CX - hx
        y = b.handy + b.hand * 1.6
        for k in (-1, 0, 1):
            out.append(poly([(x + k * 3.4 - 1.8, y - 2), (x + k * 3.4 + 1.8, y - 2), (x + k * 4.6, y + 7)]))
    return out


def race(key: str) -> list[str]:  # noqa: C901 - a list of looks, read top to bottom
    s: list[str] = []
    if key in ("human", "generic"):
        b = Body()
        s += b.shapes()
        if key == "human":
            s.append(b.hair_dome())
    elif key == "kul_tiran":
        b = Body(h=412, head=(0.048, 0.06), sh=0.15, waist=0.1, hip=0.1, thigh=0.06, ankle=0.028, arm=0.045,
                 wrist=0.03, hand=0.034, belly=0.075)
        s += b.shapes() + [b.hair_dome(1.1, 0.05)]
    elif key in ("dwarf", "dark_iron_dwarf", "earthen"):
        b = Body(h=270, head=(0.075, 0.085), neck=0.0, sh=0.17, waist=0.13, hip=0.12, torso=0.25, thigh=0.07,
                 ankle=0.035, arm=0.05, wrist=0.035, hand=0.04, belly=0.1, spread=0.02)
        s += b.shapes() + [beard(b, b.hry * 2.1, 0.9)]
        if key == "dark_iron_dwarf":
            s += spikes(b, 5, b.hry * 1.2, b.hrx * 0.9)
        elif key == "earthen":
            s += both([(CX - b.shw * 0.5, b.shy), (CX - b.shw * 0.9, b.shy - b.hry * 1.2), (CX - b.shw * 1.15, b.shy + 2)])
            s += both([(CX - b.shw * 0.95, b.shy + 4), (CX - b.shw * 1.35, b.shy - b.hry * 0.6), (CX - b.shw * 1.25, b.shy + 12)])
        else:
            s.append(b.hair_dome(1.05, 0.05))
    elif key in ("gnome", "mechagnome"):
        b = Body(h=215, head=(0.1, 0.11), neck=0.005, sh=0.11, waist=0.08, hip=0.09, torso=0.22, thigh=0.05,
                 ankle=0.03, arm=0.035, wrist=0.025, hand=0.035, spread=0.02)
        s += b.shapes() + b.ears(0.55, 0.35, 0.32) + [b.hair_dome(1.08, 0.12)]
        s += spikes(b, 3, b.hry * 0.9, b.hrx * 0.5)
        if key == "mechagnome":
            ax = CX + b.hrx * 0.35
            s += [poly([(ax - 1.5, b.hy - b.hry), (ax + 1.5, b.hy - b.hry), (ax + 6, b.hy - b.hry * 2.6), (ax + 3, b.hy - b.hry * 2.6)]),
                  ell(ax + 5, b.hy - b.hry * 2.7, 5, 5)]
            s += [ell(CX + b.shw * 0.95, b.shy + 0.03 * b.h, b.arm * 1.9, b.arm * 1.6)]
    elif key == "goblin":
        b = Body(h=235, head=(0.085, 0.09), neck=0.0, sh=0.1, waist=0.075, hip=0.085, torso=0.24, thigh=0.045,
                 ankle=0.026, arm=0.03, wrist=0.022, hand=0.04, reach=0.07, hunch=0.04, spread=0.025)
        s += b.shapes() + b.ears(1.15, 0.12, 0.42, droop=0.1)
        s.append(poly([(CX - 4, b.hy), (CX + 4, b.hy), (CX, b.hy + b.hry * 0.95)]))
    elif key in ("night_elf", "blood_elf", "void_elf", "nightborne", "skyborne"):
        tall = {"night_elf": 420, "nightborne": 422, "blood_elf": 392, "void_elf": 392, "skyborne": 396}[key]
        b = Body(h=tall, head=(0.045, 0.06), sh=0.11, waist=0.062, hip=0.078, arm=0.028, wrist=0.018,
                 thigh=0.045, ankle=0.02, hand=0.024)
        s += b.shapes()
        long = {"night_elf": 1.7, "nightborne": 1.5, "blood_elf": 1.25, "void_elf": 1.25, "skyborne": 1.1}[key]
        s += b.ears(long, 0.55 if key != "night_elf" else 0.62, 0.3)
        s.append(b.hair_dome(1.08, 0.08))
        if key == "nightborne":
            s.append(poly([(CX - b.hrx * 0.7, b.hy - b.hry * 0.5), (CX + b.hrx * 0.7, b.hy - b.hry * 0.5),
                           (CX + b.hrx * 0.25, b.hy - b.hry * 2.6), (CX - b.hrx * 0.25, b.hy - b.hry * 2.6)]))
        elif key == "void_elf":
            s += [tail(CX - b.hrx * 0.7, b.hy - b.hry * 0.4, CX - b.hrx * 2.2, b.hy - b.hry * 2.1, 3, 1, -18),
                  tail(CX + b.hrx * 0.7, b.hy - b.hry * 0.4, CX + b.hrx * 2.3, b.hy - b.hry * 1.8, 3, 1, 18),
                  ell(CX - b.hrx * 2.5, b.hy - b.hry * 2.35, 3.2, 3.2), ell(CX + b.hrx * 2.6, b.hy - b.hry * 2.05, 3, 3)]
        elif key == "skyborne":
            s += feathers(b, 1, 3) + feathers(b, -1, 2)
        elif key == "blood_elf":
            s.append(ell(CX, b.hy - b.hry * 1.05, b.hrx * 0.55, b.hry * 0.55))
    elif key in ("orc", "maghar_orc"):
        b = Body(h=402, head=(0.05, 0.06), neck=0.0, sh=0.15, waist=0.095, hip=0.095, thigh=0.06, ankle=0.028,
                 arm=0.045, wrist=0.03, hand=0.034, hunch=0.035, reach=0.02, spread=0.016)
        s += b.shapes() + b.tusks(b.hry * 0.7) + b.ears(0.5, 0.3, 0.28)
        if key == "maghar_orc":
            s.append(ell(CX, b.hy - b.hry * 1.15, b.hrx * 0.38, b.hry * 0.55))
            s.append(tail(CX + 2, b.hy - b.hry * 1.2, CX + b.hrx * 2.2, b.shy + 22, 3.5, 2, 12))
        else:
            s += spikes(b, 3, b.hry * 0.7, b.hrx * 0.35)
    elif key in ("troll", "zandalari_troll"):
        tall = 425 if key == "troll" else 438
        b = Body(h=tall, head=(0.046, 0.058), neck=0.005, sh=0.12, waist=0.06, hip=0.075, thigh=0.04, ankle=0.02,
                 arm=0.03, wrist=0.02, hand=0.03, reach=0.06, hunch=0.05 if key == "troll" else 0.015, spread=0.02)
        s += b.shapes() + b.ears(1.05, 0.15, 0.3) + b.tusks(b.hry * 1.3, 0.95)
        if key == "troll":
            s += spikes(b, 4, b.hry * 1.7, b.hrx * 0.5, w=4.5)
        else:
            s.append(poly([(CX - b.hrx * 1.2, b.hy - b.hry * 0.5), (CX + b.hrx * 1.2, b.hy - b.hry * 0.5),
                           (CX + b.hrx * 0.6, b.hy - b.hry * 1.9), (CX, b.hy - b.hry * 2.3), (CX - b.hrx * 0.6, b.hy - b.hry * 1.9)]))
    elif key in ("tauren", "highmountain_tauren"):
        b = Body(h=410, head=(0.06, 0.062), neck=0.0, sh=0.17, waist=0.11, hip=0.105, thigh=0.065, ankle=0.03,
                 arm=0.05, wrist=0.035, hand=0.036, hunch=0.05, belly=0.06, hoof=True, spread=0.016)
        s += b.shapes() + b.ears(0.55, 0.05, 0.3, droop=0.15)
        s.append(ell(CX, b.hy + b.hry * 0.6, b.hrx * 0.7, b.hry * 0.55))       # muzzle
        if key == "tauren":
            s += [horn(b, -1, b.hrx * 1.9, b.hry * 1.7, b.hry * 0.4, 5.5), horn(b, 1, b.hrx * 1.9, b.hry * 1.7, b.hry * 0.4, 5.5)]
        else:
            for side in (-1, 1):
                s.append(horn(b, side, b.hrx * 1.4, b.hry * 2.4, -b.hry * 0.2, 4))
                x0, y0 = CX + side * b.hrx * 1.5, b.hy - b.hry * 1.6
                s.append(poly([(x0 - 2, y0), (x0 + 2, y0), (x0 + side * b.hrx * 0.9, y0 - b.hry * 0.9)]))
                x0, y0 = CX + side * b.hrx * 1.9, b.hy - b.hry * 2.1
                s.append(poly([(x0 - 2, y0), (x0 + 2, y0), (x0 + side * b.hrx * 0.8, y0 - b.hry * 0.4)]))
    elif key in ("draenei", "lightforged_draenei"):
        b = Body(h=418, head=(0.046, 0.06), sh=0.13, waist=0.075, hip=0.085, thigh=0.05, ankle=0.022, arm=0.035,
                 wrist=0.022, hand=0.028, hoof=True)
        s += b.shapes()
        s += [horn(b, -1, b.hrx * 1.3, b.hry * 0.3, b.hry * 1.1, 4), horn(b, 1, b.hrx * 1.3, b.hry * 0.3, b.hry * 1.1, 4)]
        s += [tail(CX - 6, b.hy + b.hry * 0.8, CX - 9, b.hy + b.hry * 2.0, 2.2, 1, -3),
              tail(CX + 6, b.hy + b.hry * 0.8, CX + 9, b.hy + b.hry * 2.0, 2.2, 1, 3)]
        s.append(tail(CX + b.hw * 0.6, b.hipy, CX + b.hw * 2.6, GROUND - 0.12 * b.h, 6, 3, 40))
        if key == "lightforged_draenei":
            s.append(poly([(CX - 5, b.hy - b.hry * 0.8), (CX + 5, b.hy - b.hry * 0.8), (CX, b.hy - b.hry * 2.0)]))
    elif key == "undead":
        b = Body(h=392, head=(0.044, 0.058), neck=0.01, sh=0.11, waist=0.05, hip=0.065, thigh=0.034, ankle=0.017,
                 arm=0.022, wrist=0.014, hand=0.026, hunch=0.05, reach=0.03, spread=0.02)
        s += b.shapes() + [b.hair_dome(1.06, 0.06)]
        s += both([(CX - b.shw * 0.6, b.shy - 6), (CX - b.shw * 1.15, b.shy - 12), (CX - b.shw * 0.95, b.shy + 8)])
        s += claws(b)
    elif key == "worgen":
        b = Body(h=420, head=(0.052, 0.058), neck=-0.005, sh=0.15, waist=0.08, hip=0.09, thigh=0.055, ankle=0.024,
                 arm=0.042, wrist=0.028, hand=0.034, hunch=0.07, reach=0.05, spread=0.02)
        s += b.shapes() + claws(b)
        for side in (-1, 1):
            x0 = CX + side * b.hrx * 0.5
            s.append(poly([(x0 - 6, b.hy - b.hry * 0.6), (x0 + 6, b.hy - b.hry * 0.6), (x0 + side * 7, b.hy - b.hry * 2.0)]))
        s.append(ell(CX, b.hy + b.hry * 0.75, b.hrx * 0.5, b.hry * 0.6))       # snout
        s.append(tail(CX + b.hw * 0.6, b.hipy, CX + b.hw * 2.8, GROUND - 0.14 * b.h, 8, 3, 45))
    elif key == "pandaren":
        b = Body(h=400, head=(0.065, 0.065), neck=0.0, sh=0.15, waist=0.13, hip=0.12, thigh=0.065, ankle=0.032,
                 arm=0.045, wrist=0.032, hand=0.036, belly=0.13, spread=0.016)
        s += b.shapes()
        s += [ell(CX - b.hrx * 0.85, b.hy - b.hry * 0.85, b.hrx * 0.38, b.hrx * 0.38),
              ell(CX + b.hrx * 0.85, b.hy - b.hry * 0.85, b.hrx * 0.38, b.hrx * 0.38)]
    elif key == "vulpera":
        b = Body(h=230, head=(0.085, 0.09), neck=0.0, sh=0.1, waist=0.07, hip=0.085, torso=0.23, thigh=0.05,
                 ankle=0.026, arm=0.032, wrist=0.024, hand=0.032, spread=0.024)
        s += b.shapes()
        for side in (-1, 1):
            x0 = CX + side * b.hrx * 0.55
            s.append(poly([(x0 - side * b.hrx * 0.45, b.hy - b.hry * 0.5), (x0 + side * b.hrx * 0.75, b.hy - b.hry * 0.1),
                           (x0 + side * b.hrx * 1.05, b.hy - b.hry * 2.4)]))
        s.append(tail(CX + b.hw * 0.7, b.hipy - 6, CX + b.hw * 3.0, GROUND - 0.3 * b.h, 14, 4, 40))
    elif key == "dracthyr":
        b = Body(h=415, head=(0.046, 0.058), neck=0.02, sh=0.13, waist=0.075, hip=0.088, thigh=0.055, ankle=0.022,
                 arm=0.035, wrist=0.024, hand=0.03, hoof=True, spread=0.018)
        s += wings(b, b.shw * 1.05, 0.16 * b.h) + b.shapes() + claws(b)
        s += [horn(b, -1, b.hrx * 1.0, b.hry * 1.9, b.hry * 0.3, 4.5), horn(b, 1, b.hrx * 1.0, b.hry * 1.9, b.hry * 0.3, 4.5)]
        s.append(tail(CX + b.hw * 0.6, b.hipy, CX + b.hw * 2.8, GROUND - 0.06 * b.h, 9, 2.5, 35))
    elif key == "haranir":
        b = Body(h=405, head=(0.046, 0.06), sh=0.12, waist=0.068, hip=0.08, arm=0.03, wrist=0.02, thigh=0.046,
                 ankle=0.021, hand=0.026)
        s += b.shapes() + b.ears(1.2, 0.45, 0.3)
        for i, (dx, hgt, lean) in enumerate([(-0.6, 1.5, -0.6), (-0.2, 2.0, -0.2), (0.2, 2.0, 0.2), (0.6, 1.5, 0.6)]):
            x0 = CX + dx * b.hrx
            s.append(ell(x0 + lean * b.hrx * 0.5, b.hy - b.hry * (0.6 + hgt * 0.5), 4.5, b.hry * hgt * 0.55, lean * 25))
    else:
        raise KeyError(key)
    return s


def svg(key: str) -> str:
    body = "\n    ".join(race(key))
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" '
            f'aria-hidden="true">\n'
            '  <defs>\n'
            '    <linearGradient id="fill" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="0" y2="440">\n'
            '      <stop offset="0" stop-color="#9aa6b6"/>\n'
            '      <stop offset="1" stop-color="#3c4552"/>\n'
            '    </linearGradient>\n'
            '  </defs>\n'
            '  <g fill="url(#fill)" stroke="url(#fill)" stroke-width="3" stroke-linejoin="round" opacity="0.7">\n'
            f'    {body}\n'
            '  </g>\n'
            '</svg>\n')


RACES = ("human", "kul_tiran", "dwarf", "dark_iron_dwarf", "earthen", "gnome", "mechagnome", "goblin",
         "night_elf", "blood_elf", "void_elf", "nightborne", "skyborne", "orc", "maghar_orc", "troll",
         "zandalari_troll", "tauren", "highmountain_tauren", "draenei", "lightforged_draenei", "undead", "worgen",
         "pandaren", "vulpera", "dracthyr", "haranir", "generic")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for k in RACES:
        (OUT / f"{k}.svg").write_text(svg(k))
    print(f"wrote {len(RACES)} silhouettes to {OUT}")


if __name__ == "__main__":
    main()
