"""应用配置与本地数据目录初始化的单元测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app import config


class ConfigTests(unittest.TestCase):
    """验证集中配置和数据目录初始化行为。"""

    def test_runtime_configuration_types_and_derived_paths(self) -> None:
        """配置可由环境变量覆盖，但类型和路径派生关系必须稳定。"""

        self.assertIsInstance(config.HOTKEY, str)
        self.assertTrue(config.HOTKEY.strip())
        self.assertIsInstance(config.WS_PORT, int)
        self.assertIsInstance(config.API_PORT, int)
        self.assertGreaterEqual(config.WS_PORT, 1)
        self.assertLessEqual(config.WS_PORT, 65535)
        self.assertGreaterEqual(config.API_PORT, 1)
        self.assertLessEqual(config.API_PORT, 65535)
        self.assertEqual(config.OBSIDIAN_RECONCILE_INTERVAL_MS, 30_000)
        self.assertIsInstance(config.DATA_DIR, Path)
        self.assertIsInstance(config.DB_PATH, Path)
        self.assertIsInstance(config.SCREENSHOT_DIR, Path)
        self.assertEqual(config.DB_PATH, config.DATA_DIR / "capture_assistant.sqlite3")
        self.assertEqual(config.SCREENSHOT_DIR, config.DATA_DIR / "screenshots")

    def test_ensure_data_dirs_supports_isolated_paths(self) -> None:
        """测试和其他运行环境应能把数据目录定向到指定位置。"""

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            data_dir = temp_path / "custom-data"
            screenshot_dir = data_dir / "custom-screenshots"

            resolved_data_dir, resolved_screenshot_dir = config.ensure_data_dirs(
                data_dir=data_dir,
                screenshot_dir=screenshot_dir,
            )

            self.assertEqual(resolved_data_dir, data_dir.resolve())
            self.assertEqual(resolved_screenshot_dir, screenshot_dir.resolve())
            self.assertTrue(resolved_data_dir.is_dir())
            self.assertTrue(resolved_screenshot_dir.is_dir())

    def test_ensure_data_dirs_is_idempotent(self) -> None:
        """应用重复启动时，已存在的数据目录不应导致初始化失败。"""

        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            screenshot_dir = data_dir / "screenshots"

            first_result = config.ensure_data_dirs(data_dir, screenshot_dir)
            second_result = config.ensure_data_dirs(data_dir, screenshot_dir)

            self.assertEqual(second_result, first_result)


if __name__ == "__main__":
    unittest.main()
