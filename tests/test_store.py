"""SQLite/FTS5 卡片仓库的 CRUD、检索与删除测试。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from app.models import Card
from app.store import Store, StoreError


def _card(
    text: str,
    *,
    suffix: str,
    created_at: str = "2026-07-18T20:00:00+08:00",
    title: str | None = None,
    note: str = "",
) -> Card:
    return Card(
        text=text,
        text_source="ocr",
        confidence=0.9,
        screenshot_path=f"screenshots/{suffix}.png",
        full_screenshot_path=f"screenshots/full_{suffix}.png",
        source_url="https://example.com/watch?v=local",
        source_title=title,
        video_time=12.5,
        app_name="chrome.exe",
        monitor={"width": 1920, "height": 1080, "scale": 1.25},
        created_at=created_at,
        note=note,
    )


class StoreTests(unittest.TestCase):
    """每项测试使用独立 D 盘临时数据库和伪截图文件。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temporary.name) / "data"
        self.store = Store(
            db_path=self.data_dir / "cards.sqlite3",
            data_dir=self.data_dir,
        )
        self.store.init_db()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_screenshots(self, card: Card) -> tuple[Path, Path]:
        selected = self.data_dir / card.screenshot_path
        full = self.data_dir / card.full_screenshot_path
        selected.parent.mkdir(parents=True, exist_ok=True)
        selected.write_bytes(b"selected-image")
        full.write_bytes(b"full-image")
        return selected, full

    def test_init_is_idempotent_and_records_tokenizer(self) -> None:
        self.store.init_db()

        self.assertIn(self.store.fts_tokenizer(), {"trigram", "unicode61"})

    def test_add_and_get_preserve_full_card_contract(self) -> None:
        card = _card("本地数据默认不上传", suffix="one", title="隐私原则")

        saved = self.store.add_card(card)
        loaded = self.store.get_card(card.id)

        self.assertEqual(saved, card)
        self.assertEqual(loaded, card)

    def test_duplicate_id_is_rejected(self) -> None:
        card = _card("重复卡片", suffix="duplicate")
        self.store.add_card(card)

        with self.assertRaisesRegex(StoreError, "ID 已存在"):
            self.store.add_card(card)

    def test_lists_recent_with_limit_and_offset(self) -> None:
        old = _card(
            "较早观点",
            suffix="old",
            created_at="2026-07-18T20:00:00+08:00",
        )
        new = _card(
            "较新观点",
            suffix="new",
            created_at="2026-07-18T22:00:00+08:00",
        )
        self.store.add_card(old)
        self.store.add_card(new)

        self.assertEqual(self.store.list_recent(limit=1), [new])
        self.assertEqual(self.store.list_recent(limit=1, offset=1), [old])

    def test_searches_text_title_note_and_chinese_short_query(self) -> None:
        text_card = _card("本地优先的观点采集助手", suffix="text")
        title_card = _card("其他正文", suffix="title", title="证据来源标题")
        note_card = _card("待核实内容", suffix="note", note="稍后查证数据")
        for card in (text_card, title_card, note_card):
            self.store.add_card(card)

        self.assertEqual([card.id for card in self.store.search("观点采集")], [text_card.id])
        self.assertEqual([card.id for card in self.store.search("来源标题")], [title_card.id])
        self.assertEqual([card.id for card in self.store.search("查证数据")], [note_card.id])
        self.assertEqual([card.id for card in self.store.search("观点")], [text_card.id])
        self.assertEqual(self.store.search("   "), [])

    def test_unicode61_metadata_uses_substring_fallback_for_chinese(self) -> None:
        card = _card("本地优先的观点采集助手", suffix="unicode-fallback")
        self.store.add_card(card)
        with closing(sqlite3.connect(self.store.db_path)) as connection, connection:
            connection.execute(
                "UPDATE app_meta SET value = 'unicode61' WHERE key = 'fts_tokenizer'"
            )

        self.assertEqual([item.id for item in self.store.search("观点采集")], [card.id])

    def test_search_handles_fts_special_characters_as_data(self) -> None:
        card = _card('用户写下 "A-B" 与 100% 证据', suffix="special")
        self.store.add_card(card)

        self.assertEqual([item.id for item in self.store.search('"A-B"')], [card.id])
        self.assertEqual(self.store.search("不存在 OR 1"), [])
        with self.assertRaises(StoreError):
            self.store.search("bad\x00query")

    def test_update_validates_fields_and_refreshes_fts(self) -> None:
        card = _card("旧的关键词", suffix="update")
        self.store.add_card(card)

        updated = self.store.update_card(
            card.id,
            text="新的可检索观点",
            stance="doubt",
            note="需要来源核查",
        )

        self.assertEqual(updated.stance, "doubt")
        self.assertEqual(self.store.get_card(card.id), updated)
        self.assertEqual(self.store.search("旧的关键词"), [])
        self.assertEqual([item.id for item in self.store.search("可检索观点")], [card.id])
        self.assertEqual([item.id for item in self.store.search("来源核查")], [card.id])

    def test_update_rejects_unknown_immutable_and_invalid_fields(self) -> None:
        card = _card("不能破坏契约", suffix="invalid-update")
        self.store.add_card(card)

        with self.assertRaises(StoreError):
            self.store.update_card(card.id, screenshot_path="other.png")
        with self.assertRaises(StoreError):
            self.store.update_card(card.id, confidence=2.0)
        with self.assertRaises(StoreError):
            self.store.update_card(card.id, unexpected="value")
        self.assertEqual(self.store.get_card(card.id), card)

    def test_delete_removes_card_fts_and_both_screenshots(self) -> None:
        card = _card("删除后不可检索", suffix="delete")
        selected, full = self._write_screenshots(card)
        self.store.add_card(card)

        deleted = self.store.delete_card(card.id)

        self.assertTrue(deleted)
        self.assertFalse(selected.exists())
        self.assertFalse(full.exists())
        self.assertIsNone(self.store.get_card(card.id))
        self.assertEqual(self.store.search("不可检索"), [])
        self.assertFalse(self.store.delete_card(card.id))

    def test_delete_handles_same_screenshot_path_once(self) -> None:
        card = _card("共享同一截图路径", suffix="same")
        card.full_screenshot_path = card.screenshot_path
        selected = self.data_dir / card.screenshot_path
        selected.parent.mkdir(parents=True, exist_ok=True)
        selected.write_bytes(b"one-file")
        self.store.add_card(card)

        self.assertTrue(self.store.delete_card(card.id))
        self.assertFalse(selected.exists())

    def test_delete_refuses_tampered_parent_traversal_path(self) -> None:
        card = _card("数据库被篡改时也不能越界删除", suffix="tampered")
        self.store.add_card(card)
        outside = self.data_dir.parent / "outside-user-file.png"
        outside.write_bytes(b"must-survive")
        with closing(sqlite3.connect(self.store.db_path)) as connection, connection:
            connection.execute(
                "UPDATE cards SET screenshot_path = '../outside-user-file.png' WHERE id = ?",
                (card.id,),
            )

        with self.assertRaises(StoreError):
            self.store.delete_card(card.id)

        self.assertTrue(outside.exists())
        with closing(sqlite3.connect(self.store.db_path)) as connection:
            remaining = connection.execute(
                "SELECT count(*) FROM cards WHERE id = ?",
                (card.id,),
            ).fetchone()[0]
        self.assertEqual(remaining, 1)

    def test_pagination_rejects_unbounded_or_invalid_values(self) -> None:
        for limit, offset in ((0, 0), (501, 0), (True, 0), (10, -1)):
            with self.subTest(limit=limit, offset=offset):
                with self.assertRaises(StoreError):
                    self.store.list_recent(limit=limit, offset=offset)


if __name__ == "__main__":
    unittest.main()
