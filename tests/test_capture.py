"""屏幕捕获模块中可脱离真实桌面验证的单元测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from app.capture import CaptureError, CaptureMeta, monitor_index_for_point, save_image


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


if __name__ == "__main__":
    unittest.main()
