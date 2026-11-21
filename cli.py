from __future__ import annotations

import argparse
import getpass
import random
import re
import sys
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from downloader import (
    DownloadError,
    ImageGroup,
    SessionRejected,
    atomic_output,
    clear_fssessionid,
    download_document,
    fetch_image_group,
    is_complete_image,
    is_group_url,
    load_fssessionid,
    parse_familysearch_url,
    save_fssessionid,
    validate_source_url,
)

DEFAULT_OUTPUT_DIR = Path("downloads")
PAGE_PAUSE_RANGE = (4.0, 10.0)


def console_progress(value: float, message: str) -> None:
    if sys.stdout.isatty():
        sys.stdout.write(f"\r{value:5.1%}  {message:<64}")
        sys.stdout.flush()
    elif not message.startswith("Downloaded tiles:"):
        print(f"{value:5.1%}  {message}")


def ask_fssessionid() -> str:
    print(
        "Open a document page in your signed-in browser: DevTools -> Network -> "
        "a familysearch.org request -> Cookies -> the fssessionid value."
    )
    while True:
        raw = getpass.getpass("fssessionid: ").strip()
        if not raw:
            print("Empty value, try again.")
            continue
        try:
            save_fssessionid(raw)
        except DownloadError as error:
            print(error)
            continue
        return load_fssessionid() or raw


def get_session(interactive: bool) -> str:
    value = load_fssessionid()
    if value:
        print("Using the saved fssessionid.")
        return value
    if not interactive:
        raise DownloadError("No saved fssessionid. Run 'fsd login' first.")
    return ask_fssessionid()


def collect_urls(args: argparse.Namespace) -> list[str]:
    urls: list[str] = list(args.urls)
    if args.from_file:
        content = args.from_file.read_text(encoding="utf-8-sig")
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            links = re.findall(r"https?://[^\s<>\[\]\"|)]+", line)
            if links:
                urls.extend(links)
            elif args.from_file.suffix.lower() not in {".md", ".markdown"}:
                urls.append(line)
    if not urls:
        raise DownloadError("No URLs. Pass them as arguments or use --from-file.")
    return list(dict.fromkeys(url.strip() for url in urls))


def parse_frame_range(spec: str) -> tuple[int, int]:
    """Parse inclusive, one-based viewer frame numbers (e.g. 600-620)."""
    match = re.fullmatch(r"([0-9]+)-([0-9]+)", spec.strip())
    if not match:
        raise DownloadError("--frames must be an inclusive range like 600-620.")
    start, end = map(int, match.groups())
    if start < 1 or end < start:
        raise DownloadError("--frames must start at 1 or later, with start <= end.")
    return start, end


def select_group_frames(group: ImageGroup, frames: tuple[int, int] | None) -> list[str]:
    if frames is None:
        return group.image_urls
    start, end = frames
    if end > len(group.image_urls):
        raise DownloadError(
            f"Requested frames {start}-{end}, but the group lists only "
            f"{len(group.image_urls)} image(s)."
        )
    selected: dict[int, str] = {}
    for url in group.image_urls:
        number = parse_familysearch_url(url).page_number
        if number is not None and start <= number <= end:
            selected[number] = url
    missing = [str(number) for number in range(start, end + 1) if number not in selected]
    if missing:
        raise DownloadError("Image group is missing requested frame(s): " + ", ".join(missing[:10]))
    return [selected[number] for number in range(start, end + 1)]


def _write_archive_info(
    directory: Path, group: ImageGroup, frames: tuple[int, int] | None = None
) -> None:
    lines = [
        f"Title: {group.title or '-'}",
        f"Archival reference: {group.reference or '-'}",
        f"Image group number: {group.dgs_number}",
        f"Group id: {group.group_id}",
        f"Images: {len(group.image_urls)}",
        f"Selected frames: {frames[0]}-{frames[1]}" if frames else "Selected frames: all",
        f"Source: {group.source_url}",
        "",
    ]
    with atomic_output(directory / "info.txt") as temporary:
        temporary.write_text("\n".join(lines), encoding="utf-8")


@dataclass
class BatchResult:
    saved: int = 0
    skipped: int = 0
    failed: int = 0


def download_urls(args: argparse.Namespace) -> BatchResult:
    urls = collect_urls(args)
    for url in urls:
        validate_source_url(url)
    frames = parse_frame_range(args.frames) if args.frames is not None else None
    if frames is not None and any(not is_group_url(url) for url in urls):
        raise DownloadError("--frames requires an image group URL (groupId or imageGroupNumbers).")
    result = BatchResult()
    interactive = sys.stdin.isatty()
    session_value = get_session(interactive)
    base_output: Path = args.out
    base_output.mkdir(parents=True, exist_ok=True)

    jobs = deque((url, base_output) for url in urls)
    print(f"{len(jobs)} document(s), saving to {base_output.resolve()}")

    def refresh_session() -> str:
        print()
        print("FamilySearch rejected this fssessionid.")
        clear_fssessionid()
        if not interactive:
            raise DownloadError("Run 'fsd login' with a fresh value, then retry.")
        return ask_fssessionid()

    while jobs:
        raw_url, output_dir = jobs[0]
        if is_group_url(raw_url):
            try:
                group = fetch_image_group(raw_url, session_value)
                selected_urls = select_group_frames(group, frames)
            except SessionRejected:
                session_value = refresh_session()
                continue
            except DownloadError as error:
                print(f"Failed: {error}", file=sys.stderr)
                result.failed += 1
                jobs.popleft()
                continue
            group_dir = output_dir / group.folder_name
            legacy_dir = output_dir / group.group_id
            if legacy_dir.is_dir() and legacy_dir != group_dir and not group_dir.exists():
                legacy_dir.rename(group_dir)
            group_dir.mkdir(parents=True, exist_ok=True)
            _write_archive_info(group_dir, group, frames)
            print(
                f"{group.folder_name}: {len(selected_urls)} selected of "
                f"{len(group.image_urls)} image(s), saving to {group_dir.resolve()}"
            )
            jobs.popleft()
            jobs.extendleft((image_url, group_dir) for image_url in reversed(selected_urls))
            continue
        try:
            document = parse_familysearch_url(raw_url)
        except DownloadError as error:
            print(f"Failed: {error}", file=sys.stderr)
            result.failed += 1
            jobs.popleft()
            continue

        output_dir.mkdir(parents=True, exist_ok=True)
        filename = document.filename_for_format(args.format)
        target = output_dir / filename
        legacy = base_output / filename
        if not target.exists() and legacy != target and legacy.is_file():
            legacy.rename(target)
        if is_complete_image(target):
            print(f"Already downloaded: {target.name}")
            result.skipped += 1
            jobs.popleft()
            continue

        try:
            downloaded = download_document(
                raw_url,
                output_dir,
                session_value,
                jpeg_quality=args.quality,
                workers=args.workers,
                progress=console_progress,
                image_format=args.format,
            )
        except SessionRejected:
            session_value = refresh_session()
            continue
        except DownloadError as error:
            print()
            print(f"Failed: {error}", file=sys.stderr)
            result.failed += 1
            jobs.popleft()
            continue

        print()
        print(f"Saved: {downloaded.path} ({downloaded.width} × {downloaded.height})")
        result.saved += 1
        jobs.popleft()

        if jobs:
            pause = random.uniform(*PAGE_PAUSE_RANGE)
            print(f"Next page in {pause:.0f}s...")
            time.sleep(pause)

    print(f"Finished: {result.saved} saved, {result.skipped} skipped, {result.failed} failed.")
    return result


def bounded_integer(minimum: int, maximum: int):
    def parse(value: str) -> int:
        try:
            number = int(value)
        except ValueError as error:
            raise argparse.ArgumentTypeError("expected an integer") from error
        if not minimum <= number <= maximum:
            raise argparse.ArgumentTypeError(f"must be between {minimum} and {maximum}")
        return number

    return parse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fsd",
        description="Download FamilySearch document images as JPEG or PNG files.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("login", help="save a fresh fssessionid value")
    subparsers.add_parser("logout", help="remove the saved fssessionid")

    download = subparsers.add_parser("download", help="download document images")
    download.add_argument("urls", nargs="*", help="FamilySearch document URLs")
    download.add_argument(
        "-f",
        "--from-file",
        type=Path,
        help="URL list or Markdown file (including tables)",
    )
    download.add_argument(
        "-o",
        "--out",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"output directory (default: {DEFAULT_OUTPUT_DIR})",
    )
    download.add_argument(
        "--frames",
        metavar="START-END",
        help="inclusive 1-based viewer frame range for image group URLs, e.g. 600-620",
    )
    download.add_argument(
        "--format",
        choices=("jpeg", "png"),
        default="jpeg",
        help="output image format (default: jpeg; PNG avoids further JPEG compression)",
    )
    download.add_argument(
        "--quality",
        type=bounded_integer(70, 100),
        default=95,
        help="JPEG quality, 70-100 (default: 95; ignored for PNG)",
    )
    download.add_argument(
        "--workers",
        type=bounded_integer(1, 16),
        default=8,
        help="parallel tile requests (default: 8)",
    )

    stitching = subparsers.add_parser("stitch", help="stitch local overlapping screenshots")
    stitching.add_argument("folder", type=Path, help="folder with 001.png, 002.png, etc.")
    stitching.add_argument("--out", type=Path, required=True, help="output PNG path")

    capture = subparsers.add_parser(
        "capture", help="pan and capture the visible document in an open desktop browser"
    )
    capture.add_argument("--frames", required=True, metavar="START-END")
    capture.add_argument("--region", required=True, metavar="X,Y,WIDTH,HEIGHT")
    capture.add_argument("--next-click", metavar="X,Y", help="screen coordinates of Next button")
    capture.add_argument("--next-key", help="keyboard key for Next, e.g. right")
    capture.add_argument("--zoom-click", metavar="X,Y", help="viewer zoom-in button coordinate")
    capture.add_argument("--zoom-count", type=int, default=0, help="zoom-in clicks before first frame")
    capture.add_argument(
        "--wheel-zoom", type=int, default=0,
        help="plain mouse-wheel UP zoom steps in the image viewport before the first frame",
    )
    capture.add_argument(
        "--pan-mode", choices=("drag", "wheel"), default="drag",
        help="drag the image (default) or use Shift+wheel / Alt+wheel",
    )
    capture.add_argument(
        "--wheel-ticks", type=int, default=160,
        help="wheel notches per pan step in wheel mode (default 160)",
    )
    capture.add_argument(
        "--invert-horizontal-wheel", action="store_true",
        help="reverse Shift+wheel direction if it pans the wrong way",
    )
    capture.add_argument(
        "--invert-vertical-wheel", action="store_true",
        help="reverse Alt+wheel direction if it pans the wrong way",
    )
    capture.add_argument(
        "--ctrl-wheel-zoom", type=int, default=0,
        help="Ctrl+mouse-wheel UP this many notches at viewport center before first frame",
    )
    capture.add_argument(
        "--clean-ui", action="store_true",
        help="toggle installed FamilySearch clean-view userscript during each capture",
    )
    capture.add_argument("--step", type=float, default=0.65, help="viewport pan step (default 0.65)")
    capture.add_argument("--max-cols", type=int, default=12)
    capture.add_argument("--max-rows", type=int, default=12)
    capture.add_argument("--start-delay", type=float, default=5.0)
    capture.add_argument(
        "--preview-only", action="store_true",
        help="save a screenshot of --region without moving or zooming; inspect before capture",
    )
    capture.add_argument("--next-delay", type=float, default=3.0)
    capture.add_argument("--allow-single", action="store_true")
    capture.add_argument("--url", default="", help="source URL for metadata only")
    capture.add_argument("--out", type=Path, required=True)
    return parser


def _point(value: str | None) -> tuple[int, int] | None:
    if value is None:
        return None
    try:
        parts = [int(part.strip()) for part in value.split(",")]
    except ValueError as error:
        raise DownloadError("Coordinates must be X,Y") from error
    if len(parts) != 2 or min(parts) < 0:
        raise DownloadError("Coordinates must be non-negative X,Y")
    return parts[0], parts[1]


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "stitch":
            try:
                from screenshot_stitch import EXTENSIONS, StitchError, stitch
            except ImportError as error:
                raise DownloadError(
                    f"Cannot import screenshot tools: {error}. "
                    "Update your installed project and install optional dependencies: "
                    "uv pip install -r requirements-screenshots.txt"
                ) from error
            paths = sorted(
                p for p in args.folder.iterdir()
                if p.is_file() and p.suffix.lower() in EXTENSIONS and p != args.out
            )
            try:
                result = stitch(paths, args.out)
            except (StitchError, ValueError) as error:
                raise DownloadError(str(error)) from error
            print(f"Saved {result['path']}: {result['size']} from {result['images']} screenshots")
            return 0
        elif args.command == "capture":
            try:
                from screenshot_capture import (
                    CaptureError,
                    Desktop,
                    Region,
                    Scanner,
                    WheelScanner,
                    capture_pages,
                )
            except ImportError as error:
                raise DownloadError(
                    f"Cannot import screenshot tools: {error}. "
                    "Update your installed project and install optional dependencies: "
                    "uv pip install -r requirements-screenshots.txt"
                ) from error
            if (
                args.max_cols < 1 or args.max_rows < 1
                or args.zoom_count < 0 or args.ctrl_wheel_zoom < 0
                or args.wheel_zoom < 0
            ):
                raise DownloadError(
                    "max-cols/max-rows must be >= 1; zoom counts must be >= 0"
                )
            if sum(
                bool(x) for x in (args.ctrl_wheel_zoom, args.wheel_zoom, args.zoom_count)
            ) > 1:
                raise DownloadError("Choose only one of the three zoom methods.")
            if not 1 <= args.wheel_ticks <= 400:
                raise DownloadError("--wheel-ticks must be between 1 and 400")
            if args.next_click and args.next_key:
                raise DownloadError("Specify either --next-click or --next-key")
            if args.zoom_count and not args.zoom_click:
                raise DownloadError("--zoom-count requires --zoom-click")
            try:
                start, end = parse_frame_range(args.frames)
                desktop = Desktop()
                region = Region.parse(args.region)
                scanner_settings = dict(
                    step=args.step, max_cols=args.max_cols, max_rows=args.max_rows
                )
                if args.pan_mode == "wheel":
                    scanner = WheelScanner(
                        desktop, region, wheel_ticks=args.wheel_ticks,
                        horizontal_sign=-1 if args.invert_horizontal_wheel else 1,
                        vertical_sign=-1 if args.invert_vertical_wheel else 1,
                        **scanner_settings,
                    )
                else:
                    scanner = Scanner(desktop, region, **scanner_settings)
                print(f"Open viewer at frame {start}. Starting in {args.start_delay}s...")
                desktop.sleep(args.start_delay)
                if args.preview_only:
                    args.out.mkdir(parents=True, exist_ok=True)
                    preview = args.out / "preview.png"
                    desktop.screenshot(region).save(preview, "PNG")
                    print(
                        f"Viewport preview saved to {preview}; "
                        "check that it contains the document, not Cursor chat."
                    )
                    return 0
                capture_pages(
                    scanner, start=start, end=end, out=args.out,
                    next_click=_point(args.next_click), next_key=args.next_key,
                    zoom_click=_point(args.zoom_click), zoom_count=args.zoom_count,
                    ctrl_wheel_zoom=args.ctrl_wheel_zoom,
                    wheel_zoom=args.wheel_zoom,
                    next_delay=args.next_delay, allow_single=args.allow_single,
                    source_url=args.url, clean_ui=args.clean_ui,
                )
            except (CaptureError, ValueError) as error:
                raise DownloadError(str(error)) from error
            return 0
        elif args.command == "login":
            ask_fssessionid()
            print("Saved. It will be reused until FamilySearch rejects it.")
        elif args.command == "logout":
            clear_fssessionid()
            print("Saved session removed.")
        else:
            return 1 if download_urls(args).failed else 0
    except (DownloadError, OSError, EOFError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted. Run the same command to resume.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
