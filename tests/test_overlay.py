"""冻结画面框选模块的坐标和裁剪单元测试。"""

from __future__ import annotations

import unittest

from PIL import Image
from PySide6.QtCore import QRect, QSize

from app.overlay import (
    OverlayError,
    crop_selection,
    map_widget_rect_to_image,
    normalize_display_name,
    pil_image_to_qimage,
)


class OverlayTests(unittest.TestCase):
    """验证高 DPI 映射、越界裁剪和 PIL/Qt 图像转换。"""

    def test_maps_logical_selection_to_physical_pixels(self) -> None:
        mapped = map_widget_rect_to_image(
            QRect(100, 50, 400, 200),
            QSize(1920, 1080),
            QSize(3840, 2160),
        )

        self.assertEqual(mapped, QRect(200, 100, 800, 400))

    def test_mapping_rounds_outward_to_keep_edge_pixels(self) -> None:
        mapped = map_widget_rect_to_image(
            QRect(1, 1, 1, 1),
            QSize(1000, 1000),
            QSize(1500, 1500),
        )

        self.assertEqual(mapped, QRect(1, 1, 2, 2))

    def test_mapping_clips_drag_outside_overlay(self) -> None:
        mapped = map_widget_rect_to_image(
            QRect(-100, -50, 300, 200),
            QSize(1000, 500),
            QSize(2000, 1000),
        )

        self.assertEqual(mapped, QRect(0, 0, 400, 300))

    def test_mapping_rejects_zero_sized_surfaces(self) -> None:
        with self.assertRaises(OverlayError):
            map_widget_rect_to_image(QRect(0, 0, 1, 1), QSize(), QSize(10, 10))

    def test_crop_selection_uses_image_pixel_coordinates(self) -> None:
        image = Image.new("RGB", (10, 8), color="white")
        image.putpixel((3, 2), (10, 20, 30))

        cropped = crop_selection(image, QRect(3, 2, 4, 3))

        self.assertEqual(cropped.size, (4, 3))
        self.assertEqual(cropped.getpixel((0, 0)), (10, 20, 30))

    def test_crop_selection_clips_to_image(self) -> None:
        image = Image.new("RGB", (10, 8), color="white")

        cropped = crop_selection(image, QRect(8, 6, 10, 10))

        self.assertEqual(cropped.size, (2, 2))

    def test_crop_selection_rejects_empty_or_external_rect(self) -> None:
        image = Image.new("RGB", (10, 8), color="white")
        for rect in (QRect(), QRect(20, 20, 2, 2)):
            with self.subTest(rect=rect):
                with self.assertRaises(OverlayError):
                    crop_selection(image, rect)

    def test_pil_image_conversion_copies_pixels(self) -> None:
        image = Image.new("RGBA", (2, 1), color=(11, 22, 33, 44))

        converted = pil_image_to_qimage(image)
        image.putpixel((0, 0), (200, 200, 200, 200))

        color = converted.pixelColor(0, 0)
        self.assertEqual(
            (color.red(), color.green(), color.blue(), color.alpha()),
            (11, 22, 33, 44),
        )

    def test_normalizes_windows_and_qt_display_names(self) -> None:
        self.assertEqual(normalize_display_name(r"\\.\DISPLAY2"), "display2")
        self.assertEqual(normalize_display_name("DISPLAY2"), "display2")


if __name__ == "__main__":
    unittest.main()
