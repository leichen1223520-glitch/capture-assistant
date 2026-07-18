"""在真实 Windows 桌面人工验收冻结画面、框选和像素裁剪。"""

from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Sequence

from app.capture import CaptureError, grab_active_monitor, save_image
from app.overlay import OverlayError, crop_selection, select_region

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def main(argv: Sequence[str] | None = None) -> int:
    """捕获画面、显示框选浮层，并保存忽略提交的裁剪测试图。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--delay",
        type=float,
        default=3.0,
        help="捕获前等待秒数，便于把鼠标移到目标显示器（默认 3 秒）",
    )
    arguments = parser.parse_args(argv)
    if arguments.delay < 0:
        parser.error("--delay 不能小于零")

    print(f"将在 {arguments.delay:g} 秒后冻结鼠标所在显示器。")
    time.sleep(arguments.delay)
    try:
        image, meta = grab_active_monitor()
        rect = select_region(image, capture_meta=meta)
        if rect is None:
            print("用户已取消框选；未创建截图文件。")
            return 0
        cropped = crop_selection(image, rect)
        destination = save_image(cropped, PROJECT_ROOT / "test_crop.png")
    except (CaptureError, OverlayError) as exc:
        print(f"人工框选验收失败：{exc}")
        return 1

    print(f"裁剪图已保存：{destination}")
    print(f"rect=({rect.x()}, {rect.y()}, {rect.width()}, {rect.height()})")
    print(f"source_size=({meta.width}, {meta.height}), scale={meta.scale:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
