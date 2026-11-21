"""Offline screenshot-stitching and simulated capture regression tests."""
from __future__ import annotations

import random
import tempfile
import unittest
from pathlib import Path

try:
    import cv2  # noqa: F401
    import numpy  # noqa: F401
except ImportError:
    cv2 = None

if cv2 is not None:
    from PIL import Image, ImageChops, ImageDraw
    from screenshot_capture import CaptureError, Region, Scanner, capture_pages
    from screenshot_stitch import StitchError, stitch


@unittest.skipIf(cv2 is None, "Optional screenshot dependencies not installed")
class ScreenshotTests(unittest.TestCase):
    @staticmethod
    def source() -> Image.Image:
        rng = random.Random(173)
        image = Image.new("RGB", (1040, 770), "#e1d6bb")
        draw = ImageDraw.Draw(image)
        for _ in range(500):
            x, y = rng.randrange(1020), rng.randrange(750)
            size = rng.randrange(3, 28)
            draw.ellipse(
                (x, y, x + size, y + size),
                outline=(rng.randrange(30, 150), rng.randrange(10, 120), 70),
            )
        for i in range(150):
            x, y = rng.randrange(980), rng.randrange(740)
            draw.text((x, y), f"{i:03d}/{i * i:06d}", fill="#37291d")
        return image

    def test_stitch_preserves_source_pixels(self):
        source = self.source()
        positions = [
            (0, 0), (350, 0), (540, 0), (540, 260), (190, 260),
            (0, 260), (0, 410), (350, 410), (540, 410),
        ]
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            paths = []
            for index, (x, y) in enumerate(positions, 1):
                path = folder / f"{index:03d}.png"
                source.crop((x, y, x + 500, y + 360)).save(path)
                paths.append(path)
            target = folder / "output.png"
            result = stitch(paths, target)
            self.assertEqual(tuple(result["size"]), source.size)
            with Image.open(target) as actual:
                self.assertIsNone(ImageChops.difference(actual, source).getbbox())

    def test_rejects_nonoverlapping_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            a, b = folder / "a.png", folder / "b.png"
            Image.new("RGB", (400, 300), "red").save(a)
            Image.new("RGB", (400, 300), "blue").save(b)
            with self.assertRaises(StitchError):
                stitch([a, b], folder / "output.png")

    def test_region_validation(self):
        self.assertEqual(Region.parse("20,30,640,480"), Region(20, 30, 640, 480))
        for value in ["20,30,100", "1,2,80,80", "a,b,c,d", "-1,0,200,200"]:
            with self.subTest(value=value), self.assertRaises(CaptureError):
                Region.parse(value)

    def test_clean_view_switches_on_and_restores_even_after_error(self):
        class FakeDesktop:
            def __init__(self):
                self.shortcuts = []

            def hotkey(self, *keys):
                self.shortcuts.append(keys)

            def sleep(self, seconds):
                pass

        class FakeScanner:
            def __init__(self, should_fail):
                self.desktop = FakeDesktop()
                self.should_fail = should_fail

            def take_page(self, page, out, allow_single=False):
                if self.should_fail:
                    raise CaptureError("simulated capture failure")
                return {"page": page, "status": "stitched", "tiles": 2}

        expected = [("ctrl", "alt", "h"), ("ctrl", "alt", "h")]
        for should_fail in (False, True):
            with self.subTest(should_fail=should_fail):
                scanner = FakeScanner(should_fail)
                with tempfile.TemporaryDirectory() as directory:
                    if should_fail:
                        with self.assertRaises(CaptureError):
                            capture_pages(
                                scanner, start=600, end=600,
                                out=Path(directory), clean_ui=True,
                            )
                    else:
                        result = capture_pages(
                            scanner, start=600, end=600,
                            out=Path(directory), clean_ui=True,
                        )
                        self.assertEqual(result[0]["page"], 600)
                    self.assertEqual(scanner.desktop.shortcuts, expected)

    def test_snake_capture_from_simulated_viewer(self):
        source = self.source()

        class FakeDesktop:
            def __init__(self):
                self.image = source
                self.x, self.y = 180, 100

            def screenshot(self, region):
                return self.image.crop(
                    (self.x, self.y, self.x + region.width, self.y + region.height)
                )

            def drag(self, region, dx, dy):
                self.x = max(0, min(self.image.width - region.width, self.x - dx))
                self.y = max(0, min(self.image.height - region.height, self.y - dy))

        with tempfile.TemporaryDirectory() as directory:
            scanner = Scanner(
                FakeDesktop(), Region(0, 0, 500, 360), max_cols=5, max_rows=5
            )
            result = scanner.take_page(600, Path(directory))
            self.assertEqual(result["status"], "stitched")
            with Image.open(result["path"]) as actual:
                self.assertEqual(actual.size, source.size)
                self.assertIsNone(ImageChops.difference(actual, source).getbbox())


if __name__ == "__main__":
    unittest.main()
