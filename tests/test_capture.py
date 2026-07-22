"""屏幕捕获模块中可脱离真实桌面验证的单元测试。"""

from __future__ import annotations

import ctypes
import tempfile
import unittest
from ctypes import wintypes
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image

from app.capture import (
    CaptureError,
    CaptureMeta,
    ForegroundWindowSnapshot,
    foreground_app_name,
    foreground_window_intersects_capture,
    foreground_window_snapshot,
    monitor_index_for_point,
    save_image,
)


class CaptureTests(unittest.TestCase):
    """验证多屏选择、元数据契约和 PNG 保存。"""

    def setUp(self) -> None:
        self.monitors = [
            {"left": -1280, "top": 0, "width": 3200, "height": 1080},
            {"left": 0, "top": 0, "width": 1920, "height": 1080},
            {"left": -1280, "top": 100, "width": 1280, "height": 1024},
        ]

    def test_selects_monitor_containing_point(self) -> None:
        self.assertEqual(monitor_index_for_point(self.monitors, 100, 100), 1)
        self.assertEqual(monitor_index_for_point(self.monitors, -100, 200), 2)

    def test_uses_half_open_monitor_boundaries(self) -> None:
        self.assertEqual(monitor_index_for_point(self.monitors, 0, 100), 1)
        self.assertEqual(monitor_index_for_point(self.monitors, -1, 100), 2)

    def test_selects_nearest_monitor_for_layout_gap(self) -> None:
        monitors = [
            {"left": 0, "top": 0, "width": 2500, "height": 1200},
            {"left": 0, "top": 0, "width": 1000, "height": 1000},
            {"left": 1500, "top": 0, "width": 1000, "height": 1000},
        ]

        self.assertEqual(monitor_index_for_point(monitors, 1100, 500), 1)
        self.assertEqual(monitor_index_for_point(monitors, 1400, 500), 2)

    def test_rejects_missing_physical_monitors(self) -> None:
        with self.assertRaises(CaptureError):
            monitor_index_for_point(
                [{"left": 0, "top": 0, "width": 0, "height": 0}],
                0,
                0,
            )

    def test_capture_meta_exposes_only_card_monitor_contract(self) -> None:
        meta = CaptureMeta(
            monitor_index=2,
            left=-1280,
            top=100,
            width=1280,
            height=1024,
            scale=1.25,
            device_name=r"\\.\DISPLAY2",
            app_name="chrome.exe",
            captured_at="2026-07-18T22:00:00+08:00",
        )

        self.assertEqual(
            meta.card_monitor(),
            {"width": 1280, "height": 1024, "scale": 1.25},
        )

    def test_save_image_writes_png_and_creates_parent(self) -> None:
        image = Image.new("RGB", (12, 8), color=(21, 34, 55))
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "nested" / "capture.bin"

            result = save_image(image, destination)

            self.assertEqual(result, destination.resolve())
            self.assertTrue(destination.is_file())
            with Image.open(destination) as saved:
                self.assertEqual(saved.format, "PNG")
                self.assertEqual(saved.size, image.size)

    def test_public_foreground_app_wrapper_has_no_capture_side_effect(self) -> None:
        with patch("app.capture.ctypes.WinDLL", side_effect=OSError("测试失败")):
            self.assertIsNone(foreground_app_name())

    def test_reads_one_foreground_handle_and_independent_snapshot_fields(self) -> None:
        user32 = MagicMock()
        kernel32 = MagicMock()
        user32.GetForegroundWindow.return_value = 321

        def set_process_id(_window: int, process_id_pointer: object) -> int:
            process_id = ctypes.cast(
                process_id_pointer,
                ctypes.POINTER(wintypes.DWORD),
            )
            process_id.contents.value = 4321
            return 10

        def set_bounds(_window: int, bounds_pointer: object) -> int:
            bounds = ctypes.cast(
                bounds_pointer,
                ctypes.POINTER(wintypes.RECT),
            )
            bounds.contents.left = -1200
            bounds.contents.top = 100
            bounds.contents.right = 80
            bounds.contents.bottom = 900
            return 1

        def set_process_path(
            _process: int,
            _flags: int,
            buffer: object,
            _length_pointer: object,
        ) -> int:
            buffer.value = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
            return 1

        user32.GetWindowThreadProcessId.side_effect = set_process_id
        user32.GetWindowRect.side_effect = set_bounds
        kernel32.OpenProcess.return_value = 765
        kernel32.QueryFullProcessImageNameW.side_effect = set_process_path
        kernel32.CloseHandle.return_value = 1

        def load_library(name: str, *, use_last_error: bool) -> object:
            self.assertTrue(use_last_error)
            return user32 if name == "user32" else kernel32

        with patch("app.capture.ctypes.WinDLL", side_effect=load_library):
            snapshot = foreground_window_snapshot()

        self.assertEqual(
            snapshot,
            ForegroundWindowSnapshot(
                handle=321,
                app_name="chrome.exe",
                bounds=(-1200, 100, 80, 900),
            ),
        )
        user32.GetForegroundWindow.assert_called_once_with()
        kernel32.CloseHandle.assert_called_once_with(765)

    def test_snapshot_fields_degrade_independently(self) -> None:
        user32 = MagicMock()
        kernel32 = MagicMock()
        user32.GetForegroundWindow.return_value = 123
        user32.GetWindowRect.return_value = 0
        user32.GetWindowThreadProcessId.side_effect = OSError("拒绝进程查询")

        with patch(
            "app.capture.ctypes.WinDLL",
            side_effect=lambda name, **_kwargs: user32 if name == "user32" else kernel32,
        ):
            snapshot = foreground_window_snapshot()

        self.assertEqual(
            snapshot,
            ForegroundWindowSnapshot(handle=123, app_name=None, bounds=None),
        )
        user32.GetForegroundWindow.assert_called_once_with()

    def test_missing_foreground_window_returns_none_without_process_query(self) -> None:
        user32 = MagicMock()
        user32.GetForegroundWindow.return_value = 0

        with patch("app.capture.ctypes.WinDLL", return_value=user32) as loader:
            self.assertIsNone(foreground_window_snapshot())

        loader.assert_called_once_with("user32", use_last_error=True)
        user32.GetWindowThreadProcessId.assert_not_called()
        user32.GetWindowRect.assert_not_called()

    def test_foreground_app_name_reuses_snapshot(self) -> None:
        snapshot = ForegroundWindowSnapshot(
            handle=99,
            app_name="chrome.exe",
            bounds=(0, 0, 100, 100),
        )
        with patch("app.capture.foreground_window_snapshot", return_value=snapshot) as reader:
            self.assertEqual(foreground_app_name(), "chrome.exe")
        reader.assert_called_once_with()

    def test_window_and_capture_monitor_must_have_positive_area_overlap(self) -> None:
        meta = CaptureMeta(
            monitor_index=1,
            left=0,
            top=0,
            width=1920,
            height=1080,
            scale=1.0,
            device_name=r"\\.\DISPLAY1",
            app_name="chrome.exe",
            captured_at="2026-07-22T12:00:00+08:00",
        )

        crossing = ForegroundWindowSnapshot(
            handle=1,
            app_name="chrome.exe",
            bounds=(-100, 100, 200, 800),
        )
        touching_edge = ForegroundWindowSnapshot(
            handle=2,
            app_name="chrome.exe",
            bounds=(-500, 100, 0, 800),
        )
        other_monitor = ForegroundWindowSnapshot(
            handle=3,
            app_name="chrome.exe",
            bounds=(-1200, 100, -200, 800),
        )

        self.assertTrue(foreground_window_intersects_capture(crossing, meta))
        self.assertFalse(foreground_window_intersects_capture(touching_edge, meta))
        self.assertFalse(foreground_window_intersects_capture(other_monitor, meta))
        self.assertFalse(
            foreground_window_intersects_capture(
                ForegroundWindowSnapshot(handle=4, app_name=None, bounds=None),
                meta,
            )
        )
        self.assertFalse(foreground_window_intersects_capture(None, meta))


if __name__ == "__main__":
    unittest.main()
