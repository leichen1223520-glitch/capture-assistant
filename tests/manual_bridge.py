"""人工验证 Chrome 扩展与本机 WebSocket 桥。"""

from __future__ import annotations

from dataclasses import asdict
import json

from app.bridge import BrowserBridge


def main() -> int:
    bridge = BrowserBridge()
    bridge.start()
    print("桥已启动：ws://127.0.0.1:8765")
    print("请加载 extension/ 扩展，在普通 http/https 页面选中文字后按回车。")
    try:
        while True:
            input("按回车读取当前活动标签；按 Ctrl+C 退出：")
            context = bridge.get_browser_context(timeout=0.3)
            if context is None:
                print("未取得浏览器上下文（扩展未连接、页面受限或请求超时）。")
            else:
                print(json.dumps(asdict(context), ensure_ascii=False, indent=2))
    except (KeyboardInterrupt, EOFError):
        print("\n正在关闭桥……")
    finally:
        bridge.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
