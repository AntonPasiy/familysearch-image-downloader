"""Capture an open document from a desktop viewport using wheel or drag navigation.

The operator opens the first page and chooses a fixed capture rectangle.
No browser internals, cookies, API endpoints or download controls are accessed.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from screenshot_stitch import StitchError, stitch


class CaptureError(RuntimeError):
    pass


@dataclass(frozen=True)
class Region:
    x: int
    y: int
    width: int
    height: int

    @classmethod
    def parse(cls, text: str) -> "Region":
        try:
            values = [int(p.strip()) for p in text.split(",")]
        except ValueError as exc:
            raise CaptureError("--region must be X,Y,WIDTH,HEIGHT") from exc
        if len(values) != 4 or min(values[:2]) < 0 or min(values[2:]) < 128:
            raise CaptureError("--region must be X,Y,WIDTH,HEIGHT, width/height >= 128")
        return cls(*values)


class Desktop:
    """Thin wrapper around PyAutoGUI. Imported only on an interactive desktop."""

    def __init__(self, pause: float = 0.7):
        import pyautogui  # optional dependency

        pyautogui.FAILSAFE = True  # top-left corner aborts
        self.gui = pyautogui
        self.pause = pause

    def screenshot(self, region: Region) -> Image.Image:
        result = self.gui.screenshot(
            region=(region.x, region.y, region.width, region.height)
        )
        return result.convert("RGB")

    def drag(self, region: Region, dx: int, dy: int) -> None:
        gui = self.gui
        gui.moveTo(
            region.x + region.width // 2,
            region.y + region.height // 2,
        )
        gui.dragRel(dx, dy, duration=0.45, button="left")
        time.sleep(self.pause)

    def wheel_zoom(self, region: Region, steps: int) -> None:
        """Zoom at viewport center using the unmodified mouse wheel."""
        if steps <= 0:
            raise CaptureError("Wheel zoom steps must be positive.")
        self.gui.moveTo(
            region.x + region.width // 2,
            region.y + region.height // 2,
        )
        self.gui.scroll(steps)
        time.sleep(self.pause)

    def modifier_wheel_pan(
        self, region: Region, modifier: str, signed_ticks: int
    ) -> None:
        """Scroll inside the viewport with Shift (horizontal) or Alt (vertical)."""
        if modifier not in {"shift", "alt"}:
            raise CaptureError("Wheel pan modifier must be shift or alt.")
        if not signed_ticks:
            raise CaptureError("Wheel pan requires a nonzero signed tick count.")
        self.gui.moveTo(
            region.x + region.width // 2,
            region.y + region.height // 2,
        )
        self.gui.keyDown(modifier)
        try:
            self.gui.scroll(signed_ticks)
        finally:
            self.gui.keyUp(modifier)
        time.sleep(self.pause)

    def ctrl_wheel_zoom(self, region: Region, steps: int) -> None:
        """Send Ctrl+wheel at the center of the image viewport.

        A positive step count turns the wheel up. Always release Ctrl even
        when PyAutoGUI raises, so subsequent drags do not use a modifier.
        """
        if steps <= 0:
            raise CaptureError("Ctrl+wheel zoom steps must be positive.")
        gui = self.gui
        gui.moveTo(
            region.x + region.width // 2,
            region.y + region.height // 2,
        )
        gui.keyDown("ctrl")
        try:
            gui.scroll(steps)
        finally:
            gui.keyUp("ctrl")
        time.sleep(self.pause)

    def click(self, x: int, y: int) -> None:
        self.gui.click(x, y)
        time.sleep(self.pause)

    def press(self, key: str) -> None:
        self.gui.press(key)
        time.sleep(self.pause)

    def hotkey(self, *keys: str) -> None:
        self.gui.hotkey(*keys)
        time.sleep(self.pause)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class Scanner:
    def __init__(
        self,
        desktop: Desktop,
        region: Region,
        *,
        step: float = 0.65,
        max_cols: int = 12,
        max_rows: int = 12,
        tolerance: float = 1.5,
    ):
        if not 0.4 <= step <= 0.8:
            raise CaptureError("step must be between 0.4 and 0.8")
        self.desktop = desktop
        self.region = region
        self.dx = max(30, int(region.width * step))
        self.dy = max(30, int(region.height * step))
        self.max_cols = max_cols
        self.max_rows = max_rows
        self.tolerance = tolerance

    def grab(self) -> Image.Image:
        return self.desktop.screenshot(self.region)

    def changed(self, before: Image.Image, after: Image.Image) -> bool:
        # Subsampling is enough to detect a stationary viewport.
        a = np.asarray(before.convert("RGB"), dtype=np.int16)[::8, ::8]
        b = np.asarray(after.convert("RGB"), dtype=np.int16)[::8, ::8]
        return float(np.abs(a - b).mean()) > self.tolerance

    def move(self, dx: int, dy: int) -> bool:
        before = self.grab()
        self.desktop.drag(self.region, dx, dy)
        return self.changed(before, self.grab())

    def seek_edge(self, dx: int, dy: int) -> None:
        # Positive document drag exposes its top/left edge.
        for _ in range(max(self.max_cols, self.max_rows) + 3):
            if not self.move(dx, dy):
                return
        raise CaptureError("Document continues beyond the configured pan limit")

    def seek_origin(self) -> None:
        self.seek_edge(+self.dx, 0)
        self.seek_edge(0, +self.dy)
        self.seek_edge(+self.dx, 0)

    def take_page(self, page: int, out: Path, *, allow_single: bool = False) -> dict:
        result_path = out / f"frame_{page:04d}.png"
        if result_path.exists():
            return {"page": page, "status": "already_exists"}
        out.mkdir(parents=True, exist_ok=True)
        raw = out / "raw" / f"frame_{page:04d}"
        raw.mkdir(parents=True, exist_ok=True)
        # Keep the original viewport as diagnostics even if seeking an edge
        # fails before the first numbered screenshot can be captured.
        self.grab().save(raw / "000_preflight.png", "PNG")
        self.seek_origin()
        paths: list[Path] = []
        moved_x = moved_y = False
        for row in range(self.max_rows):
            if row:
                # Stay at the previous horizontal edge: consecutive images
                # overlap vertically, then sweep back across the next row.
                if not self.move(0, -self.dy):
                    break
                moved_y = True
            horizontal_drag = -self.dx if row % 2 == 0 else self.dx
            for col in range(self.max_cols):
                image_path = raw / f"{len(paths) + 1:03d}.png"
                self.grab().save(image_path, "PNG")
                paths.append(image_path)
                if col == self.max_cols - 1:
                    if self.move(horizontal_drag, 0):
                        raise CaptureError("Exceeded max-cols; increase --max-cols")
                    break
                if not self.move(horizontal_drag, 0):
                    break
                moved_x = True
        else:
            if self.move(0, -self.dy):
                raise CaptureError("Exceeded max-rows; increase --max-rows")
        if len(paths) == 1 and not allow_single:
            raise CaptureError(
                "Only one viewport was captured: full-page coverage not proven. "
                "Increase viewer zoom or pass --allow-single after visual inspection."
            )
        try:
            result = stitch(paths, result_path)
        except StitchError as exc:
            raise CaptureError(f"Frame {page}: {exc}") from exc
        return {
            "page": page,
            "status": "stitched",
            "path": str(result_path),
            "tiles": len(paths),
            "size": result["size"],
            "panned": {"horizontal": moved_x, "vertical": moved_y},
        }



class WheelScanner(Scanner):
    """Use modifier+wheel panning while retaining the tested snake scan.

    Positive requested image movement (as with dragging right/down) maps
    to positive wheel ticks unless the corresponding axis is inverted.
    Verify direction in the actual viewer before trusting a full scan.
    """

    def __init__(
        self,
        desktop: Desktop,
        region: Region,
        *,
        wheel_ticks: int = 160,
        horizontal_sign: int = 1,
        vertical_sign: int = 1,
        **kwargs,
    ):
        super().__init__(desktop, region, **kwargs)
        if not 1 <= wheel_ticks <= 400:
            raise CaptureError("--wheel-ticks must be between 1 and 400.")
        if horizontal_sign not in (-1, 1) or vertical_sign not in (-1, 1):
            raise CaptureError("Wheel axis signs must be +1 or -1.")
        self.wheel_ticks = wheel_ticks
        self.horizontal_sign = horizontal_sign
        self.vertical_sign = vertical_sign

    def seek_edge(self, dx: int, dy: int) -> None:
        # Wheel navigation can move only ~1 pixel per notch in this viewer.
        # Keep a generous ceiling and log how long it takes to find the edge.
        axis = "horizontal" if dx else "vertical"
        for attempt in range(1, 41):
            if not self.move(dx, dy):
                print(f"{axis} edge reached after {attempt - 1} wheel pan steps")
                return
        raise CaptureError(
            f"Could not reach {axis} edge within 40 steps "
            f"({self.wheel_ticks} wheel ticks each). "
            "Inspect raw/*/000_preflight.png and increase --wheel-ticks "
            "or verify the viewport is not covered by another panel."
        )

    def move(self, dx: int, dy: int) -> bool:
        if bool(dx) == bool(dy):
            raise CaptureError("Wheel pan must target exactly one axis.")
        before = self.grab()
        if dx:
            modifier = "shift"
            ticks = (1 if dx > 0 else -1) * self.horizontal_sign * self.wheel_ticks
        else:
            modifier = "alt"
            ticks = (1 if dy > 0 else -1) * self.vertical_sign * self.wheel_ticks
        self.desktop.modifier_wheel_pan(self.region, modifier, ticks)
        return self.changed(before, self.grab())


def capture_pages(
    scanner: Scanner,
    *,
    start: int,
    end: int,
    out: Path,
    next_click: tuple[int, int] | None = None,
    next_key: str | None = None,
    zoom_click: tuple[int, int] | None = None,
    zoom_count: int = 0,
    ctrl_wheel_zoom: int = 0,
    wheel_zoom: int = 0,
    next_delay: float = 3.0,
    allow_single: bool = False,
    source_url: str = "",
    clean_ui: bool = False,
) -> list[dict]:
    if not 1 <= start <= end:
        raise CaptureError("Page range must be positive and ascending")
    if start != end and not (next_click or next_key):
        raise CaptureError("Multiple pages require --next-click or --next-key")
    if min(ctrl_wheel_zoom, wheel_zoom, zoom_count) < 0:
        raise CaptureError("Zoom step counts cannot be negative.")
    if sum(bool(value) for value in (ctrl_wheel_zoom, wheel_zoom, zoom_count)) > 1:
        raise CaptureError("Use only one zoom method per run.")
    out.mkdir(parents=True, exist_ok=True)
    manifest = out / "capture_manifest.json"
    reports: list[dict] = []
    for frame in range(start, end + 1):
        if frame == start:
            if zoom_click and zoom_count:
                for _ in range(zoom_count):
                    scanner.desktop.click(*zoom_click)
            if ctrl_wheel_zoom or wheel_zoom:
                before_zoom = scanner.grab()
                if wheel_zoom:
                    scanner.desktop.wheel_zoom(scanner.region, wheel_zoom)
                else:
                    scanner.desktop.ctrl_wheel_zoom(scanner.region, ctrl_wheel_zoom)
                if not scanner.changed(before_zoom, scanner.grab()):
                    raise CaptureError(
                        "Mouse-wheel zoom did not visibly change the image viewport. "
                        "Check focus and the viewer's wheel controls."
                    )
        if clean_ui:
            scanner.desktop.hotkey("ctrl", "alt", "h")
            scanner.desktop.sleep(0.5)
        try:
            report = scanner.take_page(frame, out, allow_single=allow_single)
        finally:
            if clean_ui:
                # Restore HUD for the Next button; also restore on capture errors.
                scanner.desktop.hotkey("ctrl", "alt", "h")
                scanner.desktop.sleep(0.5)
        report["viewer_frame_number_verified"] = False  # counter not parsed
        reports.append(report)
        manifest.write_text(
            json.dumps(
                {"source_url": source_url, "frames": reports},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"Frame {frame}: {report['status']}; tiles={report.get('tiles', 0)}")
        if frame < end:
            scanner.seek_origin()
            before = scanner.grab()
            if next_click:
                scanner.desktop.click(*next_click)
            else:
                scanner.desktop.press(next_key)
            scanner.desktop.sleep(next_delay)
            scanner.seek_origin()
            if not scanner.changed(before, scanner.grab()):
                raise CaptureError(
                    f"Navigation after frame {frame} could not be confirmed. "
                    "Inspect the viewer before continuing."
                )
    return reports
