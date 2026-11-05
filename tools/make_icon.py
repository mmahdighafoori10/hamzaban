"""Draws the Hamzaban mark (two voices, one shared language) and writes it as SVG, PNG sizes and a Windows .ico. Standard library only.

    python tools/make_icon.py [OUT_FOLDER] [--preview]          default folder: engine/assets next to this tool

The mark is two overlapping speech bubbles: teal and amber are two agents, the cream lens where they overlap is the language they share.
Everything is drawn from the numbers below, so the SVG and the pixel versions always agree. --preview adds a contact sheet (preview.png)
that shows the mark at every size on a light and a dark ground, which is the quick way to check that it still reads at 16 pixels.
"""
from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

S = 512                                   # the drawing is made on a 512 x 512 square
TILE_R = 120                              # corner radius of the tile
DISC_A = (192.0, 232.0, 128.0)            # centre x, centre y, radius: the teal voice
DISC_B = (320.0, 232.0, 128.0)            # the amber voice
TAIL_A = ((128, 343), (84, 412), (225, 356))
TAIL_B = ((384, 343), (428, 412), (287, 356))
BG_TOP, BG_BOTTOM = (0x3B, 0x37, 0x8F), (0x1C, 0x1A, 0x5E)
TEAL, AMBER, LENS = (0x22, 0xC7, 0xB8), (0xFF, 0xB3, 0x47), (0xFF, 0xFD, 0xF5)
SIZES = (16, 24, 32, 48, 64, 128, 256)


def hexc(c) -> str:
    return "#%02x%02x%02x" % c


def svg() -> str:
    poly = lambda t: " ".join(f"{x} {y}" for x, y in t)  # noqa: E731
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {S} {S}" role="img" aria-label="همزبان · Hamzaban">
  <defs>
    <linearGradient id="hz-bg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="{hexc(BG_TOP)}"/>
      <stop offset="1" stop-color="{hexc(BG_BOTTOM)}"/>
    </linearGradient>
    <clipPath id="hz-left"><circle cx="{DISC_A[0]:g}" cy="{DISC_A[1]:g}" r="{DISC_A[2]:g}"/></clipPath>
  </defs>
  <rect width="{S}" height="{S}" rx="{TILE_R}" fill="url(#hz-bg)"/>
  <path d="M{poly(TAIL_A)} Z" fill="{hexc(TEAL)}"/>
  <circle cx="{DISC_A[0]:g}" cy="{DISC_A[1]:g}" r="{DISC_A[2]:g}" fill="{hexc(TEAL)}"/>
  <path d="M{poly(TAIL_B)} Z" fill="{hexc(AMBER)}"/>
  <circle cx="{DISC_B[0]:g}" cy="{DISC_B[1]:g}" r="{DISC_B[2]:g}" fill="{hexc(AMBER)}"/>
  <circle cx="{DISC_B[0]:g}" cy="{DISC_B[1]:g}" r="{DISC_B[2]:g}" fill="{hexc(LENS)}" clip-path="url(#hz-left)"/>
</svg>
"""


# ------------------------------------------------------------------------------------------------ the drawing, point by point
def in_tile(x: float, y: float) -> bool:
    ox = max(abs(x - S / 2) - (S / 2 - TILE_R), 0.0)
    oy = max(abs(y - S / 2) - (S / 2 - TILE_R), 0.0)
    return ox * ox + oy * oy <= TILE_R * TILE_R


def in_disc(d, x: float, y: float) -> bool:
    return (x - d[0]) ** 2 + (y - d[1]) ** 2 <= d[2] ** 2


def in_tri(t, x: float, y: float) -> bool:
    (x1, y1), (x2, y2), (x3, y3) = t
    d1 = (x - x2) * (y1 - y2) - (x1 - x2) * (y - y2)
    d2 = (x - x3) * (y2 - y3) - (x2 - x3) * (y - y3)
    d3 = (x - x1) * (y3 - y1) - (x3 - x1) * (y - y1)
    return not ((d1 < 0 or d2 < 0 or d3 < 0) and (d1 > 0 or d2 > 0 or d3 > 0))


def shade(x: float, y: float):
    """The colour of the drawing at one point of the 512 square, or None outside the tile."""
    if not in_tile(x, y):
        return None
    k = y / S
    col = tuple(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * k for i in range(3))
    in_a = in_disc(DISC_A, x, y)
    in_b = in_disc(DISC_B, x, y)
    if in_a or in_tri(TAIL_A, x, y):
        col = TEAL
    if in_b or in_tri(TAIL_B, x, y):
        col = AMBER
    if in_a and in_b:
        col = LENS
    return col


def raster(size: int, ss: int = 4) -> list[tuple[int, int, int, int]]:
    """size x size RGBA pixels, anti-aliased by averaging ss x ss samples per pixel (premultiplied, so the rounded corners stay clean)."""
    scale = S / size
    offs = [((i + 0.5) / ss) * scale for i in range(ss)]
    pixels = []
    for py in range(size):
        for px in range(size):
            r = g = b = a = 0.0
            for oy in offs:
                y = py * scale + oy
                for ox in offs:
                    c = shade(px * scale + ox, y)
                    if c is not None:
                        r += c[0]
                        g += c[1]
                        b += c[2]
                        a += 1.0
            n = ss * ss
            if a == 0:
                pixels.append((0, 0, 0, 0))
            else:
                pixels.append((round(r / a), round(g / a), round(b / a), round(255 * a / n)))
    return pixels


# ------------------------------------------------------------------------------------------------ file formats
def png(size: int, pixels) -> bytes:
    raw = bytearray()
    for y in range(size):
        raw.append(0)
        for x in range(size):
            raw += bytes(pixels[y * size + x])

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


def ico(images: dict[int, bytes]) -> bytes:
    """A Windows icon that holds one PNG per size (Windows Vista and later read these directly)."""
    head = struct.pack("<HHH", 0, 1, len(images))
    entries, blobs, offset = b"", b"", 6 + 16 * len(images)
    for size in sorted(images):
        data = images[size]
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset)
        blobs += data
        offset += len(data)
    return head + entries + blobs


def sheet(images: dict[int, list]) -> bytes:
    """A contact sheet: every size on a light and a dark ground."""
    pad = 20
    width = pad + sum(s + pad for s in images)
    height = 2 * (256 + 2 * pad)
    canvas = []
    for y in range(height):
        bg = (246, 244, 239) if y < height // 2 else (20, 23, 28)
        canvas.extend([bg + (255,)] * width)
    for band, bg in ((0, (246, 244, 239)), (1, (20, 23, 28))):
        x0 = pad
        for size in sorted(images):
            y0 = band * (256 + 2 * pad) + pad + (256 - size)
            for y in range(size):
                for x in range(size):
                    r, g, b, a = images[size][y * size + x]
                    if a:
                        dst = canvas[(y0 + y) * width + x0 + x]
                        k = a / 255
                        canvas[(y0 + y) * width + x0 + x] = (round(r * k + dst[0] * (1 - k)), round(g * k + dst[1] * (1 - k)),
                                                             round(b * k + dst[2] * (1 - k)), 255)
            x0 += size + pad
    return png_wh(width, height, canvas)


def png_wh(width: int, height: int, pixels) -> bytes:
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        for x in range(width):
            raw += bytes(pixels[y * width + x])

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "engine" / "assets"
    out.mkdir(parents=True, exist_ok=True)
    pixels = {s: raster(s) for s in SIZES}
    pngs = {s: png(s, pixels[s]) for s in SIZES}
    (out / "hamzaban.svg").write_text(svg(), encoding="utf-8", newline="\n")
    (out / "hamzaban.ico").write_bytes(ico(pngs))
    (out / "hamzaban-256.png").write_bytes(pngs[256])
    names = "hamzaban.svg, hamzaban.ico, hamzaban-256.png"
    if "--preview" in sys.argv:
        (out / "preview.png").write_bytes(sheet(pixels))
        names += " and preview.png"
    print(f"wrote {names} into {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
