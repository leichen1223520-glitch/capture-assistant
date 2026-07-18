"""观点卡片数据模型的单元测试。"""

from __future__ import annotations

import unittest
from datetime import datetime
from uuid import UUID

from pydantic import ValidationError

from app.models import Card


def _minimal_card_data() -> dict[str, object]:
    """返回创建观点卡片所需的最小输入。"""

    return {
        "text": "技术应当服务于人的判断。",
        "text_source": "ocr",
        "confidence": 0.93,
        "screenshot_path": "screenshots/card.png",
        "full_screenshot_path": "screenshots/full_card.png",
    }


class CardTests(unittest.TestCase):
    """验证 Card 的默认策略、合法输入和边界约束。"""

    def test_defaults_are_safe_and_traceable(self) -> None:
        """新卡片应有唯一标识、带时区时间和不推断立场的默认值。"""

        card = Card(**_minimal_card_data())

        card_id = UUID(card.id)
        created_at = datetime.fromisoformat(card.created_at.replace("Z", "+00:00"))

        self.assertEqual(card_id.version, 4)
        self.assertIsNotNone(created_at.tzinfo)
        self.assertIsNotNone(created_at.utcoffset())
        self.assertEqual(card.stance, "unknown")
        self.assertEqual(card.note, "")
        self.assertIsNone(card.source_url)
        self.assertIsNone(card.source_title)
        self.assertIsNone(card.video_time)
        self.assertIsNone(card.app_name)
        self.assertIsNone(card.monitor)

    def test_accepts_all_supported_values(self) -> None:
        """完整且合法的证据字段应被原样保留。"""

        card = Card(
            **_minimal_card_data(),
            id="b7ee7f06-a0a6-4f27-9fd6-7e42c2fb939f",
            source_url="https://example.com/video",
            source_title="示例视频",
            video_time=42.5,
            app_name="chrome.exe",
            monitor={"width": 1920, "height": 1080, "scale": 1.25},
            created_at="2026-07-18T22:00:00+08:00",
            stance="agree",
            note="这条观点值得继续验证。",
        )

        self.assertEqual(card.text_source, "ocr")
        self.assertAlmostEqual(card.confidence, 0.93)
        self.assertAlmostEqual(card.video_time or 0.0, 42.5)
        self.assertEqual(
            card.monitor,
            {"width": 1920, "height": 1080, "scale": 1.25},
        )
        self.assertIsInstance(card.monitor, dict)
        self.assertEqual(card.stance, "agree")
        self.assertEqual(card.note, "这条观点值得继续验证。")

    def test_accepts_supported_stances_and_confidence_boundaries(self) -> None:
        """所有约定态度及置信度边界值都应合法。"""

        stances = ("unknown", "agree", "disagree", "doubt", "useful")
        for stance in stances:
            for confidence in (0.0, 1.0):
                with self.subTest(stance=stance, confidence=confidence):
                    data = _minimal_card_data()
                    data["stance"] = stance
                    data["confidence"] = confidence

                    card = Card(**data)

                    self.assertEqual(card.stance, stance)
                    self.assertEqual(card.confidence, confidence)

    def test_rejects_confidence_outside_unit_interval(self) -> None:
        """置信度只能位于闭区间 0 到 1。"""

        for confidence in (-0.01, 1.01):
            with self.subTest(confidence=confidence):
                data = _minimal_card_data()
                data["confidence"] = confidence

                with self.assertRaises(ValidationError):
                    Card(**data)

    def test_rejects_unsupported_stance(self) -> None:
        """不在约定枚举中的态度不得写入观点库。"""

        with self.assertRaises(ValidationError):
            Card(**_minimal_card_data(), stance="liked")

    def test_rejects_id_that_is_not_uuid4(self) -> None:
        """卡片标识必须是格式正确的 UUID4。"""

        invalid_ids = (
            "not-a-uuid",
            "6ba7b810-9dad-11d1-80b4-00c04fd430c8",  # UUID1
        )
        for invalid_id in invalid_ids:
            with self.subTest(card_id=invalid_id):
                with self.assertRaises(ValidationError):
                    Card(**_minimal_card_data(), id=invalid_id)

    def test_rejects_absolute_screenshot_paths(self) -> None:
        """截图只能保存相对路径，避免记录或访问任意本机位置。"""

        unsafe_paths = (
            "C:/Users/example/private.png",
            "C:private.png",
            "/tmp/private.png",
            r"\\server\share\private.png",
        )
        for field_name in ("screenshot_path", "full_screenshot_path"):
            for unsafe_path in unsafe_paths:
                with self.subTest(field=field_name, path=unsafe_path):
                    data = _minimal_card_data()
                    data[field_name] = unsafe_path

                    with self.assertRaises(ValidationError):
                        Card(**data)

    def test_rejects_screenshot_paths_with_parent_traversal(self) -> None:
        """截图相对路径不得通过 .. 逃离数据目录。"""

        unsafe_paths = (
            "screenshots/../private.png",
            r"screenshots\..\private.png",
        )
        for field_name in ("screenshot_path", "full_screenshot_path"):
            for unsafe_path in unsafe_paths:
                with self.subTest(field=field_name, path=unsafe_path):
                    data = _minimal_card_data()
                    data[field_name] = unsafe_path

                    with self.assertRaises(ValidationError):
                        Card(**data)

    def test_rejects_monitor_with_missing_or_extra_fields(self) -> None:
        """显示器元数据必须且只能包含宽、高和缩放比例。"""

        invalid_monitors = (
            {"width": 1920, "height": 1080},
            {"width": 1920, "height": 1080, "scale": 1.0, "rotation": 0},
        )
        for monitor in invalid_monitors:
            with self.subTest(monitor=monitor):
                with self.assertRaises(ValidationError):
                    Card(**_minimal_card_data(), monitor=monitor)

    def test_rejects_non_positive_monitor_values(self) -> None:
        """显示器宽、高和缩放比例都必须大于零。"""

        valid_monitor = {"width": 1920, "height": 1080, "scale": 1.25}
        for field_name in ("width", "height", "scale"):
            for invalid_value in (0, -1):
                with self.subTest(field=field_name, value=invalid_value):
                    monitor = {**valid_monitor, field_name: invalid_value}

                    with self.assertRaises(ValidationError):
                        Card(**_minimal_card_data(), monitor=monitor)


if __name__ == "__main__":
    unittest.main()
