"""候选收件箱审核窗口的内存所有权与退出安全测试。"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.inbox import CandidateInbox  # noqa: E402
from app.inbox_window import CandidateInboxWindow  # noqa: E402
from app.models import Card  # noqa: E402
from app.pipeline import PreparedCard  # noqa: E402
from app.store import Store  # noqa: E402


class _FakeReviewDialog:
    def __init__(self, *, raise_from_exec: bool = False) -> None:
        self.raise_from_exec = raise_from_exec
        self.saving = True
        self.saved = False
        self.finalized = False
        self.wait_result = False
        self.wait_calls: list[int] = []

    def exec(self) -> int:
        if self.raise_from_exec:
            raise RuntimeError("测试审核事件循环故障")
        return 0

    def wait_for_save(self, timeout_ms: int) -> bool:
        self.wait_calls.append(timeout_ms)
        return self.wait_result

    def reject(self) -> None:
        if not self.saving:
            self.finalized = True


class CandidateInboxWindowTests(unittest.TestCase):
    application: QApplication

    @classmethod
    def setUpClass(cls) -> None:
        instance = QApplication.instance()
        cls.application = instance if isinstance(instance, QApplication) else QApplication([])

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temporary.name) / "data"
        self.store = Store(self.data_dir / "cards.sqlite3", self.data_dir)
        self.store.init_db()
        self.inbox = CandidateInbox()
        self.prepared = self._candidate()
        self.inbox.offer(
            self.prepared,
            session_id="session-one",
            source_key="a" * 64,
            region_key="1:0:0:5:3",
            now=1.0,
        )
        self.dialog = _FakeReviewDialog()
        self.window = CandidateInboxWindow(
            self.inbox,
            self.store,
            data_dir=self.data_dir,
            review_factory=lambda *_args, **_kwargs: self.dialog,  # type: ignore[arg-type]
        )

    def tearDown(self) -> None:
        self.dialog.saving = False
        self.dialog.finalized = True
        self.window.shutdown()
        self.inbox.close()
        self.application.processEvents()
        self.temporary.cleanup()

    @staticmethod
    def _candidate() -> PreparedCard:
        card_id = str(uuid4())
        return PreparedCard(
            card=Card(
                id=card_id,
                text="保存中的内存候选",
                text_source="dom",
                confidence=0.99,
                screenshot_path=f"screenshots/{card_id}.png",
                full_screenshot_path=f"screenshots/full_{card_id}.png",
                stance="unknown",
            ),
            selected_image=Image.new("RGB", (5, 3), "white"),
            full_image=Image.new("RGB", (20, 12), "black"),
        )

    def _review(self) -> None:
        with (
            patch("app.inbox_window.QMessageBox.warning"),
            patch("app.inbox_window.QMessageBox.information"),
        ):
            self.window._review_selected()

    def test_event_loop_return_during_save_preserves_candidate_until_finalized(
        self,
    ) -> None:
        self._review()

        self.assertEqual(len(self.inbox), 0)
        self.assertTrue(self.window.busy)
        self.assertFalse(self.prepared.is_closed)
        self.assertEqual(self.dialog.wait_calls, [10_000])
        self.assertFalse(self.window.shutdown())
        self.assertFalse(self.prepared.is_closed)

        self.dialog.saving = False
        self.dialog.saved = True
        self.dialog.finalized = True

        self.assertFalse(self.window.busy)
        self.assertTrue(self.prepared.is_closed)
        self.assertTrue(self.window.shutdown())

    def test_exception_during_save_timeout_never_requeues_or_closes_candidate(
        self,
    ) -> None:
        self.dialog.raise_from_exec = True

        self._review()

        self.assertEqual(len(self.inbox), 0)
        self.assertTrue(self.window.busy)
        self.assertFalse(self.prepared.is_closed)
        self.assertIs(self.window._active_prepared, self.prepared)
        self.assertIs(self.window._active_dialog, self.dialog)

        self.dialog.saving = False
        self.dialog.finalized = True
        self.assertFalse(self.window.busy)
        self.assertTrue(self.prepared.is_closed)

    def test_save_timeout_blocks_second_review_until_first_is_finalized(self) -> None:
        second = self._candidate()
        result = self.inbox.offer(
            second,
            session_id="session-one",
            source_key="b" * 64,
            region_key="1:0:0:5:3",
            now=2.0,
        )
        self.assertTrue(result.accepted)
        self._review()
        self.assertTrue(self.window.busy)
        self.assertEqual(len(self.inbox), 1)

        self.window.list_widget.setCurrentRow(0)
        with patch("app.inbox_window.QMessageBox.warning") as warning:
            self.window._review_selected()

        warning.assert_called_once()
        self.assertIs(self.window._active_prepared, self.prepared)
        self.assertEqual(len(self.inbox), 1)
        self.assertFalse(second.is_closed)


if __name__ == "__main__":
    unittest.main()
