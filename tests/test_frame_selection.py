"""Offline tests for frame selection and PNG output."""

import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from PIL import Image

import cli
from downloader import (
    DownloadError,
    ImageGroup,
    download_document,
    is_complete_image,
    is_group_url,
    validate_source_url,
)

DETAILS_URL = (
    "https://www.familysearch.org/records/images/image-details?"
    "imageGroupNumbers=7805075&page=1&rmsId=M9MV-Y8B"
)
VIEWER_URL = (
    "https://www.familysearch.org/ark:/61903/3:1:3Q9M-CS92-WSGQ-M?"
    "view=explore&groupId=M9MV-Y8B&lang=ru"
)


def fake_group(count: int = 622) -> ImageGroup:
    return ImageGroup(
        [
            f"https://www.familysearch.org/ark:/61903/3:1:TEST-{index}?i={index}"
            for index in range(count)
        ],
        "CDIAC-127-1015-114_7805075",
        "Boguslav 1796",
        "CDIAC 127/1015/114",
        "7805075",
        "M9MV-Y8B",
        VIEWER_URL,
    )


class FrameSelectionTests(unittest.TestCase):
    def test_both_provided_links_are_recognized_as_groups(self):
        for url in [DETAILS_URL, VIEWER_URL]:
            with self.subTest(url=url):
                validate_source_url(url)
                self.assertTrue(is_group_url(url))

    def test_inclusive_600_620(self):
        urls = cli.select_group_frames(fake_group(), (600, 620))
        self.assertEqual(len(urls), 21)
        self.assertTrue(urls[0].endswith("?i=599"))
        self.assertTrue(urls[-1].endswith("?i=619"))

    def test_rejects_invalid_and_missing_frames(self):
        for value in ["0-2", "3-2", "600", "abc", "1-2-3"]:
            with self.subTest(value=value), self.assertRaises(DownloadError):
                cli.parse_frame_range(value)
        with self.assertRaises(DownloadError):
            cli.select_group_frames(fake_group(619), (600, 620))
        group = fake_group(621)
        group.image_urls.pop(601)  # Viewer frame 602 missing; later frames still exist.
        with self.assertRaises(DownloadError):
            cli.select_group_frames(group, (600, 620))

    def test_single_page_with_frames_fails_before_authentication(self):
        with patch("cli.get_session") as session:
            args = cli.build_parser().parse_args(
                [
                    "download",
                    "https://www.familysearch.org/ark:/61903/3:1:TEST-1",
                    "--frames",
                    "1-2",
                ]
            )
            with self.assertRaises(DownloadError):
                cli.download_urls(args)
            session.assert_not_called()

    @patch("cli.time.sleep")
    @patch("cli.download_document")
    @patch("cli.fetch_image_group", return_value=fake_group())
    @patch("cli.get_session", return_value="test-session")
    def test_cli_downloads_only_selected_frames_in_png(
        self, session, fetch_group, download, sleep
    ):
        download.return_value = Mock(path=Path("page.png"), width=10, height=10)
        with TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            args = cli.build_parser().parse_args(
                ["download", VIEWER_URL, "--frames", "600-620", "--format", "png", "-o", directory]
            )
            result = cli.download_urls(args)
            self.assertEqual(result.saved, 21)
            self.assertEqual(download.call_count, 21)
            requested = [call.args[0] for call in download.call_args_list]
            self.assertTrue(requested[0].endswith("?i=599"))
            self.assertTrue(requested[-1].endswith("?i=619"))
            self.assertTrue(
                all(call.kwargs["image_format"] == "png" for call in download.call_args_list)
            )
            info = Path(directory) / fake_group().folder_name / "info.txt"
            self.assertIn("Selected frames: 600-620", info.read_text(encoding="utf-8"))

    def test_complete_png_is_skipped_but_corrupted_png_is_rejected(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "page.png"
            Image.new("RGB", (20, 20), "red").save(path)
            self.assertTrue(is_complete_image(path))
            path.write_bytes(path.read_bytes()[:30])
            self.assertFalse(is_complete_image(path))


class PngAssemblyTests(unittest.TestCase):
    def test_png_preserves_decoded_tile_pixels(self):
        jpeg = io.BytesIO()
        Image.new("RGB", (64, 40), (80, 160, 220)).save(jpeg, "JPEG", quality=81)
        tile = jpeg.getvalue()
        client = Mock()
        client.probe.side_effect = lambda level, col, row: (
            (tile, (64, 40)) if level <= 9 and col == 0 and row == 0 else None
        )
        client.get_tile.return_value = tile
        url = "https://www.familysearch.org/ark:/61903/3:1:TEST-1?i=599"
        with TemporaryDirectory() as directory, patch("downloader.TileClient", return_value=client):
            result = download_document(url, Path(directory), "test-session", image_format="png")
            self.assertEqual(result.path.name, "0600_familysearch_TEST-1.png")
            self.assertTrue(is_complete_image(result.path))
            with Image.open(result.path) as image, Image.open(io.BytesIO(tile)) as original:
                self.assertEqual(image.format, "PNG")
                self.assertEqual(image.size, original.size)
                self.assertEqual(image.getpixel((3, 3)), original.getpixel((3, 3)))
        client.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
