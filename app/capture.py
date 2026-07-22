"""Windows 屏幕抓帧适配器。

本模块只在调用抓帧函数时访问 Windows API 和 ``mss``。导入模块不会捕获
屏幕、创建文件或修改系统设置。
"""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Mapping, Sequence

from PIL import Image


class CaptureError(RuntimeError):
    """表示当前环境无法完成屏幕捕获或截图保存。"""


@dataclass(frozen=True, slots=True)
class CaptureMeta:
    """一次屏幕捕获所需的可追溯环境元数据。

    ``left`` 和 ``top`` 是所选显示器在 Windows 虚拟桌面中的物理像素坐标；
    ``width`` 和 ``height`` 与返回图像的像素尺寸一致。
    """

    monitor_index: int
    left: int
    top: int
    width: int
    height: int
    scale: float
    device_name: str | None
    app_name: str | None
    captured_at: str

    def card_monitor(self) -> dict[str, int | float]:
        """返回符合 ``Card.monitor`` 数据契约的最小显示器信息。"""

        return {
            "width": self.width,
            "height": self.height,
            "scale": self.scale,
        }


@dataclass(frozen=True, slots=True)
class ForegroundWindowSnapshot:
    """某一时刻的前台窗口身份与几何信息。

    快照只包含窗口句柄、进程文件名和屏幕矩形，不读取窗口标题或窗口正文。
    受保护进程、窗口销毁等竞态会让可选字段诚实地保留为 ``None``。
    """

    handle: int
    app_name: str | None
    bounds: tuple[int, int, int, int] | None


class _Point(ctypes.Structure):
    """Windows ``POINT`` 的本地声明，避免在非 Windows 导入时调用 API。"""

    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


def _require_windows() -> None:
    if sys.platform != "win32":
        raise CaptureError("屏幕捕获当前只支持 Windows 10/11。")


def enable_per_monitor_dpi_awareness() -> bool:
    """尽早请求 Per-Monitor-V2 DPI 感知。

    返回是否成功设置。若进程已经由 Qt 或清单设置了 DPI 模式，Windows 可能
    拒绝重复修改，此时返回 ``False``，但不会覆盖现有模式。
    """

    if sys.platform != "win32":
        return False

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    setter = getattr(user32, "SetProcessDpiAwarenessContext", None)
    if setter is not None:
        setter.argtypes = [wintypes.HANDLE]
        setter.restype = wintypes.BOOL
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 == -4
        if setter(ctypes.c_void_p(-4)):
            return True

    legacy_setter = getattr(user32, "SetProcessDPIAware", None)
    if legacy_setter is None:
        return False
    legacy_setter.argtypes = []
    legacy_setter.restype = wintypes.BOOL
    return bool(legacy_setter())


def _cursor_position() -> tuple[int, int]:
    _require_windows()
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetCursorPos.argtypes = [ctypes.POINTER(_Point)]
    user32.GetCursorPos.restype = wintypes.BOOL

    point = _Point()
    if not user32.GetCursorPos(ctypes.byref(point)):
        error_code = ctypes.get_last_error()
        raise CaptureError(f"无法读取鼠标位置（Windows 错误 {error_code}）。")
    return int(point.x), int(point.y)


def monitor_index_for_point(
    monitors: Sequence[Mapping[str, int]],
    x: int,
    y: int,
) -> int:
    """返回包含指定物理像素坐标的 ``mss`` 显示器索引。

    ``mss.monitors[0]`` 是整个虚拟桌面，因此会从索引 1 开始匹配。若坐标
    恰好落在显示器布局的缝隙中，选择欧氏距离最近的显示器。
    """

    if len(monitors) < 2:
        raise CaptureError("mss 未返回任何可捕获的物理显示器。")

    for index, monitor in enumerate(monitors[1:], start=1):
        left = int(monitor["left"])
        top = int(monitor["top"])
        right = left + int(monitor["width"])
        bottom = top + int(monitor["height"])
        if left <= x < right and top <= y < bottom:
            return index

    def squared_distance(item: tuple[int, Mapping[str, int]]) -> int:
        _, monitor = item
        left = int(monitor["left"])
        top = int(monitor["top"])
        right = left + int(monitor["width"])
        bottom = top + int(monitor["height"])
        dx = left - x if x < left else x - right if x >= right else 0
        dy = top - y if y < top else y - bottom if y >= bottom else 0
        return dx * dx + dy * dy

    candidates = list(enumerate(monitors[1:], start=1))
    return min(candidates, key=squared_distance)[0]


def _monitor_scale(x: int, y: int) -> float:
    """读取坐标所在显示器的有效 DPI，失败时诚实降级为 1.0。"""

    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        user32.MonitorFromPoint.argtypes = [_Point, wintypes.DWORD]
        user32.MonitorFromPoint.restype = wintypes.HANDLE
        monitor_handle = user32.MonitorFromPoint(_Point(x, y), 2)
        if not monitor_handle:
            return 1.0

        dpi_x = wintypes.UINT()
        dpi_y = wintypes.UINT()
        shcore.GetDpiForMonitor.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.POINTER(wintypes.UINT),
            ctypes.POINTER(wintypes.UINT),
        ]
        shcore.GetDpiForMonitor.restype = ctypes.c_long
        result = shcore.GetDpiForMonitor(
            monitor_handle,
            0,
            ctypes.byref(dpi_x),
            ctypes.byref(dpi_y),
        )
        if result == 0 and dpi_x.value > 0:
            return float(dpi_x.value) / 96.0
    except (AttributeError, OSError):
        return 1.0
    return 1.0


def _monitor_device_name(x: int, y: int) -> str | None:
    r"""返回坐标所在显示器的 Windows 设备名，例如 ``\\.\DISPLAY1``。"""

    class MonitorInfoEx(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", wintypes.RECT),
            ("rcWork", wintypes.RECT),
            ("dwFlags", wintypes.DWORD),
            ("szDevice", wintypes.WCHAR * 32),
        ]

    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.MonitorFromPoint.argtypes = [_Point, wintypes.DWORD]
        user32.MonitorFromPoint.restype = wintypes.HANDLE
        monitor_handle = user32.MonitorFromPoint(_Point(x, y), 2)
        if not monitor_handle:
            return None

        info = MonitorInfoEx()
        info.cbSize = ctypes.sizeof(MonitorInfoEx)
        user32.GetMonitorInfoW.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(MonitorInfoEx),
        ]
        user32.GetMonitorInfoW.restype = wintypes.BOOL
        if not user32.GetMonitorInfoW(monitor_handle, ctypes.byref(info)):
            return None
        return str(info.szDevice) or None
    except (AttributeError, OSError):
        return None


def _process_name_for_window(
    window: int,
    user32: object,
    kernel32: object,
) -> str | None:
    """尽力读取指定 HWND 所属的进程文件名。"""

    process_id = wintypes.DWORD()
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowThreadProcessId(window, ctypes.byref(process_id))
    if process_id.value == 0:
        return None

    process_query_limited_information = 0x1000
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    process = kernel32.OpenProcess(
        process_query_limited_information,
        False,
        process_id.value,
    )
    if not process:
        return None

    try:
        buffer = ctypes.create_unicode_buffer(32768)
        length = wintypes.DWORD(len(buffer))
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        if not kernel32.QueryFullProcessImageNameW(
            process,
            0,
            buffer,
            ctypes.byref(length),
        ):
            return None
        return os.path.basename(buffer.value) or None
    finally:
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle(process)


def _bounds_for_window(
    window: int,
    user32: object,
) -> tuple[int, int, int, int] | None:
    """尽力读取指定 HWND 在虚拟桌面中的矩形。"""

    bounds = wintypes.RECT()
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    if not user32.GetWindowRect(window, ctypes.byref(bounds)):
        return None
    return (
        int(bounds.left),
        int(bounds.top),
        int(bounds.right),
        int(bounds.bottom),
    )


def foreground_window_snapshot() -> ForegroundWindowSnapshot | None:
    """一次读取前台 HWND，并尽力补充进程名和窗口矩形。

    无 Windows 桌面或无法取得前台 HWND 时返回 ``None``；取得句柄后，进程
    查询与矩形查询彼此独立降级，避免一个受限字段丢掉另一个可用字段。
    """

    if sys.platform != "win32":
        return None

    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = wintypes.HWND
        window = user32.GetForegroundWindow()
    except (AttributeError, OSError):
        return None
    if not window:
        return None

    handle = int(window)
    try:
        bounds = _bounds_for_window(handle, user32)
    except (AttributeError, OSError):
        bounds = None

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        app_name = _process_name_for_window(handle, user32, kernel32)
    except (AttributeError, OSError):
        app_name = None

    return ForegroundWindowSnapshot(
        handle=handle,
        app_name=app_name,
        bounds=bounds,
    )


def foreground_window_intersects_capture(
    snapshot: ForegroundWindowSnapshot | None,
    capture_meta: CaptureMeta,
) -> bool:
    """纯逻辑判断前台窗口矩形是否与捕获显示器有正面积交集。"""

    if snapshot is None or snapshot.bounds is None:
        return False

    window_left, window_top, window_right, window_bottom = snapshot.bounds
    capture_right = capture_meta.left + capture_meta.width
    capture_bottom = capture_meta.top + capture_meta.height
    return (
        max(window_left, capture_meta.left) < min(window_right, capture_right)
        and max(window_top, capture_meta.top) < min(window_bottom, capture_bottom)
    )


def foreground_app_name() -> str | None:
    """兼容入口：复用同一次前台窗口快照并返回进程名。"""

    snapshot = foreground_window_snapshot()
    return snapshot.app_name if snapshot is not None else None


def grab_active_monitor() -> tuple[Image.Image, CaptureMeta]:
    """捕获鼠标所在显示器并返回 RGB 图像和环境元数据。

    在非 Windows、无交互桌面、没有显示器或系统拒绝捕获时抛出
    :class:`CaptureError`，不会自动改为全桌面或上传到其他服务。
    """

    _require_windows()
    enable_per_monitor_dpi_awareness()
    cursor_x, cursor_y = _cursor_position()

    try:
        from mss import mss
        from mss.exception import ScreenShotError
    except ImportError as exc:
        raise CaptureError("缺少 mss，无法执行本地屏幕捕获。") from exc

    try:
        with mss() as capture:
            index = monitor_index_for_point(capture.monitors, cursor_x, cursor_y)
            monitor = capture.monitors[index]
            shot = capture.grab(monitor)
            image = Image.frombytes("RGB", shot.size, shot.rgb)
    except (ScreenShotError, OSError) as exc:
        raise CaptureError(f"屏幕捕获失败：{exc}") from exc

    width, height = image.size
    meta = CaptureMeta(
        monitor_index=index,
        left=int(monitor["left"]),
        top=int(monitor["top"]),
        width=width,
        height=height,
        scale=_monitor_scale(cursor_x, cursor_y),
        device_name=_monitor_device_name(cursor_x, cursor_y),
        app_name=foreground_app_name(),
        captured_at=datetime.now().astimezone().isoformat(),
    )
    return image, meta


def save_image(image: Image.Image, path: str | Path) -> Path:
    """以 PNG 保存图像，创建必要的父目录并返回规范化路径。"""

    destination = Path(path).expanduser().resolve()
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        image.save(destination, format="PNG")
    except OSError as exc:
        raise CaptureError(f"无法保存 PNG 截图：{destination}") from exc
    return destination
