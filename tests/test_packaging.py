"""Ensure the installed CLI can locate its optional screenshot modules.

The subprocess runs outside the repository to catch py-modules packaging
omissions that are masked when Python runs from the source directory.
"""

import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

MODULES = ("cli", "downloader", "screenshot_capture", "screenshot_stitch")


class InstalledModuleTests(unittest.TestCase):
    def test_modules_are_available_outside_checkout(self):
        source = (
            "import importlib.util; "
            f"names = {MODULES!r}; "
            "missing = [name for name in names if importlib.util.find_spec(name) is None]; "
            "assert not missing, f'Not installed: {missing}'"
        )
        with TemporaryDirectory() as directory:
            completed = subprocess.run(
                [sys.executable, "-I", "-c", source],
                cwd=Path(directory),
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(
            completed.returncode,
            0,
            f"Modules are missing from the installed project:\n"
            f"{completed.stdout}{completed.stderr}",
        )


if __name__ == "__main__":
    unittest.main()
