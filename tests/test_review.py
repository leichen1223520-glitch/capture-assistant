"""在 Qt offscreen 环境验证观点卡片审核窗口的保存与隐私回滚。"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402
from PySide6.QtCore import QEventLoop, QThread, QTimer, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

from app.models import Card  # noqa: E402
from app.capture import save_image as real_save_image  # noqa: E402
from app.review import CardReviewDialog  # noqa: E402
from app.store import Store, StoreError  # noqa: E402


class CardReviewDialogTests(unittest.TestCase):
    application: QApplication

    @classmethod
    def setUpClass(cls) -> None:
        instance = QApplication.instance()
        cls.application = instance if isinstance(instance, QApplication) else QApplication([])

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._temporary.name) / "data"
        self.screenshot_dir = self.data_dir / "screenshots"
        self.screenshot_dir.mkdir(parents=True)
        self.store = Store(self.data_dir / "cards.sqlite3", self.data_dir)
        self.store.init_db()

    def tearDown(self) -> None:
        self.application.processEvents()
        self._temporary.cleanup()

    def _card(self, *, create_images: bool = True) -> Card:
        card = Card(
            text="原始观点",
            text_source="ocr",
            confidence=0.82,
            screenshot_path="screenshots/selection.png",
            full_screenshot_path="screenshots/full.png",
            source_url="https://example.test/watch?v=1",
            source_title="示例标题",
            video_time=15.25,
        )
        if create_images:
            Image.new("RGB", (120, 50), "white").save(
                self.data_dir / card.screenshot_path
            )
            Image.new("RGB", (240, 100), "black").save(
                self.data_dir / card.full_screenshot_path
            )
        self.store.add_draft(card)
        return card

    def _record_exists(self, card_id: str) -> bool:
        with closing(sqlite3.connect(self.store.db_path)) as connection:
            row = connection.execute(
                "SELECT 1 FROM cards WHERE id = ?",
                (card_id,),
            ).fetchone()
        return row is not None

    def _memory_candidate(self) -> tuple[Card, Image.Image, Image.Image]:
        card_id = str(uuid4())
        card = Card(
            id=card_id,
            text="纯内存提取原文",
            text_source="ocr",
            confidence=0.91,
            screenshot_path=f"screenshots/{card_id}.png",
            full_screenshot_path=f"screenshots/full_{card_id}.png",
            source_title="内存候选",
        )
        return (
            card,
            Image.new("RGB", (120, 50), "white"),
            Image.new("RGB", (240, 100), "black"),
        )

    def _dialog(
        self,
        card: Card,
        *,
        selected_image: Image.Image | None = None,
        full_image: Image.Image | None = None,
        **kwargs: object,
    ) -> CardReviewDialog:
        return CardReviewDialog(
            card,
            self.store,
            data_dir=self.data_dir,
            selected_image=selected_image,
            full_image=full_image,
            **kwargs,
        )

    def _wait_until(self, predicate, timeout_ms: int = 3_000) -> None:  # type: ignore[no-untyped-def]
        if predicate():
            return
        loop = QEventLoop()
        poll = QTimer()
        poll.setInterval(5)
        poll.timeout.connect(lambda: loop.quit() if predicate() else None)
        poll.start()
        QTimer.singleShot(timeout_ms, loop.quit)
        loop.exec()
        poll.stop()
        self.assertTrue(predicate(), "等待后台审核操作超时")

    @staticmethod
    def _preview_png(color: str) -> bytes:
        buffer = BytesIO()
        Image.new("RGB", (24, 16), color).save(buffer, format="PNG")
        return buffer.getvalue()

    def test_constructor_rejects_store_data_root_mismatch(self) -> None:
        card, selected_image, full_image = self._memory_candidate()
        other_data = Path(self._temporary.name) / "other-data"

        with self.assertRaisesRegex(ValueError, "Store 数据根目录不一致"):
            CardReviewDialog(
                card,
                self.store,
                data_dir=other_data,
                selected_image=selected_image,
                full_image=full_image,
            )

        selected_image.close()
        full_image.close()

    def test_memory_candidate_rejects_noncanonical_screenshot_names(self) -> None:
        card, selected_image, full_image = self._memory_candidate()
        bad_card = Card(
            **{
                **card.model_dump(),
                "screenshot_path": "screenshots/selection.png",
            }
        )

        with self.assertRaisesRegex(ValueError, "安全文件名规范"):
            self._dialog(
                bad_card,
                selected_image=selected_image,
                full_image=full_image,
            )

        selected_image.close()
        full_image.close()

    def test_preview_png_bytes_avoid_encoding_full_pil_images_on_gui_thread(self) -> None:
        card, selected_image, full_image = self._memory_candidate()
        selected_preview = self._preview_png("green")
        full_preview = self._preview_png("blue")

        with patch.object(
            Image.Image,
            "save",
            side_effect=AssertionError("提供预览后不应在 GUI 线程编码 PIL"),
        ):
            dialog = self._dialog(
                card,
                selected_image=selected_image,
                full_image=full_image,
                selected_preview_png=selected_preview,
                full_preview_png=full_preview,
            )

        self.assertFalse(dialog.screenshot_label.pixmap().isNull())
        self.assertFalse(dialog.full_screenshot_label.pixmap().isNull())
        dialog.reject()
        selected_image.close()
        full_image.close()

    def test_memory_commit_runs_database_and_file_work_off_gui_thread(self) -> None:
        card, selected_image, full_image = self._memory_candidate()
        dialog = self._dialog(
            card,
            selected_image=selected_image,
            full_image=full_image,
        )
        observed_gui_threads: list[bool] = []
        observed_file_gui_threads: list[bool] = []
        original_add_draft = self.store.add_draft

        def observed_add_draft(candidate: Card) -> Card:
            observed_gui_threads.append(
                QThread.currentThread() is self.application.thread()
            )
            return original_add_draft(candidate)

        def observed_save_image(image: Image.Image, path: Path) -> None:
            observed_file_gui_threads.append(
                QThread.currentThread() is self.application.thread()
            )
            real_save_image(image, path)

        with (
            patch.object(self.store, "add_draft", side_effect=observed_add_draft),
            patch("app.review.save_image", side_effect=observed_save_image),
        ):
            QTest.mouseClick(dialog.save_button, Qt.MouseButton.LeftButton)
            self._wait_until(lambda: dialog.finalized)

        self.assertEqual(observed_gui_threads, [False])
        self.assertEqual(observed_file_gui_threads, [False, False])
        self.assertTrue(dialog.saved)
        selected_image.close()
        full_image.close()

    def test_reject_and_close_are_blocked_while_background_save_runs(self) -> None:
        card, selected_image, full_image = self._memory_candidate()
        dialog = self._dialog(
            card,
            selected_image=selected_image,
            full_image=full_image,
        )
        dialog.show()
        started = threading.Event()
        release = threading.Event()
        original_add_draft = self.store.add_draft

        def blocked_add_draft(candidate: Card) -> Card:
            started.set()
            if not release.wait(3):
                raise RuntimeError("测试后台保存等待超时")
            return original_add_draft(candidate)

        try:
            with patch.object(self.store, "add_draft", side_effect=blocked_add_draft):
                QTest.mouseClick(dialog.save_button, Qt.MouseButton.LeftButton)
                self._wait_until(started.is_set)
                self.assertTrue(dialog.saving)
                self.assertFalse(dialog.save_button.isEnabled())
                self.assertFalse(dialog.discard_button.isEnabled())

                QTest.keyClick(dialog, Qt.Key.Key_Escape)
                self.application.processEvents()
                self.assertTrue(dialog.isVisible())
                self.assertFalse(dialog.close())
                self.assertTrue(dialog.isVisible())
                self.assertFalse(dialog.finalized)

                release.set()
                self._wait_until(lambda: dialog.finalized)
        finally:
            release.set()

        self.assertTrue(dialog.saved)
        selected_image.close()
        full_image.close()

    def test_wait_for_save_settles_before_queued_signal_exactly_once(self) -> None:
        card, selected_image, full_image = self._memory_candidate()
        dialog = self._dialog(
            card,
            selected_image=selected_image,
            full_image=full_image,
        )

        QTest.mouseClick(dialog.save_button, Qt.MouseButton.LeftButton)
        worker = dialog._save_worker
        self.assertIsNotNone(worker)
        self.assertTrue(dialog.wait_for_save(3_000))

        assert worker is not None
        self.assertIsInstance(worker.outcome, Card)
        self.assertTrue(dialog.saved)
        finalized = dialog.finalized_card
        self.application.processEvents()  # 已排队的 completed signal 必须成为幂等空操作。
        self.assertIs(dialog.finalized_card, finalized)
        self.assertFalse(dialog.saving)
        selected_image.close()
        full_image.close()

    def test_wait_for_save_failure_does_not_open_blocking_message_box(self) -> None:
        card = self._card()
        dialog = self._dialog(card)

        with (
            patch.object(
                self.store,
                "finalize_draft",
                side_effect=StoreError("模拟后台完成失败"),
            ),
            patch.object(QMessageBox, "critical") as error,
        ):
            QTest.mouseClick(dialog.save_button, Qt.MouseButton.LeftButton)
            self.assertTrue(dialog.wait_for_save(3_000))
            self.application.processEvents()

        error.assert_not_called()
        self.assertFalse(dialog.saved)
        self.assertFalse(dialog.saving)
        dialog.reject()

    def test_external_draft_cleanup_during_save_leaves_no_orphan_files(self) -> None:
        card, selected_image, full_image = self._memory_candidate()
        dialog = self._dialog(
            card,
            selected_image=selected_image,
            full_image=full_image,
        )
        selected_path = self.data_dir / card.screenshot_path
        full_path = self.data_dir / card.full_screenshot_path
        draft_added = threading.Event()
        allow_file_write = threading.Event()
        original_add_draft = self.store.add_draft
        original_save_image = real_save_image
        save_calls = 0

        def observed_add_draft(candidate: Card) -> Card:
            result = original_add_draft(candidate)
            draft_added.set()
            return result

        def blocked_first_save(image: Image.Image, path: Path) -> None:
            nonlocal save_calls
            save_calls += 1
            if save_calls == 1 and not allow_file_write.wait(3):
                raise RuntimeError("测试文件屏障超时")
            original_save_image(image, path)

        try:
            with (
                patch.object(self.store, "add_draft", side_effect=observed_add_draft),
                patch("app.review.save_image", side_effect=blocked_first_save),
                patch.object(QMessageBox, "critical") as error,
            ):
                QTest.mouseClick(dialog.save_button, Qt.MouseButton.LeftButton)
                self._wait_until(draft_added.is_set)
                self.assertTrue(self.store.delete_draft(card.id))
                allow_file_write.set()
                self.assertTrue(dialog.wait_for_save(3_000))
                self.application.processEvents()
        finally:
            allow_file_write.set()

        error.assert_not_called()
        self.assertFalse(self._record_exists(card.id))
        self.assertFalse(selected_path.exists())
        self.assertFalse(full_path.exists())
        self.assertFalse(dialog.pending_disk_draft)
        dialog.reject()
        selected_image.close()
        full_image.close()

    def test_delete_draft_exception_keeps_pending_and_blocks_close(self) -> None:
        card, selected_image, full_image = self._memory_candidate()
        dialog = self._dialog(
            card,
            selected_image=selected_image,
            full_image=full_image,
        )
        dialog.show()
        calls = 0

        def fail_second_save(image: Image.Image, path: Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("模拟保存第二张图片失败")
            real_save_image(image, path)

        with (
            patch("app.review.save_image", side_effect=fail_second_save),
            patch.object(
                self.store,
                "delete_draft",
                side_effect=StoreError("模拟草稿删除状态未知"),
            ),
            patch.object(QMessageBox, "critical") as error,
        ):
            QTest.mouseClick(dialog.save_button, Qt.MouseButton.LeftButton)
            self.assertTrue(dialog.wait_for_save(3_000))
            self.assertTrue(dialog.pending_disk_draft)
            self.assertFalse(dialog.close())
            self.assertTrue(dialog.isVisible())

        self.assertEqual(error.call_count, 1)  # wait 不弹窗，关闭失败才提示。
        self.assertTrue(self._record_exists(card.id))
        dialog.reject()
        self.assertFalse(self._record_exists(card.id))
        selected_image.close()
        full_image.close()

    def test_finalize_commit_then_exception_recovers_matching_formal_card(self) -> None:
        card, selected_image, full_image = self._memory_candidate()
        dialog = self._dialog(
            card,
            selected_image=selected_image,
            full_image=full_image,
        )
        selected_path = self.data_dir / card.screenshot_path
        full_path = self.data_dir / card.full_screenshot_path
        original_finalize = self.store.finalize_draft

        def commit_then_raise(card_id: str, **changes: object) -> Card:
            original_finalize(card_id, **changes)
            raise StoreError("模拟提交成功后的返回异常")

        with (
            patch.object(self.store, "finalize_draft", side_effect=commit_then_raise),
            patch.object(QMessageBox, "critical") as error,
        ):
            QTest.mouseClick(dialog.save_button, Qt.MouseButton.LeftButton)
            self.assertTrue(dialog.wait_for_save(3_000))
            self.application.processEvents()

        error.assert_not_called()
        self.assertTrue(dialog.saved)
        self.assertTrue(dialog.finalized)
        self.assertFalse(dialog.pending_disk_draft)
        self.assertIsNotNone(self.store.get_card(card.id))
        self.assertTrue(selected_path.is_file())
        self.assertTrue(full_path.is_file())
        self.assertTrue(self.store.delete_card(card.id))
        selected_image.close()
        full_image.close()

    def test_finalize_exception_does_not_accept_different_formal_card(self) -> None:
        card, selected_image, full_image = self._memory_candidate()
        dialog = self._dialog(
            card,
            selected_image=selected_image,
            full_image=full_image,
        )
        selected_path = self.data_dir / card.screenshot_path
        full_path = self.data_dir / card.full_screenshot_path
        original_finalize = self.store.finalize_draft

        def commit_different_then_raise(card_id: str, **changes: object) -> Card:
            original_finalize(card_id, **changes)
            self.store.update_card(card_id, note="不同的正式内容")
            raise StoreError("模拟 UUID 冲突")

        with patch.object(
            self.store,
            "finalize_draft",
            side_effect=commit_different_then_raise,
        ):
            QTest.mouseClick(dialog.save_button, Qt.MouseButton.LeftButton)
            self.assertTrue(dialog.wait_for_save(3_000))

        self.assertFalse(dialog.saved)
        self.assertTrue(dialog.pending_disk_draft)
        stored = self.store.get_card(card.id)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.note, "不同的正式内容")
        self.assertTrue(selected_path.is_file())
        self.assertTrue(full_path.is_file())
        self.assertTrue(self.store.delete_card(card.id))
        selected_image.close()
        full_image.close()

    def test_save_updates_card_and_exposes_final_state(self) -> None:
        card = self._card()
        dialog = self._dialog(card)
        self.assertTrue(dialog.stance_buttons["unknown"].isChecked())
        self.assertFalse(dialog.screenshot_label.pixmap().isNull())
        self.assertFalse(dialog.full_screenshot_label.pixmap().isNull())
        self.assertIn("保存时会一并保留", dialog.full_screenshot_notice.text())

        dialog.text_edit.setPlainText("编辑后的观点")
        dialog.stance_buttons["agree"].setChecked(True)
        dialog.note_edit.setPlainText("这条值得继续研究")
        QTest.mouseClick(dialog.save_button, Qt.MouseButton.LeftButton)
        self._wait_until(lambda: dialog.finalized)

        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertTrue(dialog.saved)
        self.assertIsNotNone(dialog.finalized_card)
        stored = self.store.get_card(card.id)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.text, "原始观点")
        self.assertEqual(stored.edited_text, "编辑后的观点")
        self.assertEqual(stored.stance, "agree")
        self.assertEqual(stored.note, "这条值得继续研究")
        self.assertEqual(self.store.search("编辑后的观点")[0].id, card.id)
        self.assertTrue((self.data_dir / card.screenshot_path).exists())

    def test_memory_candidate_is_not_persisted_until_save(self) -> None:
        card, selected_image, full_image = self._memory_candidate()
        selected_path = self.data_dir / card.screenshot_path
        full_path = self.data_dir / card.full_screenshot_path
        dialog = self._dialog(
            card,
            selected_image=selected_image,
            full_image=full_image,
        )

        self.assertFalse(self._record_exists(card.id))
        self.assertFalse(selected_path.exists())
        self.assertFalse(full_path.exists())
        self.assertTrue(dialog.captured_text_view.isReadOnly())
        self.assertEqual(dialog.captured_text_view.toPlainText(), card.text)
        self.assertFalse(dialog.screenshot_label.pixmap().isNull())
        self.assertFalse(dialog.full_screenshot_label.pixmap().isNull())

        dialog.text_edit.setPlainText("人工整理后的文字")
        dialog.stance_buttons["doubt"].setChecked(True)
        dialog.note_edit.setPlainText("需要核对来源")
        QTest.mouseClick(dialog.save_button, Qt.MouseButton.LeftButton)
        self._wait_until(lambda: dialog.finalized)

        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        stored = self.store.get_card(card.id)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.text, "纯内存提取原文")
        self.assertEqual(stored.edited_text, "人工整理后的文字")
        self.assertEqual(stored.stance, "doubt")
        self.assertEqual(stored.note, "需要核对来源")
        self.assertTrue(selected_path.is_file())
        self.assertTrue(full_path.is_file())
        self.assertEqual(self.store.search("纯内存提取原文")[0].id, card.id)
        self.assertEqual(self.store.search("人工整理后的文字")[0].id, card.id)
        selected_image.close()
        full_image.close()

    def test_discarding_memory_candidate_never_writes_disk_or_database(self) -> None:
        card, selected_image, full_image = self._memory_candidate()
        dialog = self._dialog(
            card,
            selected_image=selected_image,
            full_image=full_image,
        )

        QTest.mouseClick(dialog.discard_button, Qt.MouseButton.LeftButton)

        self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
        self.assertTrue(dialog.finalized)
        self.assertTrue(dialog.discarded)
        self.assertFalse(self._record_exists(card.id))
        self.assertFalse((self.data_dir / card.screenshot_path).exists())
        self.assertFalse((self.data_dir / card.full_screenshot_path).exists())
        selected_image.close()
        full_image.close()

    def test_memory_save_failure_rolls_back_first_image_and_database(self) -> None:
        card, selected_image, full_image = self._memory_candidate()
        selected_path = self.data_dir / card.screenshot_path
        full_path = self.data_dir / card.full_screenshot_path
        dialog = self._dialog(
            card,
            selected_image=selected_image,
            full_image=full_image,
        )
        dialog.show()
        call_count = 0

        def fail_on_second_image(image: Image.Image, path: Path) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise OSError("模拟第二张截图写入失败")
            real_save_image(image, path)

        with (
            patch("app.review.save_image", side_effect=fail_on_second_image),
            patch.object(
                QMessageBox,
                "critical",
                return_value=QMessageBox.StandardButton.Ok,
            ) as error,
        ):
            QTest.mouseClick(dialog.save_button, Qt.MouseButton.LeftButton)
            self._wait_until(lambda: not dialog.saving)

        self.assertTrue(dialog.isVisible())
        self.assertFalse(dialog.saved)
        self.assertFalse(self._record_exists(card.id))
        self.assertFalse(selected_path.exists())
        self.assertFalse(full_path.exists())
        self.assertTrue(dialog.save_button.isEnabled())
        self.assertTrue(dialog.discard_button.isEnabled())
        error.assert_called_once()
        QTest.mouseClick(dialog.save_button, Qt.MouseButton.LeftButton)
        self._wait_until(lambda: dialog.finalized)
        self.assertTrue(dialog.saved)
        self.assertTrue(selected_path.is_file())
        self.assertTrue(full_path.is_file())
        selected_image.close()
        full_image.close()

    def test_discard_button_deletes_record_fts_and_both_images(self) -> None:
        card = self._card()
        dialog = self._dialog(card)

        QTest.mouseClick(dialog.discard_button, Qt.MouseButton.LeftButton)

        self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
        self.assertFalse(dialog.saved)
        self.assertIsNone(dialog.finalized_card)
        self.assertIsNone(self.store.get_card(card.id))
        self.assertEqual(self.store.search("原始观点"), [])
        self.assertFalse((self.data_dir / card.screenshot_path).exists())
        self.assertFalse((self.data_dir / card.full_screenshot_path).exists())

    def test_escape_deletes_candidate(self) -> None:
        card = self._card()
        dialog = self._dialog(card)
        dialog.show()

        QTest.keyClick(dialog, Qt.Key.Key_Escape)
        self.application.processEvents()

        self.assertFalse(dialog.isVisible())
        self.assertIsNone(self.store.get_card(card.id))

    def test_window_close_deletes_candidate(self) -> None:
        card = self._card()
        dialog = self._dialog(card)
        dialog.show()

        self.assertTrue(dialog.close())
        self.application.processEvents()

        self.assertFalse(dialog.isVisible())
        self.assertIsNone(self.store.get_card(card.id))
        self.assertFalse((self.data_dir / card.screenshot_path).exists())
        self.assertFalse((self.data_dir / card.full_screenshot_path).exists())

    def test_finalize_failure_shows_error_and_keeps_dialog_open(self) -> None:
        card = self._card()
        dialog = self._dialog(card)
        dialog.show()

        with (
            patch.object(
                self.store,
                "finalize_draft",
                side_effect=StoreError("模拟写入失败"),
            ),
            patch.object(QMessageBox, "critical", return_value=QMessageBox.StandardButton.Ok) as error,
        ):
            QTest.mouseClick(dialog.save_button, Qt.MouseButton.LeftButton)
            self._wait_until(lambda: not dialog.saving)

        self.assertTrue(dialog.isVisible())
        self.assertFalse(dialog.saved)
        self.assertIsNone(dialog.finalized_card)
        self.assertTrue(self._record_exists(card.id))
        error.assert_called_once()
        dialog.reject()

    def test_delete_failure_blocks_escape_and_close(self) -> None:
        card = self._card()
        dialog = self._dialog(card)
        dialog.show()

        with (
            patch.object(
                self.store,
                "delete_draft",
                side_effect=StoreError("模拟删除失败"),
            ),
            patch.object(QMessageBox, "critical", return_value=QMessageBox.StandardButton.Ok) as error,
        ):
            QTest.keyClick(dialog, Qt.Key.Key_Escape)
            self.application.processEvents()
            self.assertTrue(dialog.isVisible())
            self.assertFalse(dialog.saved)
            self.assertTrue(self._record_exists(card.id))

            self.assertFalse(dialog.close())
            self.application.processEvents()
            self.assertTrue(dialog.isVisible())
            self.assertTrue(self._record_exists(card.id))
            self.assertEqual(error.call_count, 2)

        self.assertTrue(dialog.close())

    def test_missing_screenshot_uses_placeholder(self) -> None:
        card = self._card(create_images=False)
        dialog = self._dialog(card)

        self.assertTrue(dialog.screenshot_label.pixmap().isNull())
        self.assertIn("不存在", dialog.screenshot_label.text())
        self.assertTrue(dialog.full_screenshot_label.pixmap().isNull())
        self.assertIn("完整冻结画面", dialog.full_screenshot_notice.text())
        dialog.reject()

    def test_direct_done_rejected_also_cleans_draft_and_images(self) -> None:
        card = self._card()
        dialog = self._dialog(card)
        selected = self.data_dir / card.screenshot_path
        full = self.data_dir / card.full_screenshot_path

        dialog.done(int(QDialog.DialogCode.Rejected))

        self.assertTrue(dialog.finalized)
        self.assertTrue(dialog.discarded)
        self.assertFalse(dialog.saved)
        self.assertFalse(self._record_exists(card.id))
        self.assertFalse(selected.exists())
        self.assertFalse(full.exists())

    def test_unsafe_screenshot_path_is_never_loaded(self) -> None:
        outside = Path(self._temporary.name) / "outside.png"
        Image.new("RGB", (30, 30), "red").save(outside)
        safe_card = self._card(create_images=False)
        unsafe_card = Card.model_construct(
            **{
                **safe_card.model_dump(),
                "screenshot_path": "../outside.png",
            }
        )

        dialog = self._dialog(unsafe_card)

        self.assertTrue(dialog.screenshot_label.pixmap().isNull())
        self.assertIn("路径不安全", dialog.screenshot_label.text())
        # Store 中仍是经过模型验证的安全路径，丢弃只会处理受控目录内的目标。
        dialog.reject()
        self.assertTrue(outside.exists())


if __name__ == "__main__":
    unittest.main()
