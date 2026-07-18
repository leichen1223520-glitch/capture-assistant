"""在 Qt offscreen 平台验证框选窗口的事件与线程边界。"""

from __future__ import annotations

import os
import unittest

# 必须在创建 QApplication 前设置，避免自动测试访问用户的真实桌面。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402
from PySide6.QtCore import QPoint, QThread, QTimer, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import app.overlay as overlay_module  # noqa: E402
from app.capture import CaptureMeta  # noqa: E402
from app.overlay import (  # noqa: E402
    OverlayError,
    SelectionOverlay,
    screen_for_capture,
    select_region,
)


class _WrongThreadSelection(QThread):
    """从非 GUI 线程尝试创建浮层并保留可断言的异常。"""

    def __init__(self) -> None:
        super().__init__()
        self.error: Exception | None = None

    def run(self) -> None:
        try:
            select_region(Image.new("RGB", (20, 10), "white"))
        except Exception as exc:  # 测试线程需要把异常传回主线程断言
            self.error = exc


class OverlayQtTests(unittest.TestCase):
    """验证 Enter/Esc/右键、屏幕匹配、线程和重入保护。"""

    application: QApplication

    @classmethod
    def setUpClass(cls) -> None:
        instance = QApplication.instance()
        cls.application = instance if isinstance(instance, QApplication) else QApplication([])

    def _new_overlay(self) -> SelectionOverlay:
        screen = self.application.primaryScreen()
        self.assertIsNotNone(screen)
        assert screen is not None
        return SelectionOverlay(Image.new("RGB", (320, 180), "white"), screen)

    def test_drag_and_enter_returns_image_pixel_selection(self) -> None:
        overlay = self._new_overlay()

        def interact() -> None:
            QTest.mousePress(
                overlay,
                Qt.MouseButton.LeftButton,
                pos=QPoint(10, 10),
            )
            QTest.mouseMove(overlay, QPoint(110, 60))
            QTest.mouseRelease(
                overlay,
                Qt.MouseButton.LeftButton,
                pos=QPoint(110, 60),
            )
            QTest.keyClick(overlay, Qt.Key.Key_Return)

        QTimer.singleShot(20, interact)
        result = overlay.exec_selection()

        self.assertIsNotNone(result)
        assert result is not None
        self.assertGreater(result.width(), 0)
        self.assertGreater(result.height(), 0)

    def test_escape_cancels_selection(self) -> None:
        overlay = self._new_overlay()
        QTimer.singleShot(20, lambda: QTest.keyClick(overlay, Qt.Key.Key_Escape))

        self.assertIsNone(overlay.exec_selection())

    def test_right_click_cancels_selection(self) -> None:
        overlay = self._new_overlay()
        QTimer.singleShot(
            20,
            lambda: QTest.mouseClick(overlay, Qt.MouseButton.RightButton),
        )

        self.assertIsNone(overlay.exec_selection())

    def test_resolves_capture_screen_without_reading_cursor_again(self) -> None:
        screen = self.application.primaryScreen()
        self.assertIsNotNone(screen)
        assert screen is not None
        ratio = screen.devicePixelRatio()
        meta = CaptureMeta(
            monitor_index=1,
            left=0,
            top=0,
            width=round(screen.size().width() * ratio),
            height=round(screen.size().height() * ratio),
            scale=ratio,
            device_name=screen.name() or None,
            app_name=None,
            captured_at="2026-07-18T22:00:00+08:00",
        )

        self.assertIs(screen_for_capture(meta), screen)

    def test_rejects_non_gui_thread(self) -> None:
        worker = _WrongThreadSelection()
        worker.start()

        self.assertTrue(worker.wait(3000))
        self.assertIsInstance(worker.error, OverlayError)
        self.assertIn("GUI 主线程", str(worker.error))

    def test_rejects_reentrant_selection(self) -> None:
        previous_state = overlay_module._SELECTION_ACTIVE
        overlay_module._SELECTION_ACTIVE = True
        try:
            with self.assertRaisesRegex(OverlayError, "已有框选会话"):
                select_region(Image.new("RGB", (20, 10), "white"))
        finally:
            overlay_module._SELECTION_ACTIVE = previous_state


if __name__ == "__main__":
    unittest.main()
