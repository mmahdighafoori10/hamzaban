"""The icon: tools/make_icon.py must keep producing a valid SVG, PNG and ICO, and the files shipped in engine/assets must be valid too.
Structure only, never bytes: the compressed bytes of a PNG depend on the zlib build, the pictures must not."""
import importlib.util
import struct
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "make_icon.py"
SHIPPED = ROOT / "engine" / "assets"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def load_tool():
    spec = importlib.util.spec_from_file_location("make_icon", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def png_size(case, data: bytes) -> tuple[int, int]:
    """Walk every chunk, check every CRC, inflate the pixels and check that there are as many as the header says."""
    case.assertEqual(data[:8], PNG_SIGNATURE)
    pos, size, idat, kinds = 8, None, b"", []
    while pos < len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        (crc,) = struct.unpack(">I", data[pos + 8 + length:pos + 12 + length])
        case.assertEqual(crc, zlib.crc32(kind + body) & 0xFFFFFFFF, f"bad checksum in {kind!r}")
        kinds.append(kind)
        if kind == b"IHDR":
            size = struct.unpack(">II", body[:8])
        elif kind == b"IDAT":
            idat += body
        pos += 12 + length
    case.assertEqual((kinds[0], kinds[-1]), (b"IHDR", b"IEND"))
    case.assertEqual(len(zlib.decompress(idat)), size[1] * (1 + 4 * size[0]), "one filter byte and four bytes per pixel in every row")
    return size


def ico_sizes(case, data: bytes) -> list[int]:
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    case.assertEqual((reserved, kind), (0, 1), "an icon, not a cursor")
    sizes = []
    for i in range(count):
        w, h, _colours, _reserved, _planes, _bits, length, offset = struct.unpack("<BBBBHHII", data[6 + 16 * i:22 + 16 * i])
        w, h = w or 256, h or 256                       # 0 stands for 256
        blob = data[offset:offset + length]
        case.assertEqual(len(blob), length, "the entry points inside the file")
        case.assertEqual(png_size(case, blob), (w, h), "each entry holds a PNG of the size its directory line says")
        sizes.append(w)
    return sizes


class Generator(unittest.TestCase):
    def test_the_tool_writes_a_valid_svg_png_and_ico(self):
        tool = load_tool()
        with tempfile.TemporaryDirectory(prefix="hz-icon-") as tmp, mock.patch.object(sys, "argv", ["make_icon.py", tmp]):
            self.assertEqual(tool.main(), 0)
            out = Path(tmp)
            self.assertEqual(sorted(p.name for p in out.iterdir()), ["hamzaban-256.png", "hamzaban.ico", "hamzaban.svg"])
            self.assertTrue(ET.fromstring((out / "hamzaban.svg").read_text(encoding="utf-8")).tag.endswith("svg"))
            self.assertEqual(png_size(self, (out / "hamzaban-256.png").read_bytes()), (256, 256))
            self.assertEqual(ico_sizes(self, (out / "hamzaban.ico").read_bytes()), sorted(tool.SIZES))

    def test_the_pictures_are_not_empty_and_not_flat(self):
        tool = load_tool()
        pixels = tool.raster(64)
        self.assertEqual(len(pixels), 64 * 64)
        self.assertTrue(any(p[3] == 0 for p in pixels), "there is transparent space around the mark")
        self.assertTrue(any(p[3] == 255 for p in pixels), "and something solid in it")
        self.assertGreater(len({tuple(p) for p in pixels}), 3, "more than a flat blob")


class Shipped(unittest.TestCase):
    """What the installer puts on the user's desktop is what is checked in."""

    def test_the_icon_files_are_there_and_valid(self):
        tool = load_tool()
        for name in ("hamzaban.svg", "hamzaban.ico", "hamzaban-256.png"):
            self.assertTrue((SHIPPED / name).is_file(), f"engine/assets/{name} is missing: run python tools/make_icon.py")
        self.assertTrue(ET.fromstring((SHIPPED / "hamzaban.svg").read_text(encoding="utf-8")).tag.endswith("svg"))
        self.assertEqual(png_size(self, (SHIPPED / "hamzaban-256.png").read_bytes()), (256, 256))
        self.assertEqual(ico_sizes(self, (SHIPPED / "hamzaban.ico").read_bytes()), sorted(tool.SIZES))


if __name__ == "__main__":
    unittest.main()
