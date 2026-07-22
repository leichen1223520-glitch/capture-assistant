"""Windows 全局快捷键的最小权限封装。

本模块只通过 ``RegisterHotKey`` 注册一个明确的组合键，不安装键盘钩子，
也不会观察或记录其他按键。导入模块不会注册快捷键或创建 Qt 对象。
"""

from __future__ import annotations

import ctypes
import re
import sys
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any, Protocol

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, QThread, Signal
from PySide6.QtWidgets import QApplication

from .config import HOTKEY

WM_HOTKEY = 0x0312

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

VK_F1 = 0x70
VK_F24 = 0x87

# RegisterHotKey 文档为应用程序保留 0x0000--0xBFFF。该编号只在当前线程使用。
DEFAULT_HOTKEY_ID = 0x4CA1

_MODIFIER_VALUES = {
    "ctrl": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
}
_MODIFIER_ORDER = ("ctrl", "alt", "shift", "win")
_WINDOWS_EVENT_TYPES = {
    b"windows_dispatcher_MSG",
    b"windows_generic_MSG",
    b"generic_MSG",
}
_FUNCTION_KEY_PATTERN = re.compile(r"f([1-9]|1[0-9]|2[0-4])", re.IGNORECASE)


class HotkeyError(RuntimeError):
    """快捷键格式、运行环境或系统注册失败。"""


@dataclass(frozen=True, slots=True)
class ParsedHotkey:
    """已经规范化、可直接传给 ``RegisterHotKey`` 的快捷键。"""

    modifiers: int
    virtual_key: int
    canonical: str


class _User32(Protocol):
    """本模块使用的最小 user32 接口，便于无副作用地单元测试。"""

    def RegisterHotKey(
        self,
        window: object,
        hotkey_id: int,
        modifiers: int,
        virtual_key: int,
    ) -> int: ...

    def UnregisterHotKey(self, window: object, hotkey_id: int) -> int: ...


def parse_hotkey(value: str) -> ParsedHotkey:
    """解析严格的全局快捷键字符串。

    支持 ``ctrl``、``alt``、``shift``、``win`` 修饰键，以及单个英文字母、
    数字或 F1--F24。必须恰好包含一个普通键和至少一个修饰键；空片段、
    重复键及别名会被拒绝，避免用户误以为注册了另一个组合键。
    """

    if not isinstance(value, str) or not value.strip():
        raise HotkeyError("快捷键不能为空。")

    raw_parts = value.split("+")
    parts = [part.strip().lower() for part in raw_parts]
    if any(not part for part in parts):
        raise HotkeyError("快捷键格式无效：加号两侧都必须有按键名称。")

    seen_modifiers: set[str] = set()
    key_name: str | None = None
    virtual_key: int | None = None

    for part in parts:
        if part in _MODIFIER_VALUES:
            if part in seen_modifiers:
                raise HotkeyError(f"快捷键包含重复的修饰键：{part}。")
            seen_modifiers.add(part)
            continue

        parsed_virtual_key = _parse_virtual_key(part)
        if parsed_virtual_key is None:
            raise HotkeyError(
                f"不支持的按键名称：{part}。仅支持单字母、数字和 F1-F24。"
            )
        if virtual_key is not None:
            raise HotkeyError("快捷键只能包含一个普通键。")
        virtual_key = parsed_virtual_key
        key_name = part.upper()

    if not seen_modifiers:
        raise HotkeyError("全局快捷键必须至少包含一个修饰键。")
    if virtual_key is None or key_name is None:
        raise HotkeyError("快捷键缺少字母、数字或 F1-F24 普通键。")

    modifiers = 0
    canonical_parts: list[str] = []
    for modifier_name in _MODIFIER_ORDER:
        if modifier_name in seen_modifiers:
            modifiers |= _MODIFIER_VALUES[modifier_name]
            canonical_parts.append(modifier_name)
    canonical_parts.append(key_name.lower())

    return ParsedHotkey(
        modifiers=modifiers,
        virtual_key=virtual_key,
        canonical="+".join(canonical_parts),
    )


def _parse_virtual_key(value: str) -> int | None:
    """把受支持的普通键名称转换为 Windows 虚拟键码。"""

    if len(value) == 1 and ("a" <= value <= "z" or "0" <= value <= "9"):
        return ord(value.upper())

    match = _FUNCTION_KEY_PATTERN.fullmatch(value)
    if match is None:
        return None
    return VK_F1 + int(match.group(1)) - 1


def _load_user32() -> _User32:
    """加载并声明本模块所需的两个 user32 函数。"""

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.RegisterHotKey.argtypes = [
        wintypes.HWND,
        ctypes.c_int,
        wintypes.UINT,
        wintypes.UINT,
    ]
    user32.RegisterHotKey.restype = wintypes.BOOL
    user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.UnregisterHotKey.restype = wintypes.BOOL
    return user32


def _get_application() -> QApplication | None:
    """返回现有 QApplication；本模块不会隐式创建应用实例。"""

    instance = QApplication.instance()
    return instance if isinstance(instance, QApplication) else None


def _event_type_bytes(event_type: Any) -> bytes:
    """兼容 QByteArray、bytes 和测试使用的字符串事件类型。"""

    if isinstance(event_type, bytes):
        return event_type.rstrip(b"\x00")
    if isinstance(event_type, str):
        return event_type.encode("ascii", errors="ignore").rstrip(b"\x00")
    try:
        return bytes(event_type).rstrip(b"\x00")
    except (TypeError, ValueError):
        return b""


def _message_address(message: Any) -> int:
    """取得 Qt VoidPtr 或 ctypes 测试指针的地址。"""

    if isinstance(message, int):
        return message
    try:
        return int(message)
    except (TypeError, ValueError):
        try:
            return ctypes.addressof(message.contents)
        except (AttributeError, TypeError, ValueError):
            return 0


class _NativeHotkeyFilter(QAbstractNativeEventFilter):
    """只转发属于当前注册编号的 ``WM_HOTKEY`` 消息。"""

    def __init__(self, owner: "HotkeyManager") -> None:
        super().__init__()
        self._owner = owner

    def nativeEventFilter(self, event_type: Any, message: Any) -> tuple[bool, int]:
        if _event_type_bytes(event_type) not in _WINDOWS_EVENT_TYPES:
            return False, 0

        address = _message_address(message)
        if not address:
            return False, 0

        native_message = ctypes.cast(
            address,
            ctypes.POINTER(wintypes.MSG),
        ).contents
        if (
            native_message.message == WM_HOTKEY
            and int(native_message.wParam) == self._owner.hotkey_id
        ):
            self._owner._emit_activated()
        return False, 0


class HotkeyManager(QObject):
    """注册并管理一个 Windows 全局快捷键。

    ``start`` 与 ``stop`` 都是幂等操作，且必须在已有 QApplication 的 GUI
    主线程调用。注册冲突、非 Windows 环境和 QApplication 缺失都会抛出
    ``HotkeyError``，不会静默降级为键盘监听。
    """

    activated = Signal()

    def __init__(
        self,
        hotkey: str = HOTKEY,
        parent: QObject | None = None,
        *,
        hotkey_id: int = DEFAULT_HOTKEY_ID,
        _user32: _User32 | None = None,
        _platform_name: str | None = None,
    ) -> None:
        super().__init__(parent)
        if not 0 <= hotkey_id <= 0xBFFF:
            raise HotkeyError("快捷键编号必须位于 Windows 应用程序保留范围内。")

        self._parsed = parse_hotkey(hotkey)
        self._hotkey_id = hotkey_id
        self._user32_override = _user32
        self._platform_name = sys.platform if _platform_name is None else _platform_name
        self._user32: _User32 | None = None
        self._application: QApplication | None = None
        self._native_filter = _NativeHotkeyFilter(self)
        self._started = False

    @property
    def hotkey(self) -> str:
        """返回规范化后的快捷键字符串。"""

        return self._parsed.canonical

    @property
    def hotkey_id(self) -> int:
        """返回传给 Windows 消息系统的注册编号。"""

        return self._hotkey_id

    @property
    def is_started(self) -> bool:
        """快捷键当前是否已成功注册。"""

        return self._started

    def start(self) -> None:
        """安装 Qt 原生事件过滤器并注册全局快捷键。"""

        if self._started:
            return
        if self._platform_name != "win32":
            raise HotkeyError("全局快捷键目前只支持 Windows 10/11。")

        application = _get_application()
        if application is None:
            raise HotkeyError("启动全局快捷键前必须先创建 QApplication。")
        if QThread.currentThread() is not application.thread():
            raise HotkeyError("全局快捷键必须在 QApplication 的 GUI 主线程启动。")

        user32 = self._user32_override or _load_user32()
        application.installNativeEventFilter(self._native_filter)
        modifiers = self._parsed.modifiers | MOD_NOREPEAT
        registered = bool(
            user32.RegisterHotKey(
                None,
                self._hotkey_id,
                modifiers,
                self._parsed.virtual_key,
            )
        )
        if not registered:
            application.removeNativeEventFilter(self._native_filter)
            error_getter = getattr(ctypes, "get_last_error", None)
            error_code = int(error_getter()) if error_getter is not None else 0
            detail = f"（Windows 错误 {error_code}）" if error_code else ""
            raise HotkeyError(
                f"无法注册 {self.hotkey}：快捷键可能已被其他程序占用或被系统拒绝{detail}。"
            )

        self._user32 = user32
        self._application = application
        self._started = True

    def stop(self) -> None:
        """注销快捷键并移除 Qt 原生事件过滤器。"""

        if not self._started:
            return
        assert self._user32 is not None
        assert self._application is not None

        unregistered = bool(self._user32.UnregisterHotKey(None, self._hotkey_id))
        if not unregistered:
            raise HotkeyError(
                f"无法注销 {self.hotkey}；快捷键仍保持注册，请重试或退出应用。"
            )

        self._application.removeNativeEventFilter(self._native_filter)
        self._started = False
        self._application = None
        self._user32 = None

    def _emit_activated(self) -> None:
        """由原生事件过滤器在 GUI 线程发出业务信号。"""

        if self._started:
            self.activated.emit()

