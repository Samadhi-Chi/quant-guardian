from __future__ import annotations

import ctypes
from types import SimpleNamespace
from unittest.mock import Mock, patch

from quant_guardian.recovery import launch_preflight


def test_launcher_manifest_is_read_as_data_without_running_it():
    manifest = b"<requestedExecutionLevel level='requireAdministrator' uiAccess='false'/>"
    buffer = ctypes.create_string_buffer(manifest)
    kernel = SimpleNamespace(
        LoadLibraryExW=Mock(return_value=9), FindResourceW=Mock(return_value=2),
        SizeofResource=Mock(return_value=len(manifest)), LoadResource=Mock(return_value=3),
        LockResource=Mock(return_value=ctypes.addressof(buffer)), FreeLibrary=Mock(),
    )
    launch_preflight._manifest_level.cache_clear()
    with patch.object(ctypes, "WinDLL", return_value=kernel):
        assert launch_preflight._manifest_level("fake.exe", 1, 100) == "requireAdministrator"
    kernel.LoadLibraryExW.assert_called_once_with("fake.exe", None, 0x22)
    kernel.FreeLibrary.assert_called_once_with(9)


def test_launcher_permission_blocks_without_triggering_uac(tmp_path):
    executable = tmp_path / "qmt.exe"
    executable.write_bytes(b"placeholder")
    with patch.object(launch_preflight, "is_elevated", return_value=False), patch.object(launch_preflight, "_manifest_level", return_value="requireAdministrator"):
        assert "管理员" in launch_preflight.launch_permission_problem(str(executable))
    with patch.object(launch_preflight, "is_elevated", return_value=True), patch.object(launch_preflight, "_manifest_level") as manifest:
        assert launch_preflight.launch_permission_problem(str(executable)) == ""
        manifest.assert_not_called()


def test_missing_or_normal_launcher_has_no_elevation_requirement(tmp_path):
    executable = tmp_path / "qmt.exe"
    with patch.object(launch_preflight, "is_elevated", return_value=False):
        assert launch_preflight.launch_permission_problem(str(executable)) == ""
        executable.write_bytes(b"placeholder")
        with patch.object(launch_preflight, "_manifest_level", return_value="asInvoker"):
            assert launch_preflight.launch_permission_problem(str(executable)) == ""
