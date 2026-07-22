"""人工验证 Chrome 扩展与本机 WebSocket 桥。"""

from __future__ import annotations

from dataclasses import asdict
import json
import time

from app.bridge import BrowserBridge

FOCUS_DELAY_SECONDS = 5


def main() -> int:
    bridge = BrowserBridge()
    bridge.start()
    print("桥已启动：ws://127.0.0.1:8765")
    print("请加载 extension/ 扩展，并在普通 http/https 页面准备好测试内容。")
    try:
        while True:
            input("按回车开始；随后请在 5 秒内切回目标 Chrome 窗口：")
            print("5 秒后读取。请现在切回 Chrome，并保持目标标签页在前台……")
            time.sleep(FOCUS_DELAY_SECONDS)
            context = bridge.get_browser_context(timeout=0.3)
            if context is None:
                print(
                    "未取得浏览器上下文（扩展未连接、Chrome 未在前台、"
                    "页面受限或请求超时）。"
                )
            else:
                print(json.dumps(asdict(context), ensure_ascii=False, indent=2))
    except (KeyboardInterrupt, EOFError):
        print("\n正在关闭桥……")
    finally:
        bridge.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
