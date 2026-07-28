"""半自动观察候选的纯内存审核收件箱窗口。"""

from __future__ import annotations

import logging
from pathlib import Path
import time
from typing import Callable

from PySide6.QtCore import QByteArray, Qt, Signal, Slot
from PySide6.QtGui import QCloseEvent, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from .config import DATA_DIR
from .inbox import CandidateInbox, CandidateSummary
from .pipeline import PreparedCard
from .review import CardReviewDialog
from .store import Store

LOGGER = logging.getLogger(__name__)
ReviewFactory = Callable[..., CardReviewDialog]


class CandidateInboxWindow(QMainWindow):
    """列出未落盘候选，并逐条复用正式卡片审核窗口。"""

    count_changed = Signal(int)

    def __init__(
        self,
        inbox: CandidateInbox,
        store: Store,
        *,
        data_dir: str | Path = DATA_DIR,
        review_factory: ReviewFactory = CardReviewDialog,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.inbox = inbox
        self.store = store
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.review_factory = review_factory
        self._summaries: dict[str, CandidateSummary] = {}
        self._active_dialog: CardReviewDialog | None = None
        self._active_prepared: PreparedCard | None = None
        self._shutting_down = False

        self.setWindowTitle("半自动观察候选收件箱")
        self.setMinimumSize(900, 620)

        self.list_widget = QListWidget()
        self.list_widget.setObjectName("candidateList")
        self.list_widget.currentItemChanged.connect(self._show_selected)

        self.text_label = QLabel("尚未选择候选")
        self.text_label.setObjectName("candidateText")
        self.text_label.setTextFormat(Qt.TextFormat.PlainText)
        self.text_label.setWordWrap(True)
        self.text_label.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.meta_label = QLabel("")
        self.meta_label.setTextFormat(Qt.TextFormat.PlainText)
        self.meta_label.setWordWrap(True)

        self.selected_preview = self._preview_label("candidateSelectedPreview")
        self.full_preview = self._preview_label("candidateFullPreview")
        preview_row = QHBoxLayout()
        preview_row.addWidget(self.selected_preview)
        preview_row.addWidget(self.full_preview)

        self.review_button = QPushButton("审核此条")
        self.review_button.setObjectName("reviewCandidate")
        self.discard_button = QPushButton("丢弃选中")
        self.discard_button.setObjectName("discardCandidate")
        self.discard_all_button = QPushButton("全部丢弃")
        self.discard_all_button.setObjectName("discardAllCandidates")
        button_row = QHBoxLayout()
        button_row.addWidget(self.review_button)
        button_row.addWidget(self.discard_button)
        button_row.addStretch(1)
        button_row.addWidget(self.discard_all_button)

        detail = QWidget()
        detail_layout = QVBoxLayout(detail)
        detail_layout.addWidget(QLabel("候选原文（不代表你的态度）"))
        detail_layout.addWidget(self.text_label, 1)
        detail_layout.addWidget(self.meta_label)
        detail_layout.addLayout(preview_row, 2)
        detail_layout.addLayout(button_row)

        splitter = QSplitter()
        splitter.addWidget(self.list_widget)
        splitter.addWidget(detail)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        self.setCentralWidget(splitter)

        self.review_button.clicked.connect(self._review_selected)
        self.discard_button.clicked.connect(self._discard_selected)
        self.discard_all_button.clicked.connect(self._discard_all)
        self.refresh()

    @staticmethod
    def _preview_label(name: str) -> QLabel:
        label = QLabel("无预览")
        label.setObjectName(name)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setMinimumSize(240, 170)
        label.setStyleSheet("QLabel { background: #202124; color: #dddddd; }")
        return label

    def _reap_completed_review(self) -> None:
        dialog = self._active_dialog
        prepared = self._active_prepared
        if (
            dialog is not None
            and prepared is not None
            and not dialog.saving
            and dialog.finalized
        ):
            prepared.close()
            self._active_prepared = None
            self._active_dialog = None

    @property
    def busy(self) -> bool:
        """有候选正在审核或保存时阻止应用退出。"""

        self._reap_completed_review()
        return self._active_dialog is not None or self._active_prepared is not None

    @Slot()
    def refresh(self) -> None:
        selected_item = self.list_widget.currentItem()
        selected_id = (
            selected_item.data(Qt.ItemDataRole.UserRole)
            if selected_item is not None
            else None
        )
        summaries = self.inbox.snapshot()
        self._summaries = {summary.entry_id: summary for summary in summaries}
        self.list_widget.clear()
        selected_row = -1
        for row, summary in enumerate(summaries):
            text = " ".join(summary.text.split())
            if len(text) > 70:
                text = f"{text[:70]}…"
            prefix = "原生文字" if summary.text_source == "dom" else "离线 OCR"
            item = QListWidgetItem(f"[{prefix}] {text}")
            item.setData(Qt.ItemDataRole.UserRole, summary.entry_id)
            item.setToolTip(summary.source_title or "未知来源")
            self.list_widget.addItem(item)
            if summary.entry_id == selected_id:
                selected_row = row
        if self.list_widget.count() > 0:
            self.list_widget.setCurrentRow(selected_row if selected_row >= 0 else 0)
        else:
            self._clear_detail()
        count = len(summaries)
        self.count_changed.emit(count)
        self.discard_all_button.setEnabled(count > 0)

    @Slot(QListWidgetItem, QListWidgetItem)
    def _show_selected(
        self,
        current: QListWidgetItem | None,
        previous: QListWidgetItem | None,
    ) -> None:
        del previous
        if current is None:
            self._clear_detail()
            return
        entry_id = current.data(Qt.ItemDataRole.UserRole)
        summary = self._summaries.get(entry_id)
        if summary is None:
            self._clear_detail()
            return
        self.text_label.setText(summary.text)
        source = summary.source_title or "未知页面"
        time_text = (
            f"{summary.video_time:.1f} 秒"
            if summary.video_time is not None
            else "无视频时间码"
        )
        self.meta_label.setText(
            f"来源：{source}\n时间：{time_text}\n"
            f"置信度：{summary.confidence:.0%} · 出现 {summary.occurrences} 次\n"
            "状态：只在内存，尚未保存"
        )
        self._set_preview(self.selected_preview, summary.selected_preview_png)
        self._set_preview(self.full_preview, summary.full_preview_png)
        self.review_button.setEnabled(True)
        self.discard_button.setEnabled(True)

    @staticmethod
    def _set_preview(label: QLabel, payload: bytes) -> None:
        pixmap = QPixmap()
        if payload and pixmap.loadFromData(QByteArray(payload), "PNG"):
            label.setPixmap(
                pixmap.scaled(
                    label.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
            label.setText("")
        else:
            label.setPixmap(QPixmap())
            label.setText("无预览")

    def _clear_detail(self) -> None:
        self.text_label.setText("收件箱为空")
        self.meta_label.setText("候选只有经过人工审核并点击保存后才会落盘。")
        for label in (self.selected_preview, self.full_preview):
            label.setPixmap(QPixmap())
            label.setText("无预览")
        self.review_button.setEnabled(False)
        self.discard_button.setEnabled(False)

    def _current_summary(self) -> CandidateSummary | None:
        item = self.list_widget.currentItem()
        if item is None:
            return None
        return self._summaries.get(item.data(Qt.ItemDataRole.UserRole))

    @Slot()
    def _review_selected(self) -> None:
        if self.busy:
            QMessageBox.warning(
                self,
                "仍在安全保存",
                "上一条候选仍在后台保存，请等待保存完成后再审核下一条。",
            )
            return
        summary = self._current_summary()
        if summary is None:
            return
        prepared = self.inbox.take(summary.entry_id)
        if prepared is None:
            self.refresh()
            return
        self._active_prepared = prepared
        self.refresh()
        dialog: CardReviewDialog | None = None
        preserve_for_save = False
        try:
            dialog = self.review_factory(
                prepared.card,
                self.store,
                data_dir=self.data_dir,
                selected_image=prepared.selected_image,
                full_image=prepared.full_image,
                selected_preview_png=prepared.selected_preview_png or None,
                full_preview_png=prepared.full_preview_png or None,
                parent=self,
            )
            self._active_dialog = dialog
            dialog.exec()
        except Exception:
            LOGGER.exception("候选审核窗口失败（未记录候选正文）")
            if dialog is not None and dialog.saving:
                if not dialog.wait_for_save(10_000):
                    preserve_for_save = True
                    QMessageBox.warning(
                        self,
                        "仍在安全保存",
                        "保存线程尚未结束；程序会继续持有候选，暂时不能退出。",
                    )
            if not preserve_for_save:
                safely_requeued = False
                already_saved = False
                cleanup_certain = False
                try:
                    already_saved = self.store.get_card(prepared.card.id) is not None
                    if not already_saved:
                        # 只删除仍为 draft 的记录；正式卡片绝不会被此处删除。
                        self.store.delete_draft(prepared.card.id)
                    cleanup_certain = True
                except Exception:
                    LOGGER.exception("无法确认候选审核异常后的草稿状态")
                if cleanup_certain and not already_saved and not prepared.is_closed:
                    try:
                        result = self.inbox.offer(
                            prepared,
                            session_id=summary.session_id,
                            source_key=summary.source_key,
                            region_key=summary.region_key,
                            now=max(summary.last_seen, time.monotonic()),
                        )
                        safely_requeued = result.accepted
                    except Exception:
                        LOGGER.exception("无法把审核失败候选安全放回内存收件箱")
                if not safely_requeued:
                    prepared.close()
                QMessageBox.warning(
                    self,
                    "审核失败",
                    (
                        "候选已安全放回内存收件箱，请稍后重试。"
                        if safely_requeued
                        else "无法安全恢复此候选；已释放内存，请检查终端。"
                    ),
                )
        else:
            # 正常情况下保存期间对话框不能关闭；但 Windows 会话结束或 Qt
            # 全局退出可能让嵌套事件循环提前返回。保存线程未归并时继续持有
            # PreparedCard，绝不能与写 PNG 的线程并发 close。
            if dialog.saving and not dialog.wait_for_save(10_000):
                preserve_for_save = True
                LOGGER.warning("审核事件循环结束后候选仍在后台保存，继续持有内存证据")
            else:
                saved = dialog.saved
                prepared.close()
                if saved:
                    try:
                        QMessageBox.information(
                            self,
                            "卡片已保存",
                            "观点卡片已保存到本机资料库。",
                        )
                    except Exception:
                        LOGGER.exception("无法显示候选保存结果通知")
        finally:
            if not preserve_for_save:
                self._active_prepared = None
                self._active_dialog = None
            self.refresh()

    @Slot()
    def _discard_selected(self) -> None:
        summary = self._current_summary()
        if summary is not None:
            self.inbox.discard(summary.entry_id)
            self.refresh()

    @Slot()
    def _discard_all(self) -> None:
        if len(self.inbox) == 0:
            return
        answer = QMessageBox.question(
            self,
            "丢弃全部候选",
            "这些候选尚未落盘。确定要从内存中全部丢弃吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.inbox.discard_all()
            self.refresh()

    def shutdown(self) -> bool:
        """结束活动审核；保存未能归并时返回 ``False`` 且不释放其图像。"""

        self._shutting_down = True
        self._reap_completed_review()
        dialog = self._active_dialog
        prepared = self._active_prepared
        if dialog is not None:
            if dialog.saving and not dialog.wait_for_save(10_000):
                LOGGER.warning("退出前未能在限定时间内结束候选保存")
                return False
            if not dialog.finalized:
                dialog.reject()
            if not dialog.finalized:
                return False
        if prepared is not None:
            prepared.close()
        self._active_prepared = None
        self._active_dialog = None
        self.hide()
        return True

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self._shutting_down:
            event.accept()
        else:
            event.ignore()
            self.hide()


__all__ = ["CandidateInboxWindow"]
