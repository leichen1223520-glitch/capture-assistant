"""PySide6 冻结画面与主动框选浮层。

浮层只显示调用方传入的内存图像。用户按 Enter 确认后返回原图物理像素坐标，
按 Esc、右键或关闭窗口时返回 ``None``，不会自行保存任何画面。
"""

from __future__ import annotations

import math
import sys
from typing import Final

from PIL import Image
from PySide6.QtCore import QEventLoop, QPoint, QRect, QSize, QThread, Qt
from PySide6.QtGui import (
    QColor,
    QCloseEvent,
    QCursor,
    QGuiApplication,
    QImage,
    QKeyEvent,
    QMouseEvent,
    QPaintEvent,
    QPainter,
    QPen,
    QPixmap,
    QScreen,
)
from PySide6.QtWidgets import QApplication, QWidget

from .capture import CaptureMeta, enable_per_monitor_dpi_awareness

MIN_SELECTION_SIZE: Final = 3
_SELECTION_ACTIVE = False


class OverlayError(RuntimeError):
    """表示当前环境无法显示或处理框选浮层。"""


def pil_image_to_qimage(image: Image.Image) -> QImage:
    """将 PIL 图像复制为与源缓冲区生命周期无关的 Qt 图像。"""

    rgba = image.convert("RGBA")
    width, height = rgba.size
    if width <= 0 or height <= 0:
        raise OverlayError("冻结画面的尺寸必须大于零。")
    bytes_per_line = width * 4
    qt_image = QImage(
        rgba.tobytes("raw", "RGBA"),
        width,
        height,
        bytes_per_line,
        QImage.Format.Format_RGBA8888,
    )
    return qt_image.copy()


def map_widget_rect_to_image(
    rect: QRect,
    widget_size: QSize,
    image_size: QSize,
) -> QRect:
    """把浮层逻辑像素选区映射为原图物理像素矩形。

    左上边界向下取整、右下边界向上取整，避免在 DPI 缩放时漏掉用户看见的
    边缘像素。越过浮层边界的拖动会被安全裁剪。
    """

    widget_width = widget_size.width()
    widget_height = widget_size.height()
    image_width = image_size.width()
    image_height = image_size.height()
    if min(widget_width, widget_height, image_width, image_height) <= 0:
        raise OverlayError("浮层和原图尺寸必须大于零。")

    bounds = QRect(0, 0, widget_width, widget_height)
    clipped = rect.normalized().intersected(bounds)
    if clipped.isEmpty():
        return QRect()

    scale_x = image_width / widget_width
    scale_y = image_height / widget_height
    left = max(0, math.floor(clipped.x() * scale_x))
    top = max(0, math.floor(clipped.y() * scale_y))
    right = min(image_width, math.ceil((clipped.x() + clipped.width()) * scale_x))
    bottom = min(image_height, math.ceil((clipped.y() + clipped.height()) * scale_y))
    return QRect(left, top, max(0, right - left), max(0, bottom - top))


def crop_selection(image: Image.Image, rect: QRect) -> Image.Image:
    """按原图像素矩形裁剪并返回独立图像副本。"""

    image_bounds = QRect(0, 0, image.width, image.height)
    clipped = rect.normalized().intersected(image_bounds)
    if clipped.isEmpty():
        raise OverlayError("不能裁剪空选区或原图范围之外的选区。")
    return image.crop(
        (
            clipped.x(),
            clipped.y(),
            clipped.x() + clipped.width(),
            clipped.y() + clipped.height(),
        )
    ).copy()


def normalize_display_name(name: str) -> str:
    """规范化 Windows 与 Qt 可能使用的两种显示器设备名格式。"""

    normalized = name.strip().casefold().replace("/", "\\")
    if normalized.startswith("\\\\.\\"):
        normalized = normalized[4:]
    return normalized


def screen_for_capture(meta: CaptureMeta) -> QScreen | None:
    """根据捕获时保存的设备身份解析同一块 Qt 显示器。

    优先匹配 Windows 设备名；设备名不可用时再以物理像素尺寸和捕获索引
    降级。调用方不得把重新读取鼠标位置作为首选，因为鼠标可能已经跨屏。
    """

    screens = QGuiApplication.screens()
    if meta.device_name:
        expected_name = normalize_display_name(meta.device_name)
        for candidate in screens:
            if normalize_display_name(candidate.name()) == expected_name:
                return candidate

    size_matches = [
        candidate
        for candidate in screens
        if round(candidate.size().width() * candidate.devicePixelRatio()) == meta.width
        and round(candidate.size().height() * candidate.devicePixelRatio()) == meta.height
    ]
    if len(size_matches) == 1:
        return size_matches[0]

    screen_index = meta.monitor_index - 1
    if 0 <= screen_index < len(screens):
        return screens[screen_index]
    return None


class SelectionOverlay(QWidget):
    """覆盖单个显示器并收集一次矩形选区的无边框窗口。"""

    def __init__(self, frozen_image: Image.Image, screen: QScreen) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Window,
        )
        self._image_size = QSize(frozen_image.width, frozen_image.height)
        self._pixmap = QPixmap.fromImage(pil_image_to_qimage(frozen_image))
        self._screen = screen
        self._anchor: QPoint | None = None
        self._selection = QRect()
        self._accepted = False
        self._finished = False
        self._event_loop: QEventLoop | None = None

        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.CrossCursor)

        # 强制创建原生窗口后再指定目标屏幕，可避免负坐标多屏布局跑到主屏。
        self.winId()
        window = self.windowHandle()
        if window is not None:
            window.setScreen(screen)
        self.setGeometry(screen.geometry())

    @property
    def selection(self) -> QRect:
        """返回当前浮层逻辑像素选区的副本。"""

        return QRect(self._selection)

    def exec_selection(self) -> QRect | None:
        """显示浮层并等待确认，返回原图像素选区或 ``None``。"""

        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus(Qt.FocusReason.ActiveWindowFocusReason)

        self._event_loop = QEventLoop(self)
        self._event_loop.exec()
        self.hide()

        if not self._accepted:
            return None
        mapped = map_widget_rect_to_image(self._selection, self.size(), self._image_size)
        if mapped.width() < 1 or mapped.height() < 1:
            return None
        return mapped

    def _finish(self, *, accepted: bool) -> None:
        if self._finished:
            return
        if accepted and (
            self._selection.width() < MIN_SELECTION_SIZE
            or self._selection.height() < MIN_SELECTION_SIZE
        ):
            return

        self._accepted = accepted
        self._finished = True
        if self._event_loop is not None and self._event_loop.isRunning():
            self._event_loop.quit()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.RightButton:
            self._finish(accepted=False)
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._anchor = event.position().toPoint()
            self._selection = QRect(self._anchor, self._anchor)
            self.update()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._anchor is not None:
            self._selection = QRect(self._anchor, event.position().toPoint()).normalized()
            self.update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._anchor is not None:
            self._selection = QRect(self._anchor, event.position().toPoint()).normalized()
            self._anchor = None
            self.update()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._finish(accepted=True)
            event.accept()
            return
        if event.key() == Qt.Key.Key_Escape:
            self._finish(accepted=False)
            event.accept()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        self._finish(accepted=False)
        event.accept()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.drawPixmap(self.rect(), self._pixmap)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 145))

        if not self._selection.isEmpty():
            visible_selection = self._selection.intersected(self.rect())
            if not visible_selection.isEmpty():
                painter.save()
                painter.setClipRect(visible_selection)
                painter.drawPixmap(self.rect(), self._pixmap)
                painter.restore()
                painter.setPen(QPen(QColor(46, 204, 113), 2))
                painter.drawRect(visible_selection.adjusted(0, 0, -1, -1))

                size_text = f"{visible_selection.width()} × {visible_selection.height()}"
                self._draw_badge(
                    painter,
                    size_text,
                    visible_selection.topLeft() + QPoint(0, -34),
                    QColor(24, 24, 24, 220),
                )

        self._draw_badge(
            painter,
            "● 捕获中",
            QPoint(20, 20),
            QColor(165, 45, 45, 225),
        )
        instruction = "拖动框选内容  ·  Enter 确认  ·  Esc / 右键取消"
        metrics = painter.fontMetrics()
        instruction_width = metrics.horizontalAdvance(instruction) + 32
        self._draw_badge(
            painter,
            instruction,
            QPoint(max(20, (self.width() - instruction_width) // 2), 20),
            QColor(24, 24, 24, 220),
        )
        painter.end()

    @staticmethod
    def _draw_badge(
        painter: QPainter,
        text: str,
        top_left: QPoint,
        background: QColor,
    ) -> None:
        metrics = painter.fontMetrics()
        width = metrics.horizontalAdvance(text) + 24
        height = metrics.height() + 14
        x = max(8, top_left.x())
        y = max(8, top_left.y())
        badge = QRect(x, y, width, height)
        painter.fillRect(badge, background)
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(
            badge.adjusted(12, 0, -12, 0),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            text,
        )


def select_region(
    frozen_image: Image.Image,
    *,
    capture_meta: CaptureMeta | None = None,
    screen: QScreen | None = None,
) -> QRect | None:
    """在捕获来源显示器上显示冻结画面并返回原图像素选区。

    调用必须发生在 GUI 主线程。若调用方尚未创建 ``QApplication``，本函数会
    创建最小实例。通过 :func:`grab_active_monitor` 获得图像时应同时传入
    ``capture_meta``，防止鼠标跨屏后画面与浮层错配。无可用显示器时抛出
    :class:`OverlayError`。
    """

    if frozen_image.width <= 0 or frozen_image.height <= 0:
        raise OverlayError("冻结画面的尺寸必须大于零。")

    application = QApplication.instance()
    if application is None:
        enable_per_monitor_dpi_awareness()
        QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )
        application = QApplication([sys.argv[0]])

    if not isinstance(application, QApplication):
        raise OverlayError("当前 Qt 事件循环不是 QApplication，无法创建框选窗口。")
    if QThread.currentThread() != application.thread():
        raise OverlayError("框选浮层必须由 Qt GUI 主线程创建。")

    target_screen = screen
    if target_screen is None and capture_meta is not None:
        target_screen = screen_for_capture(capture_meta)
    if target_screen is None:
        target_screen = QGuiApplication.screenAt(QCursor.pos())
    if target_screen is None:
        target_screen = QGuiApplication.primaryScreen()
    if target_screen is None:
        raise OverlayError("没有可用的图形显示器，无法显示框选浮层。")

    global _SELECTION_ACTIVE
    if _SELECTION_ACTIVE:
        raise OverlayError("已有框选会话正在进行，请先完成或取消当前框选。")
    _SELECTION_ACTIVE = True
    try:
        overlay = SelectionOverlay(frozen_image, target_screen)
        return overlay.exec_selection()
    finally:
        _SELECTION_ACTIVE = False
