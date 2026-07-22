"""桌面端入口。

当前只负责准备本地目录。全局快捷键、托盘和可见操作编排将在 T7 实现。
"""

from __future__ import annotations

import logging

from .config import DB_PATH, ensure_data_dirs

LOGGER = logging.getLogger(__name__)


def main() -> None:
    """初始化本地目录并启动当前阶段的应用入口。"""

    data_dir, screenshot_dir = ensure_data_dirs()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    LOGGER.info("数据目录已就绪：%s", data_dir)
    LOGGER.info("截图目录已就绪：%s", screenshot_dir)
    LOGGER.info("数据库路径：%s", DB_PATH)
    LOGGER.info("T0–T6 基础模块已就绪；用户可见的快捷键与确认流程将在 T7 串联。")


if __name__ == "__main__":
    main()
