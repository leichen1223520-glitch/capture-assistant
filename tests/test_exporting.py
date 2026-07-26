"""观点卡片 JSON/Markdown 只读导出的安全性测试。"""

from __future__ import annotations

import json
import re
import unittest
from uuid import uuid4

from app.exporting import cards_to_json, cards_to_markdown
from app.models import Card


def _card(*, text: str, source_url: str | None = None, note: str = "") -> Card:
    card_id = str(uuid4())
    return Card(
        id=card_id,
        text=text,
        edited_text="人工整理 ``` 仍然只是资料",
        text_source="ocr",
        confidence=0.8,
        screenshot_path=f"screenshots/{card_id}.png",
        full_screenshot_path=f"screenshots/full_{card_id}.png",
        source_url=source_url,
        source_title="来源标题",
        created_at="2026-07-26T12:00:00+08:00",
        stance="doubt",
        note=note,
    )


class ExportingTests(unittest.TestCase):
    def test_json_keeps_original_and_edited_text_distinct(self) -> None:
        card = _card(text="不可覆盖的原文", note="用户备注")

        payload = json.loads(cards_to_json([card]))

        self.assertEqual(payload[0]["text"], "不可覆盖的原文")
        self.assertEqual(payload[0]["edited_text"], "人工整理 ``` 仍然只是资料")
        self.assertEqual(payload[0]["stance"], "doubt")

    def test_markdown_uses_longer_fence_than_untrusted_backticks(self) -> None:
        malicious = (
            "证据开始\n```\n<script>alert('x')</script>\n"
            "``````\n# 伪造标题\n证据结束"
        )
        card = _card(
            text=malicious,
            source_url="javascript:alert(1)",
            note="```` 试图闭合备注",
        )

        exported = cards_to_markdown([card])

        fence_lines = [
            line
            for line in exported.splitlines()
            if line.endswith("text") and set(line.removesuffix("text")) == {"`"}
        ]
        self.assertTrue(fence_lines)
        original_fence_length = len(fence_lines[0]) - len("text")
        longest_untrusted_run = max(
            len(match.group(0)) for match in re.finditer(r"`+", malicious)
        )
        self.assertGreater(original_fence_length, longest_untrusted_run)
        self.assertNotIn("[打开来源]", exported)
        self.assertIn("javascript:alert(1)", exported)

    def test_markdown_only_links_http_or_https_sources(self) -> None:
        card = _card(
            text="普通资料",
            source_url="https://example.test/watch?q=本地 资料",
        )

        exported = cards_to_markdown([card])

        self.assertIn("[打开来源](<https://example.test/", exported)
        self.assertIn("%20", exported)


if __name__ == "__main__":
    unittest.main()
