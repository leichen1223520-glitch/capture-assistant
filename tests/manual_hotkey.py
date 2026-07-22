"""在真实 Windows 桌面人工验证全局快捷键。"""

from __future__ import annotations

import signal
import sys
from datetime import datetime

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from app.config import HOTKEY
from app.hotkey import HotkeyError, HotkeyManager


def main() -> int:
    """注册配置中的快捷键并持续显示触发次数，直到用户终止。"""

    application = QApplication(sys.argv)
    manager = HotkeyManager(HOTKEY)
    trigger_count = 0

    def on_activated() -> None:
        nonlocal trigger_count
        trigger_count += 1
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        print(f"快捷键已触发 {trigger_count} 次（{now}）", flush=True)

    manager.activated.connect(on_activated)
    signal.signal(signal.SIGINT, lambda *_args: application.quit())
    # 定时回到 Python 解释器，使控制台 Ctrl+C 能被及时处理。
    signal_timer = QTimer()
    signal_timer.timeout.connect(lambda: None)
    signal_timer.start(250)

    try:
        manager.start()
    except HotkeyError as exc:
        print(f"无法启动人工测试：{exc}", file=sys.stderr)
        return 1

    print(f"已注册全局快捷键：{manager.hotkey}", flush=True)
    print("请切换到其他应用后按快捷键；每次触发都会在这里计数。", flush=True)
    print("完成后回到此窗口按 Ctrl+C 退出。", flush=True)

    exit_code = application.exec()
    try:
        manager.stop()
    except HotkeyError as exc:
        print(f"退出时注销快捷键失败：{exc}", file=sys.stderr)
        return 1
    print(f"测试结束，共触发 {trigger_count} 次。", flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

