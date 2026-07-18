"""桌面端入口。

T0 只负责准备本地目录。全局快捷键、托盘和任务编排将在后续阶段实现。
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
    LOGGER.info("T0 初始化完成；桌面功能将在后续任务中实现。")


if __name__ == "__main__":
    main()
