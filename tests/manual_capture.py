"""在真实 Windows 桌面人工验收鼠标所在显示器的截图能力。"""

from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Sequence

from app.capture import CaptureError, grab_active_monitor, save_image

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def main(argv: Sequence[str] | None = None) -> int:
    """捕获鼠标所在显示器并在仓库根目录保存忽略提交的测试图。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--save-full-screen",
        action="store_true",
        help="明确同意把当前整屏截图保存为本地测试文件",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=3.0,
        help="捕获前等待秒数，便于把鼠标移到目标显示器（默认 3 秒）",
    )
    arguments = parser.parse_args(argv)
    if not arguments.save_full_screen:
        print("未执行捕获：整屏可能包含敏感信息。确认后请加入 --save-full-screen。")
        return 2
    if arguments.delay < 0:
        parser.error("--delay 不能小于零")

    print(f"将在 {arguments.delay:g} 秒后捕获鼠标所在整块显示器，请避开敏感内容。")
    time.sleep(arguments.delay)
    try:
        image, meta = grab_active_monitor()
        destination = save_image(image, PROJECT_ROOT / "test_capture.png")
    except CaptureError as exc:
        print(f"人工截图验收失败：{exc}")
        return 1

    print(f"截图已保存：{destination}")
    print(f"monitor_index={meta.monitor_index}")
    print(f"left={meta.left}, top={meta.top}")
    print(f"width={meta.width}, height={meta.height}")
    print(f"scale={meta.scale:.2f}")
    print(f"device_name={meta.device_name or 'unknown'}")
    print(f"app_name={meta.app_name or 'unknown'}")
    print(f"captured_at={meta.captured_at}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
