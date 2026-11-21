"""Stitch consecutive, overlapping screenshot crops by translation only.

Capture in snake order; images must all have the same pixel dimensions.
No rotation, resampling, enhancements, or invented pixels.
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
from PIL import Image

EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff"}


class StitchError(Exception):
    pass


def read_rgb(path: Path) -> np.ndarray:
    with Image.open(path) as image:
        return np.asarray(image.convert("RGB")).copy()


def translation(a: np.ndarray, b: np.ndarray) -> tuple[int, int, int, float]:
    """Return integer offset of B relative to A, feature inliers, and overlap MAD."""
    if a.shape != b.shape:
        raise StitchError("Screenshots must share the same capture rectangle and scale.")
    h, w = a.shape[:2]
    scale = min(1.0, 1500 / max(h, w))
    wa, ha = max(32, round(w * scale)), max(32, round(h * scale))
    ag = cv2.cvtColor(cv2.resize(a, (wa, ha)), cv2.COLOR_RGB2GRAY)
    bg = cv2.cvtColor(cv2.resize(b, (wa, ha)), cv2.COLOR_RGB2GRAY)
    sift = cv2.SIFT_create(nfeatures=5000, contrastThreshold=0.015)
    ka, da = sift.detectAndCompute(ag, None)
    kb, db = sift.detectAndCompute(bg, None)
    if da is None or db is None:
        raise StitchError("Not enough detail in overlapping area; capture a larger overlap.")
    candidates = cv2.BFMatcher(cv2.NORM_L2).knnMatch(da, db, k=2)
    good = [m for m, n in candidates if m.distance < 0.75 * n.distance]
    if len(good) < 10:
        raise StitchError(f"Too few matching details ({len(good)}). Capture ~30% overlap.")
    src = np.float32([kb[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
    dst = np.float32([ka[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
    matrix, mask = cv2.estimateAffinePartial2D(
        src, dst, method=cv2.RANSAC, ransacReprojThreshold=2.5,
        maxIters=3000, confidence=0.999,
    )
    if matrix is None or mask is None:
        raise StitchError("Could not determine screenshot movement.")
    count = int(mask.sum())
    if count < 10:
        raise StitchError(f"Insufficient reliable matches ({count}).")
    if np.max(np.abs(matrix[:, :2] - np.eye(2))) > 0.01:
        raise StitchError("Detected zoom/rotation change. Keep viewer zoom fixed.")
    offsets = (dst[mask[:, 0] == 1, 0] - src[mask[:, 0] == 1, 0]) / scale
    dx, dy = np.rint(np.median(offsets, axis=0)).astype(int)
    if abs(dx) >= w or abs(dy) >= h or (abs(dx) < 3 and abs(dy) < 3):
        raise StitchError(f"No useful overlap or duplicate screenshots ({dx}, {dy}).")
    if (w - abs(dx)) * (h - abs(dy)) < w * h * 0.12:
        raise StitchError(f"Overlap below 12% ({dx}, {dy}).")
    ax, bx = max(0, dx), max(0, -dx)
    ay, by = max(0, dy), max(0, -dy)
    ow, oh = w - abs(dx), h - abs(dy)
    aa = a[ay:ay + oh, ax:ax + ow].astype(np.int16)
    bb = b[by:by + oh, bx:bx + ow].astype(np.int16)
    difference = float(np.abs(aa - bb).mean())
    if difference > 20:
        raise StitchError(f"Overlap does not match (pixel difference {difference:.1f}).")
    return int(dx), int(dy), count, difference


def stitch(paths: list[Path], output: Path) -> dict:
    if not paths:
        raise StitchError("No screenshots found.")
    frames = [read_rgb(p) for p in paths]
    if any(im.shape != frames[0].shape for im in frames):
        raise StitchError("Screenshots have different dimensions.")
    offsets = [(0, 0)]
    for index in range(1, len(frames)):
        try:
            dx, dy, matches, difference = translation(frames[index - 1], frames[index])
        except StitchError as exc:
            raise StitchError(f"{paths[index - 1].name} -> {paths[index].name}: {exc}") from exc
        oldx, oldy = offsets[-1]
        offsets.append((oldx + dx, oldy + dy))
        print(
            f"{paths[index].name}: step=({dx:+d}, {dy:+d}), "
            f"matches={matches}, pixel_diff={difference:.1f}"
        )
    height, width = frames[0].shape[:2]
    minx, miny = min(x for x, _ in offsets), min(y for _, y in offsets)
    maxx = max(x for x, _ in offsets) + width
    maxy = max(y for _, y in offsets) + height
    outw, outh = maxx - minx, maxy - miny
    if outw * outh > 200_000_000:
        raise StitchError("Result exceeds 200 million pixels; split into smaller sheets.")
    canvas = np.zeros((outh, outw, 3), dtype=np.uint8)
    filled = np.zeros((outh, outw), dtype=np.bool_)
    for frame, (x, y) in zip(frames, offsets):
        x -= minx
        y -= miny
        destination = canvas[y:y + height, x:x + width]
        new = ~filled[y:y + height, x:x + width]
        destination[new] = frame[new]  # existing pixels only, no blending
        filled[y:y + height, x:x + width] = True
    missing = int((~filled).sum())
    if missing:
        raise StitchError(f"Missing {missing:,} pixels in assembled bounds.")
    output.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(canvas).save(output, "PNG")
    return {"size": (outw, outh), "images": len(paths), "path": str(output)}
