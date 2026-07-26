"""在 Qt offscreen 环境验证本地观点库的检索、编辑、删除与导出。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox  # noqa: E402

from app.library import (  # noqa: E402
    LibraryWindow,
    _controlled_screenshot_file,
    _safe_http_url,
)
from app.models import Card  # noqa: E402
from app.store import Store  # noqa: E402


class LibraryWindowTests(unittest.TestCase):
    application: QApplication

    @classmethod
    def setUpClass(cls) -> None:
        instance = QApplication.instance()
        cls.application = (
            instance if isinstance(instance, QApplication) else QApplication([])
        )

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.data_dir = self.root / "data"
        self.screenshot_dir = self.data_dir / "screenshots"
        self.screenshot_dir.mkdir(parents=True)
        self.store = Store(self.data_dir / "cards.sqlite3", self.data_dir)
        self.store.init_db()
        self.windows: list[LibraryWindow] = []

    def tearDown(self) -> None:
        for window in self.windows:
            window.wait_for_idle(3_000)
            window.close()
            window.deleteLater()
        self.application.processEvents()
        self._temporary.cleanup()

    def _card(
        self,
        text: str,
        *,
        title: str,
        stance: str = "unknown",
        source: str = "ocr",
        note: str = "",
        url: str | None = "https://example.test/watch?v=1",
    ) -> Card:
        card_id = str(uuid4())
        card = Card(
            id=card_id,
            text=text,
            text_source=source,
            confidence=0.88,
            screenshot_path=f"screenshots/{card_id}.png",
            full_screenshot_path=f"screenshots/full_{card_id}.png",
            source_url=url,
            source_title=title,
            video_time=12.5,
            stance=stance,
            note=note,
        )
        Image.new("RGB", (160, 80), "white").save(
            self.data_dir / card.screenshot_path
        )
        Image.new("RGB", (320, 180), "black").save(
            self.data_dir / card.full_screenshot_path
        )
        return self.store.add_card(card)

    def _window(self) -> LibraryWindow:
        window = LibraryWindow(self.store, data_dir=self.data_dir)
        self.windows.append(window)
        self.assertTrue(window.wait_for_idle(3_000), "观点库初次读取超时")
        self.application.processEvents()
        return window

    def _select_card(self, window: LibraryWindow, card_id: str) -> None:
        for row in range(window.card_list.count()):
            item = window.card_list.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == card_id:
                window.card_list.setCurrentRow(row)
                self.application.processEvents()
                return
        self.fail(f"列表中没有卡片 {card_id}")

    def _wait(self, window: LibraryWindow) -> None:
        self.assertTrue(window.wait_for_idle(3_000), "观点库后台操作超时")
        self.application.processEvents()

    def test_lists_searches_and_filters_cards_with_read_only_evidence(self) -> None:
        useful = self._card(
            "真正有价值的采集",
            title="<b>不应当成富文本的标题</b>",
            stance="useful",
            source="dom",
        )
        self._card(
            "另一条画面观点",
            title="OCR 来源",
            stance="doubt",
            source="ocr",
        )
        window = self._window()

        self.assertEqual(window.card_list.count(), 2)
        self._select_card(window, useful.id)
        self.assertTrue(window.original_text.isReadOnly())
        self.assertEqual(window.original_text.toPlainText(), useful.text)
        self.assertEqual(
            window.source_title_label.text(),
            "<b>不应当成富文本的标题</b>",
        )
        current_item = window.card_list.currentItem()
        self.assertIsNotNone(current_item)
        assert current_item is not None
        self.assertEqual(
            current_item.toolTip(),
            "&lt;b&gt;不应当成富文本的标题&lt;/b&gt;",
        )
        self.assertIsNotNone(window.selected_screenshot.pixmap())
        self.assertFalse(window.selected_screenshot.pixmap().isNull())
        self.assertIsNotNone(window.full_screenshot.pixmap())
        self.assertFalse(window.full_screenshot.pixmap().isNull())

        window.search_edit.setText("画面观点")
        QTest.mouseClick(window.search_button, Qt.MouseButton.LeftButton)
        self._wait(window)
        self.assertEqual(window.card_list.count(), 1)
        self.assertIn("OCR 来源", window.card_list.item(0).text())

        window.search_edit.clear()
        window.stance_filter.setCurrentIndex(
            window.stance_filter.findData("useful")
        )
        window.source_filter.setCurrentIndex(window.source_filter.findData("dom"))
        QTest.mouseClick(window.search_button, Qt.MouseButton.LeftButton)
        self._wait(window)
        self.assertEqual(window.card_list.count(), 1)
        self.assertEqual(
            window.card_list.item(0).data(Qt.ItemDataRole.UserRole),
            useful.id,
        )

    def test_saves_only_editable_fields_and_preserves_original_evidence(self) -> None:
        card = self._card("不可覆盖的原始观点", title="保存测试")
        window = self._window()
        self._select_card(window, card.id)

        window.edited_text.setPlainText("人工整理后的观点")
        window.stance_edit.setCurrentIndex(window.stance_edit.findData("agree"))
        window.note_edit.setPlainText("这是用户明确写下的备注")
        QTest.mouseClick(window.save_button, Qt.MouseButton.LeftButton)
        self._wait(window)

        saved = self.store.get_card(card.id)
        self.assertIsNotNone(saved)
        assert saved is not None
        self.assertEqual(saved.text, "不可覆盖的原始观点")
        self.assertEqual(saved.edited_text, "人工整理后的观点")
        self.assertEqual(saved.stance, "agree")
        self.assertEqual(saved.note, "这是用户明确写下的备注")
        self.assertEqual(window.original_text.toPlainText(), saved.text)
        self.assertIn("修改已安全保存", window.status_label.text())
        self.assertEqual(self.store.search("人工整理后的观点")[0].id, card.id)

    def test_delete_requires_confirmation_and_removes_record_and_images(self) -> None:
        card = self._card("等待彻底删除", title="删除测试")
        selected = self.data_dir / card.screenshot_path
        full = self.data_dir / card.full_screenshot_path
        window = self._window()
        self._select_card(window, card.id)

        with patch.object(
            QMessageBox,
            "warning",
            return_value=QMessageBox.StandardButton.Cancel,
        ):
            QTest.mouseClick(window.delete_button, Qt.MouseButton.LeftButton)
        self.assertIsNotNone(self.store.get_card(card.id))
        self.assertTrue(selected.exists())
        self.assertTrue(full.exists())

        with patch.object(
            QMessageBox,
            "warning",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            QTest.mouseClick(window.delete_button, Qt.MouseButton.LeftButton)
        self._wait(window)

        self.assertIsNone(self.store.get_card(card.id))
        self.assertFalse(selected.exists())
        self.assertFalse(full.exists())
        self.assertEqual(window.card_list.count(), 0)
        self.assertIn("彻底删除", window.status_label.text())

    def test_exports_current_results_to_selected_json_and_markdown_paths(self) -> None:
        included = self._card(
            "需要导出的观点",
            title="导出来源",
            stance="agree",
            source="dom",
        )
        self._card("不在筛选结果中", title="其他来源", stance="doubt")
        window = self._window()
        window.stance_filter.setCurrentIndex(window.stance_filter.findData("agree"))
        QTest.mouseClick(window.search_button, Qt.MouseButton.LeftButton)
        self._wait(window)
        self.assertEqual(window.card_list.count(), 1)

        json_path = self.root / "chosen-export"
        with patch.object(
            QFileDialog,
            "getSaveFileName",
            return_value=(str(json_path), "JSON 文件 (*.json)"),
        ):
            QTest.mouseClick(
                window.export_json_button,
                Qt.MouseButton.LeftButton,
            )
        self._wait(window)
        json_target = json_path.with_name(json_path.name + ".json")
        payload = json.loads(json_target.read_text(encoding="utf-8"))
        self.assertEqual([item["id"] for item in payload], [included.id])
        self.assertIsNotNone(self.store.get_card(included.id))

        markdown_path = self.root / "chosen-export.md"
        with patch.object(
            QFileDialog,
            "getSaveFileName",
            return_value=(str(markdown_path), "Markdown 文件 (*.md)"),
        ):
            QTest.mouseClick(
                window.export_markdown_button,
                Qt.MouseButton.LeftButton,
            )
        self._wait(window)
        markdown = markdown_path.read_text(encoding="utf-8")
        self.assertIn("需要导出的观点", markdown)
        self.assertIn(included.id, markdown)
        self.assertNotIn("不在筛选结果中", markdown)

    def test_dirty_edits_block_export_and_refresh_can_be_cancelled(self) -> None:
        card = self._card("尚未保存的观点", title="未保存保护")
        window = self._window()
        self.assertEqual(window.search_edit.maxLength(), 500)
        self._select_card(window, card.id)
        window.edited_text.setPlainText("仍在编辑中的文字")

        with (
            patch.object(QMessageBox, "information") as information,
            patch.object(QFileDialog, "getSaveFileName") as save_dialog,
        ):
            QTest.mouseClick(window.export_json_button, Qt.MouseButton.LeftButton)
        information.assert_called_once()
        save_dialog.assert_not_called()

        with patch.object(
            QMessageBox,
            "warning",
            return_value=QMessageBox.StandardButton.Cancel,
        ):
            self.assertFalse(window.request_refresh())
        self.assertEqual(window.edited_text.toPlainText(), "仍在编辑中的文字")

        with patch.object(
            QMessageBox,
            "warning",
            return_value=QMessageBox.StandardButton.Discard,
        ):
            self.assertTrue(window.request_refresh())
        self._wait(window)
        self.assertEqual(window.edited_text.toPlainText(), card.text)

    def test_close_discard_restores_persisted_values(self) -> None:
        card = self._card("关闭前的原始观点", title="关闭保护", note="已保存备注")
        window = self._window()
        self._select_card(window, card.id)
        window.edited_text.setPlainText("不应保留的临时编辑")
        window.note_edit.setPlainText("不应保留的临时备注")

        with patch.object(
            QMessageBox,
            "warning",
            return_value=QMessageBox.StandardButton.Discard,
        ):
            self.assertTrue(window.close())

        self.assertEqual(window.edited_text.toPlainText(), card.text)
        self.assertEqual(window.note_edit.toPlainText(), "已保存备注")
    def test_rejects_mismatched_data_root_and_unsafe_screenshot_paths(self) -> None:
        outside = self.root / "outside.png"
        Image.new("RGB", (10, 10), "red").save(outside)

        self.assertIsNone(
            _controlled_screenshot_file(self.data_dir, "../outside.png")
        )
        self.assertIsNone(
            _controlled_screenshot_file(self.data_dir, str(outside))
        )
        fake_png = self.screenshot_dir / "fake.png"
        fake_png.write_bytes(b"not-a-png")
        self.assertIsNone(
            _controlled_screenshot_file(self.data_dir, "screenshots/fake.png")
        )
        self.assertIsNone(_safe_http_url("javascript:alert(1)"))
        self.assertIsNone(_safe_http_url("https://example.test/\nunsafe"))
        with self.assertRaisesRegex(ValueError, "数据目录.*不一致"):
            LibraryWindow(self.store, data_dir=self.root / "other")

    def test_rejects_export_over_database_or_managed_screenshot(self) -> None:
        self._card("保护受管文件", title="路径保护")
        window = self._window()

        with self.assertRaisesRegex(RuntimeError, "数据库或证据截图"):
            window._validate_export_target(self.store.db_path, ".json")
        with self.assertRaisesRegex(RuntimeError, "数据库或证据截图"):
            window._validate_export_target(
                self.screenshot_dir / "overwrite.png",
                ".json",
            )
        with self.assertRaisesRegex(RuntimeError, "网络路径"):
            window._validate_export_target(
                Path(r"\\server\share\cards.json"),
                ".json",
            )


if __name__ == "__main__":
    unittest.main()
