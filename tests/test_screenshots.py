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
    from screenshot_capture import CaptureError, Desktop, Region, Scanner, WheelScanner, capture_pages
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

    def test_ctrl_wheel_zoom_releases_modifier_on_failure(self):
        class FakeGui:
            def __init__(self):
                self.events = []

            def moveTo(self, x, y):
                self.events.append(("move", x, y))

            def keyDown(self, key):
                self.events.append(("down", key))

            def scroll(self, steps):
                self.events.append(("scroll", steps))
                raise RuntimeError("simulated mouse failure")

            def keyUp(self, key):
                self.events.append(("up", key))

        desktop = Desktop.__new__(Desktop)
        desktop.gui = FakeGui()
        desktop.pause = 0
        with self.assertRaisesRegex(RuntimeError, "simulated mouse failure"):
            desktop.ctrl_wheel_zoom(Region(100, 200, 800, 600), 3)
        self.assertEqual(
            desktop.gui.events,
            [("move", 500, 500), ("down", "ctrl"), ("scroll", 3), ("up", "ctrl")],
        )

    def test_ctrl_wheel_zoom_precedes_snake_capture(self):
        class FakeDesktop:
            def __init__(self):
                self.events = []

            def ctrl_wheel_zoom(self, region, steps):
                self.events.append(("ctrl_wheel_zoom", steps))

        class FakeScanner:
            def __init__(self, changed=True):
                self.desktop = FakeDesktop()
                self.region = Region(20, 40, 400, 300)
                self.has_changed = changed

            def grab(self):
                self.desktop.events.append(("grab",))
                return None

            def changed(self, before, after):
                self.desktop.events.append(("changed",))
                return self.has_changed

            def take_page(self, page, out, allow_single=False):
                self.desktop.events.append(("capture", page))
                return {"page": page, "status": "stitched", "tiles": 3}

        with tempfile.TemporaryDirectory() as directory:
            scanner = FakeScanner()
            reports = capture_pages(
                scanner, start=600, end=600, out=Path(directory),
                ctrl_wheel_zoom=4,
            )
            self.assertEqual(reports[0]["page"], 600)
            self.assertEqual(
                scanner.desktop.events,
                [
                    ("grab",), ("ctrl_wheel_zoom", 4), ("grab",),
                    ("changed",), ("capture", 600),
                ],
            )
            no_change_scanner = FakeScanner(changed=False)
            with self.assertRaisesRegex(CaptureError, "did not visibly change"):
                capture_pages(
                    no_change_scanner, start=600, end=600,
                    out=Path(directory), ctrl_wheel_zoom=2,
                )
            self.assertNotIn(("capture", 600), no_change_scanner.desktop.events)

    def test_ctrl_wheel_and_button_zoom_are_mutually_exclusive(self):
        class FakeScanner:
            pass

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(CaptureError, "only one zoom method"):
                capture_pages(
                    FakeScanner(), start=600, end=600, out=Path(directory),
                    ctrl_wheel_zoom=2, zoom_count=2, zoom_click=(10, 10),
                )

    def test_plain_wheel_zoom_and_modifier_pan_release_keys(self):
        class Gui:
            def __init__(self):
                self.events = []

            def moveTo(self, *xy):
                self.events.append(("move", *xy))

            def scroll(self, ticks):
                self.events.append(("scroll", ticks))

            def keyDown(self, key):
                self.events.append(("down", key))

            def keyUp(self, key):
                self.events.append(("up", key))

        desktop = Desktop.__new__(Desktop)
        desktop.gui = Gui()
        desktop.pause = 0
        region = Region(100, 200, 500, 360)
        desktop.wheel_zoom(region, 2)
        desktop.modifier_wheel_pan(region, "shift", -3)
        desktop.modifier_wheel_pan(region, "alt", 3)
        self.assertEqual(
            desktop.gui.events,
            [
                ("move", 350, 380), ("scroll", 2),
                ("move", 350, 380), ("down", "shift"),
                ("scroll", -3), ("up", "shift"),
                ("move", 350, 380), ("down", "alt"),
                ("scroll", 3), ("up", "alt"),
            ],
        )

    def test_wheel_scanner_snake_covers_all_edges(self):
        source = self.source()

        class WheelDesktop:
            def __init__(self):
                self.x, self.y = 90, 70
                self.commands = []

            def screenshot(self, region):
                return source.crop(
                    (self.x, self.y, self.x + region.width, self.y + region.height)
                )

            def modifier_wheel_pan(self, region, modifier, ticks):
                self.commands.append((modifier, ticks))
                travel = ticks * 85
                if modifier == "shift":
                    self.x = max(0, min(source.width - region.width, self.x - travel))
                elif modifier == "alt":
                    self.y = max(0, min(source.height - region.height, self.y - travel))
                else:
                    raise AssertionError("unexpected modifier")

        with tempfile.TemporaryDirectory() as directory:
            desktop = WheelDesktop()
            scanner = WheelScanner(
                desktop, Region(0, 0, 500, 360),
                wheel_ticks=3, max_cols=7, max_rows=7,
            )
            result = scanner.take_page(600, Path(directory))
            self.assertEqual(result["status"], "stitched")
            self.assertTrue(any(key == "shift" for key, _ in desktop.commands))
            self.assertTrue(any(key == "alt" for key, _ in desktop.commands))
            with Image.open(result["path"]) as actual:
                self.assertEqual(actual.size, source.size)
                self.assertIsNone(ImageChops.difference(actual, source).getbbox())

    def test_inverted_wheel_direction_is_configurable(self):
        class WheelDesktop:
            def __init__(self):
                self.commands = []

            def screenshot(self, region):
                return Image.new("RGB", (region.width, region.height), "black")

            def modifier_wheel_pan(self, region, modifier, ticks):
                self.commands.append((modifier, ticks))

        desktop = WheelDesktop()
        scanner = WheelScanner(
            desktop, Region(0, 0, 400, 300), wheel_ticks=2,
            horizontal_sign=-1, vertical_sign=1,
        )
        self.assertFalse(scanner.move(+100, 0))
        self.assertFalse(scanner.move(0, -100))
        self.assertEqual(desktop.commands, [("shift", -2), ("alt", -2)])

    def test_slow_wheel_panning_reaches_edge_after_more_than_15_steps(self):
        import numpy as np

        source = Image.fromarray(
            np.random.default_rng(15).integers(
                0, 256, size=(280, 4200, 3), dtype=np.uint8
            )
        )

        class SlowWheelDesktop:
            def __init__(self):
                self.x = 2900
                self.wheel_calls = 0

            def screenshot(self, region):
                return source.crop(
                    (self.x, 0, self.x + region.width, region.height)
                )

            def modifier_wheel_pan(self, region, modifier, ticks):
                self.wheel_calls += 1
                assert modifier == "shift"
                # The user observed only ~1 pixel of travel per wheel notch.
                self.x = max(0, min(4200 - region.width, self.x - ticks))

        desktop = SlowWheelDesktop()
        scanner = WheelScanner(desktop, Region(0, 0, 400, 250))
        self.assertEqual(scanner.wheel_ticks, 160)
        scanner.seek_edge(+scanner.dx, 0)
        self.assertEqual(desktop.x, 0)
        self.assertGreater(desktop.wheel_calls, 15)

    def test_preflight_is_saved_even_if_edge_seeking_fails(self):
        class FailingScanner(Scanner):
            def seek_origin(self):
                raise CaptureError("simulated edge seeking failure")

            def grab(self):
                return Image.new("RGB", (400, 300), "white")

        with tempfile.TemporaryDirectory() as directory:
            scanner = FailingScanner(
                object(), Region(0, 0, 400, 300),
            )
            with self.assertRaises(CaptureError):
                scanner.take_page(600, Path(directory))
            diagnostic = (
                Path(directory) / "raw" / "frame_0600" / "000_preflight.png"
            )
            self.assertTrue(diagnostic.is_file())
            with Image.open(diagnostic) as image:
                self.assertEqual(image.size, (400, 300))

    def test_preview_only_does_not_move_the_page(self):
        import cli
        from unittest.mock import patch

        class PreviewDesktop:
            def __init__(self):
                self.sleeps = 0

            def sleep(self, duration):
                self.sleeps += 1

            def screenshot(self, region):
                return Image.new("RGB", (region.width, region.height), "blue")

        desktop = PreviewDesktop()
        with tempfile.TemporaryDirectory() as directory:
            argv = [
                "fsd", "capture", "--frames", "600-600",
                "--region", "295,365,954,600",
                "--preview-only", "--out", directory,
            ]
            with (
                patch("sys.argv", argv),
                patch("screenshot_capture.Desktop", return_value=desktop),
            ):
                self.assertEqual(cli.main(), 0)
            target = Path(directory) / "preview.png"
            self.assertTrue(target.is_file())
            self.assertEqual(desktop.sleeps, 1)
            with Image.open(target) as image:
                self.assertEqual(image.size, (954, 600))

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
