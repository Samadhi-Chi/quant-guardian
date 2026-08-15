from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from quant_guardian.probe.readonly_xtquant import configure_xtquant_import


class XtQuantPathTests(unittest.TestCase):
    def test_parent_directory_named_xtquant_is_not_confused_with_package(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "XtQuant"
            package = parent / "xtquant"
            package.mkdir(parents=True)
            with patch(
                "quant_guardian.probe.readonly_xtquant.os.add_dll_directory",
                return_value=object(),
            ):
                configure_xtquant_import(str(parent))
            self.assertEqual(sys.path[0], str(parent))
            sys.path.remove(str(parent))

    def test_direct_package_path_adds_its_parent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "xtquant"
            package.mkdir()
            with patch(
                "quant_guardian.probe.readonly_xtquant.os.add_dll_directory",
                return_value=object(),
            ):
                configure_xtquant_import(str(package))
            self.assertEqual(sys.path[0], str(package.parent))
            sys.path.remove(str(package.parent))


if __name__ == "__main__":
    unittest.main()
