"""Capture a visible permitted document by dragging its viewport on a Windows desktop.

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
    next_delay: float = 3.0,
    allow_single: bool = False,
    source_url: str = "",
    clean_ui: bool = False,
) -> list[dict]:
    if not 1 <= start <= end:
        raise CaptureError("Page range must be positive and ascending")
    if start != end and not (next_click or next_key):
        raise CaptureError("Multiple pages require --next-click or --next-key")
    out.mkdir(parents=True, exist_ok=True)
    manifest = out / "capture_manifest.json"
    reports: list[dict] = []
    for frame in range(start, end + 1):
        if frame == start and zoom_click and zoom_count:
            for _ in range(zoom_count):
                scanner.desktop.click(*zoom_click)
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
