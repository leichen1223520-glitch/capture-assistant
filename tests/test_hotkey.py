"""测试 Windows 全局快捷键的解析、注册和消息过滤。"""

from __future__ import annotations

import ctypes
import os
import unittest
from ctypes import wintypes
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.hotkey import (  # noqa: E402
    MOD_ALT,
    MOD_CONTROL,
    MOD_NOREPEAT,
    MOD_SHIFT,
    MOD_WIN,
    VK_F1,
    HotkeyError,
    HotkeyManager,
    WM_HOTKEY,
    parse_hotkey,
)


class _FakeUser32:
    """记录注册调用并允许测试模拟 Windows 返回值。"""

    def __init__(self, *, register_result: bool = True, unregister_result: bool = True) -> None:
        self.register_result = register_result
        self.unregister_result = unregister_result
        self.register_calls: list[tuple[object, int, int, int]] = []
        self.unregister_calls: list[tuple[object, int]] = []

    def RegisterHotKey(
        self,
        window: object,
        hotkey_id: int,
        modifiers: int,
        virtual_key: int,
    ) -> int:
        self.register_calls.append((window, hotkey_id, modifiers, virtual_key))
        return int(self.register_result)

    def UnregisterHotKey(self, window: object, hotkey_id: int) -> int:
        self.unregister_calls.append((window, hotkey_id))
        return int(self.unregister_result)


class HotkeyParserTests(unittest.TestCase):
    """验证严格且与操作系统无关的字符串解析。"""

    def test_parses_and_canonicalizes_modifiers(self) -> None:
        parsed = parse_hotkey(" SHIFT + Ctrl + S ")

        self.assertEqual(parsed.modifiers, MOD_CONTROL | MOD_SHIFT)
        self.assertEqual(parsed.virtual_key, ord("S"))
        self.assertEqual(parsed.canonical, "ctrl+shift+s")

    def test_supports_all_modifier_types_and_key_ranges(self) -> None:
        parsed = parse_hotkey("win+alt+ctrl+shift+F24")

        self.assertEqual(
            parsed.modifiers,
            MOD_CONTROL | MOD_ALT | MOD_SHIFT | MOD_WIN,
        )
        self.assertEqual(parsed.virtual_key, VK_F1 + 23)
        self.assertEqual(parsed.canonical, "ctrl+alt+shift+win+f24")
        self.assertEqual(parse_hotkey("ctrl+0").virtual_key, ord("0"))
        self.assertEqual(parse_hotkey("alt+f1").virtual_key, VK_F1)

    def test_rejects_ambiguous_or_unsafe_forms(self) -> None:
        invalid_values = (
            "",
            "s",
            "ctrl",
            "ctrl+ctrl+s",
            "ctrl+s+t",
            "ctrl++s",
            "control+s",
            "ctrl+space",
            "ctrl+f0",
            "ctrl+f25",
            "ctrl+你",
        )

        for value in invalid_values:
            with self.subTest(value=value), self.assertRaises(HotkeyError):
                parse_hotkey(value)


class HotkeyManagerTests(unittest.TestCase):
    """用伪造 user32 验证生命周期，避免测试抢占真实快捷键。"""

    application: QApplication

    @classmethod
    def setUpClass(cls) -> None:
        instance = QApplication.instance()
        cls.application = instance if isinstance(instance, QApplication) else QApplication([])

    def test_construction_has_no_registration_side_effect(self) -> None:
        user32 = _FakeUser32()

        manager = HotkeyManager(_user32=user32, _platform_name="win32")

        self.assertFalse(manager.is_started)
        self.assertEqual(user32.register_calls, [])

    def test_start_and_stop_are_idempotent_and_use_no_repeat(self) -> None:
        user32 = _FakeUser32()
        manager = HotkeyManager(
            "ctrl+shift+s",
            _user32=user32,
            _platform_name="win32",
        )

        manager.start()
        manager.start()
        self.assertTrue(manager.is_started)
        self.assertEqual(
            user32.register_calls,
            [(None, manager.hotkey_id, MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT, ord("S"))],
        )

        manager.stop()
        manager.stop()
        self.assertFalse(manager.is_started)
        self.assertEqual(user32.unregister_calls, [(None, manager.hotkey_id)])

    def test_reports_non_windows_and_missing_application(self) -> None:
        manager = HotkeyManager(_user32=_FakeUser32(), _platform_name="linux")
        with self.assertRaisesRegex(HotkeyError, "只支持 Windows"):
            manager.start()

        manager = HotkeyManager(_user32=_FakeUser32(), _platform_name="win32")
        with patch("app.hotkey._get_application", return_value=None):
            with self.assertRaisesRegex(HotkeyError, "QApplication"):
                manager.start()

    def test_reports_registration_conflict_and_remains_stopped(self) -> None:
        user32 = _FakeUser32(register_result=False)
        manager = HotkeyManager(_user32=user32, _platform_name="win32")

        with self.assertRaisesRegex(HotkeyError, "占用"):
            manager.start()

        self.assertFalse(manager.is_started)
        self.assertEqual(len(user32.register_calls), 1)

    def test_emits_only_matching_hotkey_for_supported_qt_event_types(self) -> None:
        user32 = _FakeUser32()
        manager = HotkeyManager(_user32=user32, _platform_name="win32")
        triggered: list[int] = []
        manager.activated.connect(lambda: triggered.append(1))
        manager.start()
        try:
            for event_type in (
                b"windows_dispatcher_MSG",
                b"windows_generic_MSG",
                b"generic_MSG",
            ):
                message = wintypes.MSG()
                message.message = WM_HOTKEY
                message.wParam = manager.hotkey_id
                handled, result = manager._native_filter.nativeEventFilter(
                    event_type,
                    ctypes.addressof(message),
                )
                self.assertFalse(handled)
                self.assertEqual(result, 0)

            wrong_id = wintypes.MSG()
            wrong_id.message = WM_HOTKEY
            wrong_id.wParam = manager.hotkey_id + 1
            manager._native_filter.nativeEventFilter(
                b"windows_dispatcher_MSG",
                ctypes.addressof(wrong_id),
            )

            wrong_message = wintypes.MSG()
            wrong_message.message = 0x0100
            wrong_message.wParam = manager.hotkey_id
            manager._native_filter.nativeEventFilter(
                b"windows_dispatcher_MSG",
                ctypes.addressof(wrong_message),
            )
            manager._native_filter.nativeEventFilter(
                b"unrelated_event",
                ctypes.addressof(message),
            )
        finally:
            manager.stop()

        self.assertEqual(len(triggered), 3)

    def test_failed_unregister_keeps_manager_active_for_retry(self) -> None:
        user32 = _FakeUser32(unregister_result=False)
        manager = HotkeyManager(_user32=user32, _platform_name="win32")
        manager.start()

        with self.assertRaisesRegex(HotkeyError, "仍保持注册"):
            manager.stop()

        self.assertTrue(manager.is_started)
        user32.unregister_result = True
        manager.stop()
        self.assertFalse(manager.is_started)


if __name__ == "__main__":
    unittest.main()

