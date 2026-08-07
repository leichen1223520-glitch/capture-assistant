"""应用集中配置与本地数据目录初始化。"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(
    os.environ.get("CAPTURE_ASSISTANT_DATA_DIR", PROJECT_ROOT / "data")
).expanduser()
DB_PATH = DATA_DIR / "capture_assistant.sqlite3"
SCREENSHOT_DIR = DATA_DIR / "screenshots"

HOTKEY = os.environ.get("CAPTURE_ASSISTANT_HOTKEY", "ctrl+shift+s")
WS_PORT = int(os.environ.get("CAPTURE_ASSISTANT_WS_PORT", "8765"))
API_PORT = int(os.environ.get("CAPTURE_ASSISTANT_API_PORT", "8000"))
OBSIDIAN_RECONCILE_INTERVAL_MS = 30_000


def ensure_data_dirs(
    data_dir: Path = DATA_DIR,
    screenshot_dir: Path = SCREENSHOT_DIR,
) -> tuple[Path, Path]:
    """创建运行所需目录并返回规范化路径。

    参数可被测试覆盖，避免测试向真实用户数据目录写入文件。
    """

    resolved_data_dir = data_dir.expanduser().resolve()
    resolved_screenshot_dir = screenshot_dir.expanduser().resolve()
    resolved_data_dir.mkdir(parents=True, exist_ok=True)
    resolved_screenshot_dir.mkdir(parents=True, exist_ok=True)
    return resolved_data_dir, resolved_screenshot_dir
