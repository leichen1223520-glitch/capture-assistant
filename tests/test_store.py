"""SQLite/FTS5 卡片仓库的 CRUD、检索与删除测试。"""

from __future__ import annotations

import json
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
    text_source: str = "ocr",
    stance: str = "unknown",
    title: str | None = None,
    note: str = "",
) -> Card:
    return Card(
        text=text,
        text_source=text_source,
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
        stance=stance,
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

    def test_legacy_database_migrates_existing_cards_as_saved(self) -> None:
        legacy_db = self.data_dir / "legacy.sqlite3"
        card = _card("旧库中的正式观点", suffix="legacy", title="旧库标题")
        with closing(sqlite3.connect(legacy_db)) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE cards (
                    id TEXT PRIMARY KEY NOT NULL,
                    text TEXT NOT NULL,
                    text_source TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    screenshot_path TEXT NOT NULL,
                    full_screenshot_path TEXT NOT NULL,
                    source_url TEXT,
                    source_title TEXT,
                    video_time REAL,
                    app_name TEXT,
                    monitor_json TEXT,
                    created_at TEXT NOT NULL,
                    stance TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT ''
                );
                CREATE VIRTUAL TABLE cards_fts USING fts5(
                    card_id UNINDEXED,
                    text,
                    source_title,
                    note,
                    tokenize = 'unicode61'
                );
                """
            )
            connection.execute(
                """
                INSERT INTO cards VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    card.id,
                    card.text,
                    card.text_source,
                    card.confidence,
                    card.screenshot_path,
                    card.full_screenshot_path,
                    card.source_url,
                    card.source_title,
                    card.video_time,
                    card.app_name,
                    json.dumps(card.monitor),
                    card.created_at,
                    card.stance,
                    card.note,
                ),
            )

        migrated = Store(legacy_db, self.data_dir)
        migrated.init_db()

        self.assertEqual(migrated.get_card(card.id), card)
        self.assertEqual([item.id for item in migrated.search("正式观点")], [card.id])
        with closing(sqlite3.connect(legacy_db)) as connection:
            migrated_columns = connection.execute(
                "SELECT record_state, edited_text FROM cards WHERE id = ?",
                (card.id,),
            ).fetchone()
            fts_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(cards_fts)").fetchall()
            }
        self.assertEqual(migrated_columns, ("saved", None))
        self.assertIn("edited_text", fts_columns)

    def test_draft_is_hidden_from_get_list_search_and_fts(self) -> None:
        draft = _card("尚未审核的候选观点", suffix="hidden-draft")
        self._write_screenshots(draft)

        self.store.add_draft(draft)

        self.assertIsNone(self.store.get_card(draft.id))
        self.assertEqual(self.store.list_recent(), [])
        self.assertEqual(self.store.search("候选观点"), [])
        with closing(sqlite3.connect(self.store.db_path)) as connection:
            state = connection.execute(
                "SELECT record_state FROM cards WHERE id = ?", (draft.id,)
            ).fetchone()
            indexed = connection.execute(
                "SELECT count(*) FROM cards_fts WHERE card_id = ?", (draft.id,)
            ).fetchone()
        self.assertEqual(state, ("draft",))
        self.assertEqual(indexed, (0,))

    def test_finalize_draft_makes_edited_card_visible_and_searchable(self) -> None:
        draft = _card("审核前的候选文字", suffix="finalize")
        self.store.add_draft(draft)

        finalized = self.store.finalize_draft(
            draft.id,
            edited_text="审核后的正式观点",
            stance="agree",
            note="已由用户确认",
        )

        self.assertEqual(finalized.text, "审核前的候选文字")
        self.assertEqual(finalized.edited_text, "审核后的正式观点")
        self.assertEqual(finalized.stance, "agree")
        self.assertEqual(self.store.get_card(draft.id), finalized)
        self.assertEqual(self.store.list_recent(), [finalized])
        self.assertEqual([item.id for item in self.store.search("正式观点")], [draft.id])
        self.assertEqual([item.id for item in self.store.search("候选文字")], [draft.id])
        with closing(sqlite3.connect(self.store.db_path)) as connection:
            state = connection.execute(
                "SELECT record_state FROM cards WHERE id = ?", (draft.id,)
            ).fetchone()
            indexed = connection.execute(
                "SELECT count(*) FROM cards_fts WHERE card_id = ?", (draft.id,)
            ).fetchone()
        self.assertEqual(state, ("saved",))
        self.assertEqual(indexed, (1,))

    def test_cleanup_drafts_deletes_only_drafts_and_their_images(self) -> None:
        saved = _card("需要保留的正式资料", suffix="saved")
        draft_one = _card("异常退出草稿一", suffix="draft-one")
        draft_two = _card("异常退出草稿二", suffix="draft-two")
        draft_one.screenshot_path = f"screenshots/{draft_one.id}.png"
        draft_one.full_screenshot_path = f"screenshots/full_{draft_one.id}.png"
        draft_two.screenshot_path = f"screenshots/{draft_two.id}.png"
        draft_two.full_screenshot_path = f"screenshots/full_{draft_two.id}.png"
        saved_paths = self._write_screenshots(saved)
        draft_paths = (
            *self._write_screenshots(draft_one),
            *self._write_screenshots(draft_two),
        )
        self.store.add_card(saved)
        self.store.add_draft(draft_one)
        self.store.add_draft(draft_two)

        self.assertEqual(self.store.cleanup_drafts(), 2)

        self.assertEqual(self.store.get_card(saved.id), saved)
        self.assertTrue(all(path.exists() for path in saved_paths))
        self.assertTrue(all(not path.exists() for path in draft_paths))
        self.assertEqual(self.store.cleanup_drafts(), 0)
        with closing(sqlite3.connect(self.store.db_path)) as connection:
            remaining_drafts = connection.execute(
                "SELECT count(*) FROM cards WHERE record_state = 'draft'"
            ).fetchone()
        self.assertEqual(remaining_drafts, (0,))

    def test_cleanup_preflight_rejects_tampered_database_path_without_partial_delete(
        self,
    ) -> None:
        safe_draft = _card("应保留到人工处理的草稿", suffix="safe-preflight")
        tampered = _card("被篡改的草稿", suffix="tampered-preflight")
        for card in (safe_draft, tampered):
            card.screenshot_path = f"screenshots/{card.id}.png"
            card.full_screenshot_path = f"screenshots/full_{card.id}.png"
            self._write_screenshots(card)
            self.store.add_draft(card)
        safe_paths = (
            self.data_dir / safe_draft.screenshot_path,
            self.data_dir / safe_draft.full_screenshot_path,
        )
        with closing(sqlite3.connect(self.store.db_path)) as connection, connection:
            connection.execute(
                "UPDATE cards SET screenshot_path = 'cards.sqlite3' WHERE id = ?",
                (tampered.id,),
            )

        with self.assertRaisesRegex(StoreError, "安全清理规范"):
            self.store.cleanup_drafts()

        self.assertTrue(self.store.db_path.is_file())
        self.assertTrue(all(path.is_file() for path in safe_paths))
        with closing(sqlite3.connect(self.store.db_path)) as connection:
            remaining = connection.execute(
                "SELECT count(*) FROM cards WHERE record_state = 'draft'"
            ).fetchone()
        self.assertEqual(remaining, (2,))

    def test_cleanup_never_deletes_database_even_if_it_has_canonical_png_name(
        self,
    ) -> None:
        draft = _card("数据库路径伪装成截图", suffix="database-disguise")
        draft.screenshot_path = f"screenshots/{draft.id}.png"
        draft.full_screenshot_path = f"screenshots/full_{draft.id}.png"
        disguised_database = self.data_dir / draft.screenshot_path
        dangerous_store = Store(disguised_database, self.data_dir)
        dangerous_store.init_db()
        dangerous_store.add_draft(draft)

        with self.assertRaisesRegex(StoreError, "指向本地数据库"):
            dangerous_store.cleanup_drafts()

        self.assertTrue(disguised_database.is_file())
        with closing(sqlite3.connect(disguised_database)) as connection:
            remaining = connection.execute(
                "SELECT count(*) FROM cards WHERE id = ?", (draft.id,)
            ).fetchone()
        self.assertEqual(remaining, (1,))

    def test_add_and_get_preserve_full_card_contract(self) -> None:
        card = _card("本地数据默认不上传", suffix="one", title="隐私原则")

        saved = self.store.add_card(card)
        loaded = self.store.get_card(card.id)

        self.assertEqual(saved, card)
        self.assertEqual(loaded, card)

    def test_round_trip_preserves_separate_edited_text(self) -> None:
        base = _card("OCR 最初提取的文字", suffix="edited")
        card = Card(
            **{
                **base.model_dump(),
                "edited_text": "审核后的可读文字",
            }
        )

        self.store.add_card(card)

        loaded = self.store.get_card(card.id)
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.text, "OCR 最初提取的文字")
        self.assertEqual(loaded.edited_text, "审核后的可读文字")

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

    def test_query_cards_filters_in_database_before_limit(self) -> None:
        matching = _card(
            "共同关键词：应被找到",
            suffix="matching",
            text_source="dom",
            stance="agree",
            created_at="2026-07-18T19:00:00+08:00",
        )
        newer_nonmatching = _card(
            "共同关键词：但态度不同",
            suffix="newer-nonmatching",
            text_source="ocr",
            stance="doubt",
            created_at="2026-07-18T22:00:00+08:00",
        )
        self.store.add_card(matching)
        self.store.add_card(newer_nonmatching)

        result = self.store.query_cards(
            "共同关键词",
            stance="agree",
            text_source="dom",
            limit=1,
        )

        self.assertEqual([card.id for card in result], [matching.id])
        self.assertEqual(
            [card.id for card in self.store.query_cards(stance="doubt")],
            [newer_nonmatching.id],
        )
        with self.assertRaises(StoreError):
            self.store.query_cards(stance="invalid")  # type: ignore[arg-type]
        with self.assertRaises(StoreError):
            self.store.query_cards(text_source="invalid")  # type: ignore[arg-type]
        with self.assertRaisesRegex(StoreError, "500"):
            self.store.query_cards("x" * 501)
        with self.assertRaisesRegex(StoreError, "500"):
            self.store.search("x" * 501)


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
            edited_text="新的可检索观点",
            stance="doubt",
            note="需要来源核查",
        )

        self.assertEqual(updated.stance, "doubt")
        self.assertEqual(updated.text, "旧的关键词")
        self.assertEqual(updated.edited_text, "新的可检索观点")
        self.assertEqual(self.store.get_card(card.id), updated)
        self.assertEqual([item.id for item in self.store.search("旧的关键词")], [card.id])
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
        with self.assertRaises(StoreError):
            self.store.update_card(card.id, text="不允许覆盖原始提取文字")
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

    def test_delete_draft_never_deletes_saved_card_or_evidence(self) -> None:
        card = _card("正式卡片不能被草稿回滚误删", suffix="saved-protected")
        selected, full = self._write_screenshots(card)
        self.store.add_card(card)

        self.assertFalse(self.store.delete_draft(card.id))

        self.assertEqual(self.store.get_card(card.id), card)
        self.assertTrue(selected.is_file())
        self.assertTrue(full.is_file())

    def test_delete_draft_removes_only_matching_draft(self) -> None:
        draft = _card("保存阶段短暂草稿", suffix="draft-delete")
        draft.screenshot_path = f"screenshots/{draft.id}.png"
        draft.full_screenshot_path = f"screenshots/full_{draft.id}.png"
        selected, full = self._write_screenshots(draft)
        self.store.add_draft(draft)

        self.assertTrue(self.store.delete_draft(draft.id))

        self.assertFalse(selected.exists())
        self.assertFalse(full.exists())
        self.assertFalse(self.store.delete_draft(draft.id))

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

    def test_delete_refuses_path_inside_data_but_outside_screenshot_root(self) -> None:
        card = _card("截图根目录边界", suffix="outside-screenshot-root")
        card.screenshot_path = "other/evidence.png"
        card.full_screenshot_path = "other/full_evidence.png"
        selected, full = self._write_screenshots(card)
        self.store.add_card(card)

        with self.assertRaisesRegex(StoreError, "受控截图目录"):
            self.store.delete_card(card.id)

        self.assertTrue(selected.is_file())
        self.assertTrue(full.is_file())
        self.assertEqual(self.store.get_card(card.id), card)

    def test_delete_refuses_screenshot_referenced_by_another_card(self) -> None:
        first = _card("第一张卡片", suffix="shared-first")
        second = _card("第二张卡片", suffix="shared-second")
        second.screenshot_path = first.screenshot_path
        first_paths = self._write_screenshots(first)
        self._write_screenshots(second)
        self.store.add_card(first)
        self.store.add_card(second)

        with self.assertRaisesRegex(StoreError, "其他记录引用"):
            self.store.delete_card(first.id)

        self.assertTrue(all(path.is_file() for path in first_paths))
        self.assertEqual(self.store.get_card(first.id), first)
        self.assertEqual(self.store.get_card(second.id), second)

    def test_cleanup_refuses_draft_path_referenced_by_saved_card(self) -> None:
        draft = _card("待清理草稿", suffix="draft-cross-reference")
        draft.screenshot_path = f"screenshots/{draft.id}.png"
        draft.full_screenshot_path = f"screenshots/full_{draft.id}.png"
        saved = _card("仍需保留的卡片", suffix="saved-cross-reference")
        saved.screenshot_path = draft.screenshot_path
        draft_paths = self._write_screenshots(draft)
        self._write_screenshots(saved)
        self.store.add_draft(draft)
        self.store.add_card(saved)

        with self.assertRaisesRegex(StoreError, "其他记录引用"):
            self.store.cleanup_drafts()

        self.assertTrue(all(path.is_file() for path in draft_paths))
        self.assertEqual(self.store.get_card(saved.id), saved)
        with closing(sqlite3.connect(self.store.db_path)) as connection:
            draft_exists = connection.execute(
                "SELECT count(*) FROM cards WHERE id = ?", (draft.id,)
            ).fetchone()
        self.assertEqual(draft_exists, (1,))

    def test_pagination_rejects_unbounded_or_invalid_values(self) -> None:
        for limit, offset in ((0, 0), (501, 0), (True, 0), (10, -1)):
            with self.subTest(limit=limit, offset=offset):
                with self.assertRaises(StoreError):
                    self.store.list_recent(limit=limit, offset=offset)


if __name__ == "__main__":
    unittest.main()
