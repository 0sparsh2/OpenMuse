"""Render the OpenMuse PWA icons (orbit mark) as PNGs — no image libraries needed.

    python client/make_icons.py      # writes client/web/icons/*.png
"""
import math
import os
import struct
import zlib

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "icons")
BLUE, WHITE = (29, 78, 216), (255, 255, 255)
COS, SIN = math.cos(math.radians(24)), math.sin(math.radians(24))


def glyph(x, y):
    """Coverage of the mark at (x, y) in the 32-unit logo space."""
    dx, dy = x - 16, y - 16
    if dx * dx + dy * dy <= 36:                                    # core
        return True
    if (x - 27) ** 2 + (y - 10) ** 2 <= 2.4 ** 2:                 # moon
        return True
    u, v = dx * COS - dy * SIN, dx * SIN + dy * COS               # rotate +24° (undo -24°)
    return abs(math.hypot(u / 13, v / 6) - 1) * min(13, 6) * 1.5 <= 1.0  # ring (stroke ~2)


def render(size, *, maskable):
    ss, rows = 3, []
    scale = 0.62 if maskable else 0.74                             # maskable: mark inside the 80% safe zone
    radius = 0 if maskable else size * 0.22
    for py in range(size):
        row = bytearray([0])
        for px in range(size):
            # rounded-square background (full-bleed when maskable)
            cx, cy = min(px, size - 1 - px), min(py, size - 1 - py)
            inside_bg = not (cx < radius and cy < radius and math.hypot(radius - cx, radius - cy) > radius)
            hits = 0
            for sy in range(ss):
                for sx in range(ss):
                    lx = ((px + (sx + .5) / ss) / size - .5) / scale * 32 + 16
                    ly = ((py + (sy + .5) / ss) / size - .5) / scale * 32 + 16
                    hits += glyph(lx, ly)
            a = hits / (ss * ss)
            if not inside_bg:
                row += bytes((0, 0, 0, 0))
            else:
                row += bytes(round(BLUE[i] * (1 - a) + WHITE[i] * a) for i in range(3)) + b"\xff"
        rows.append(bytes(row))
    raw = zlib.compress(b"".join(rows), 9)
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", raw) + chunk(b"IEND", b""))


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    for name, size, mask in [("icon-192.png", 192, False), ("icon-512.png", 512, False),
                             ("maskable-512.png", 512, True), ("apple-touch-icon.png", 180, True),
                             ("badge-96.png", 96, False)]:
        with open(os.path.join(OUT, name), "wb") as fh:
            fh.write(render(size, maskable=mask))
        print("wrote", name)
