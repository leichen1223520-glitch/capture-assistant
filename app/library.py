"""面向普通用户的本地观点库桌面窗口。

窗口只通过 :class:`app.store.Store` 读写已经审核保存的卡片；原始提取文字和
证据截图始终只读。搜索、更新、删除和导出在有界后台任务中执行，模块导入本身
不会创建 QApplication、访问数据库、读取截图或写入文件。
"""

from __future__ import annotations

import html
import os
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from threading import Event, Lock
from time import monotonic
from typing import Final, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from PySide6.QtCore import QObject, QRunnable, QSize, QThreadPool, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QCloseEvent, QDesktopServices, QImageReader, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .models import Card, Stance
from .store import Store

STANCE_LABELS: Final[dict[Stance, str]] = {
    "unknown": "暂不判断",
    "agree": "认同",
    "disagree": "反对",
    "doubt": "存疑",
    "useful": "只是有用",
}
SOURCE_LABELS: Final[dict[str, str]] = {
    "dom": "网页选中文字",
    "ocr": "画面 OCR",
}
MAX_VISIBLE_CARDS: Final = 200
MAX_SCREENSHOT_FILE_BYTES: Final = 32 * 1024 * 1024
MAX_SCREENSHOT_PIXELS: Final = 25_000_000
_PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"
_UNSET: Final = object()

TaskKind = Literal["load", "update", "delete", "export"]


class _WorkerSignals(QObject):
    completed = Signal(object)


class _LibraryWorker(QRunnable):
    """在后台执行一个不接触 GUI 对象的有界操作。"""

    def __init__(self, operation: Callable[[], object]) -> None:
        super().__init__()
        self.signals = _WorkerSignals()
        self._operation: Callable[[], object] | None = operation
        self._completed = Event()
        self._lock = Lock()
        self._outcome: object = _UNSET
        self.setAutoDelete(False)

    @Slot()
    def run(self) -> None:
        operation = self._operation
        if operation is None:
            return
        try:
            outcome: object = operation()
        except Exception as exc:
            outcome = exc
        with self._lock:
            self._outcome = outcome
        self._completed.set()
        self.signals.completed.emit(self)

    @property
    def outcome(self) -> object:
        with self._lock:
            return self._outcome

    def wait(self, timeout_ms: int) -> bool:
        timeout = None if timeout_ms < 0 else timeout_ms / 1000.0
        return self._completed.wait(timeout)

    def release(self) -> None:
        self._operation = None


def _is_descendant(candidate: Path, root: Path) -> bool:
    """判断 ``candidate`` 是否严格位于 ``root`` 内。"""

    return candidate != root and root in candidate.parents


def _controlled_screenshot_file(data_dir: Path, relative_path: str) -> Path | None:
    """只返回数据根的 ``screenshots`` 内现存普通文件。"""

    try:
        root = data_dir.expanduser().resolve()
        screenshot_root = (root / "screenshots").resolve()
        candidate = (root / relative_path).resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    if (
        not _is_descendant(screenshot_root, root)
        or not _is_descendant(candidate, screenshot_root)
    ):
        return None
    try:
        if candidate.suffix.casefold() != ".png" or not candidate.is_file():
            return None
        if candidate.stat().st_size > MAX_SCREENSHOT_FILE_BYTES:
            return None
        with candidate.open("rb") as stream:
            if stream.read(len(_PNG_SIGNATURE)) != _PNG_SIGNATURE:
                return None
    except OSError:
        return None
    return candidate


def _safe_http_url(value: str | None) -> QUrl | None:
    """把 HTTP(S) 来源解析成可主动打开的 URL，拒绝本地和脚本协议。"""

    if not value or any(ord(character) < 32 or ord(character) == 127 for character in value):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return None
    url = QUrl(value)
    return url if url.isValid() else None


def _atomic_write_text(target: Path, content: str) -> Path:
    """在目标目录创建临时文件后原子替换用户选择的导出文件。"""

    parent = target.parent
    if not parent.is_dir():
        raise RuntimeError("导出目录不存在，请重新选择保存位置。")
    if target.exists() and target.is_dir():
        raise RuntimeError("导出目标是文件夹，请选择一个文件名。")
    temporary = parent / f".{target.name}.{uuid4().hex}.tmp"
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise RuntimeError("无法写入所选导出文件。") from exc
    return target


class LibraryWindow(QMainWindow):
    """搜索、审核和导出本地观点卡片的可复用主窗口。"""

    def __init__(
        self,
        store: Store,
        *,
        data_dir: str | Path | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.store = store
        selected_root = store.data_dir if data_dir is None else Path(data_dir)
        self.data_dir = selected_root.expanduser().resolve()
        store_root = store.data_dir.expanduser().resolve()
        if store_root != self.data_dir:
            raise ValueError("观点库数据目录与 Store 数据根目录不一致")
        database_path = store.db_path.expanduser().resolve()
        if not _is_descendant(database_path, self.data_dir):
            raise ValueError("观点库数据库必须位于受控数据目录内")

        self._thread_pool = QThreadPool.globalInstance()
        self._worker: _LibraryWorker | None = None
        self._task_kind: TaskKind | None = None
        self._task_context: dict[str, object] = {}
        self._busy = False
        self._cards: list[Card] = []
        self._cards_by_id: dict[str, Card] = {}
        self._current_card: Card | None = None
        self._changing_selection = False

        self.setObjectName("libraryWindow")
        self.setWindowTitle("我的观点库")
        self.resize(1120, 780)
        self.setMinimumSize(900, 640)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        central = QWidget()
        outer = QVBoxLayout(central)

        search_row = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setObjectName("librarySearch")
        self.search_edit.setPlaceholderText("搜索原文、整理文字、来源标题或备注")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setMaxLength(500)
        self.search_edit.returnPressed.connect(self.request_refresh)
        search_row.addWidget(self.search_edit, 1)

        self.stance_filter = QComboBox()
        self.stance_filter.setObjectName("stanceFilter")
        self.stance_filter.addItem("全部态度", None)
        for stance, label in STANCE_LABELS.items():
            self.stance_filter.addItem(label, stance)
        search_row.addWidget(self.stance_filter)

        self.source_filter = QComboBox()
        self.source_filter.setObjectName("sourceFilter")
        self.source_filter.addItem("全部文字来源", None)
        for source, label in SOURCE_LABELS.items():
            self.source_filter.addItem(label, source)
        search_row.addWidget(self.source_filter)

        self.search_button = QPushButton("搜索")
        self.search_button.setObjectName("searchCards")
        self.search_button.clicked.connect(self.request_refresh)
        search_row.addWidget(self.search_button)
        self.clear_button = QPushButton("清除条件")
        self.clear_button.setObjectName("clearFilters")
        self.clear_button.clicked.connect(self._clear_filters)
        search_row.addWidget(self.clear_button)
        self.refresh_button = QPushButton("刷新")
        self.refresh_button.setObjectName("refreshCards")
        self.refresh_button.clicked.connect(self.request_refresh)
        search_row.addWidget(self.refresh_button)
        outer.addLayout(search_row)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.card_list = QListWidget()
        self.card_list.setObjectName("cardList")
        self.card_list.setMinimumWidth(300)
        self.card_list.setAlternatingRowColors(True)
        self.card_list.currentItemChanged.connect(self._on_current_item_changed)
        splitter.addWidget(self.card_list)

        detail_scroll = QScrollArea()
        detail_scroll.setWidgetResizable(True)
        detail_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        detail = QWidget()
        detail_layout = QVBoxLayout(detail)

        self.screenshot_tabs = QTabWidget()
        self.screenshot_tabs.setObjectName("screenshotTabs")
        self.selected_screenshot = self._new_image_label("selectedScreenshot")
        self.full_screenshot = self._new_image_label("fullScreenshot")
        self.screenshot_tabs.addTab(self.selected_screenshot, "选区截图")
        self.screenshot_tabs.addTab(self.full_screenshot, "完整画面")
        self.screenshot_tabs.setMinimumHeight(260)
        detail_layout.addWidget(self.screenshot_tabs)

        source_box = QGroupBox("来源信息（只读）")
        source_form = QFormLayout(source_box)
        self.source_title_label = self._plain_label("尚未选择卡片")
        self.source_url_label = self._plain_label("—")
        self.created_at_label = self._plain_label("—")
        self.video_time_label = self._plain_label("—")
        self.confidence_label = self._plain_label("—")
        self.text_source_label = self._plain_label("—")
        source_form.addRow("标题：", self.source_title_label)
        source_form.addRow("网址：", self.source_url_label)
        source_form.addRow("采集时间：", self.created_at_label)
        source_form.addRow("视频位置：", self.video_time_label)
        source_form.addRow("文字来源：", self.text_source_label)
        source_form.addRow("置信度：", self.confidence_label)
        self.open_source_button = QPushButton("在浏览器中打开来源")
        self.open_source_button.setObjectName("openSource")
        self.open_source_button.clicked.connect(self._open_source)
        source_form.addRow("", self.open_source_button)
        detail_layout.addWidget(source_box)

        detail_layout.addWidget(QLabel("初始提取原文（证据，只读）"))
        self.original_text = QPlainTextEdit()
        self.original_text.setObjectName("libraryOriginalText")
        self.original_text.setReadOnly(True)
        self.original_text.setMaximumHeight(130)
        detail_layout.addWidget(self.original_text)

        detail_layout.addWidget(QLabel("整理文字（可编辑，不会覆盖原文）"))
        self.edited_text = QPlainTextEdit()
        self.edited_text.setObjectName("libraryEditedText")
        self.edited_text.setPlaceholderText("可在这里校对或整理；上面的初始原文不会改变")
        self.edited_text.setMinimumHeight(120)
        detail_layout.addWidget(self.edited_text)

        edit_form = QFormLayout()
        self.stance_edit = QComboBox()
        self.stance_edit.setObjectName("libraryStance")
        for stance, label in STANCE_LABELS.items():
            self.stance_edit.addItem(label, stance)
        edit_form.addRow("我的态度：", self.stance_edit)
        detail_layout.addLayout(edit_form)

        detail_layout.addWidget(QLabel("备注"))
        self.note_edit = QPlainTextEdit()
        self.note_edit.setObjectName("libraryNote")
        self.note_edit.setPlaceholderText("记录保存原因、疑问或后续行动")
        self.note_edit.setMaximumHeight(100)
        detail_layout.addWidget(self.note_edit)

        action_row = QHBoxLayout()
        self.save_button = QPushButton("保存修改")
        self.save_button.setObjectName("saveLibraryCard")
        self.save_button.clicked.connect(self._save_current)
        action_row.addWidget(self.save_button)
        self.delete_button = QPushButton("彻底删除")
        self.delete_button.setObjectName("deleteLibraryCard")
        self.delete_button.clicked.connect(self._delete_current)
        action_row.addWidget(self.delete_button)
        action_row.addStretch(1)
        detail_layout.addLayout(action_row)

        export_box = QGroupBox("导出当前结果")
        export_row = QHBoxLayout(export_box)
        export_row.addWidget(QLabel("只导出左侧当前搜索和筛选结果："))
        export_row.addStretch(1)
        self.export_json_button = QPushButton("导出 JSON")
        self.export_json_button.setObjectName("exportJson")
        self.export_json_button.clicked.connect(lambda: self._choose_export("json"))
        export_row.addWidget(self.export_json_button)
        self.export_markdown_button = QPushButton("导出 Markdown")
        self.export_markdown_button.setObjectName("exportMarkdown")
        self.export_markdown_button.clicked.connect(lambda: self._choose_export("md"))
        export_row.addWidget(self.export_markdown_button)
        detail_layout.addWidget(export_box)
        detail_layout.addStretch(1)

        detail_scroll.setWidget(detail)
        splitter.addWidget(detail_scroll)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 5)
        outer.addWidget(splitter, 1)

        self.status_label = QLabel("正在读取本地观点库……")
        self.status_label.setObjectName("libraryStatus")
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        outer.addWidget(self.status_label)
        self.setCentralWidget(central)
        self._set_detail_enabled(False)

    @staticmethod
    def _plain_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        return label

    @staticmethod
    def _new_image_label(object_name: str) -> QLabel:
        label = QLabel("尚未选择卡片")
        label.setObjectName(object_name)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setMinimumHeight(220)
        label.setStyleSheet(
            "QLabel { background: #202124; color: #dddddd; padding: 12px; }"
        )
        return label

    def _set_detail_enabled(self, enabled: bool) -> None:
        editable = enabled and not self._busy
        self.edited_text.setEnabled(editable)
        self.stance_edit.setEnabled(editable)
        self.note_edit.setEnabled(editable)
        self.save_button.setEnabled(editable)
        self.delete_button.setEnabled(editable)
        safe_url = (
            _safe_http_url(self._current_card.source_url)
            if self._current_card is not None
            else None
        )
        self.open_source_button.setEnabled(editable and safe_url is not None)

    def _set_busy(self, busy: bool, message: str | None = None) -> None:
        self._busy = busy
        for widget in (
            self.search_edit,
            self.stance_filter,
            self.source_filter,
            self.search_button,
            self.clear_button,
            self.refresh_button,
            self.card_list,
        ):
            widget.setEnabled(not busy)
        self._set_detail_enabled(self._current_card is not None)
        can_export = bool(self._cards) and not busy
        self.export_json_button.setEnabled(can_export)
        self.export_markdown_button.setEnabled(can_export)
        if message is not None:
            self.status_label.setText(message)

    @property
    def busy(self) -> bool:
        """是否正在执行后台数据库或导出任务。"""

        return self._busy

    def refresh(self) -> None:
        """按当前关键词和筛选条件异步刷新有限条卡片。"""

        if self._busy:
            return
        query = " ".join(self.search_edit.text().split())
        stance = self.stance_filter.currentData()
        source = self.source_filter.currentData()
        selected_id = self._current_card.id if self._current_card is not None else None
        store = self.store

        def load() -> list[Card]:
            return store.query_cards(
                query,
                stance=stance,
                text_source=source,
                limit=MAX_VISIBLE_CARDS,
            )

        self._start_task(
            "load",
            load,
            {"selected_id": selected_id, "prefix": ""},
            "正在搜索本地观点库……",
        )

    def request_refresh(self) -> bool:
        """经未保存确认后刷新；返回是否真正启动了刷新。"""

        if self._busy or not self._confirm_discard_changes():
            return False
        self.refresh()
        return True

    def _clear_filters(self) -> None:
        if self._busy or not self._confirm_discard_changes():
            return
        self.search_edit.clear()
        self.stance_filter.setCurrentIndex(0)
        self.source_filter.setCurrentIndex(0)
        self.refresh()

    def _start_task(
        self,
        kind: TaskKind,
        operation: Callable[[], object],
        context: dict[str, object],
        message: str,
    ) -> None:
        if self._worker is not None:
            return
        worker = _LibraryWorker(operation)
        worker.signals.completed.connect(
            self._on_worker_completed,
            Qt.ConnectionType.QueuedConnection,
        )
        self._worker = worker
        self._task_kind = kind
        self._task_context = context
        self._set_busy(True, message)
        self._thread_pool.start(worker)

    @Slot(object)
    def _on_worker_completed(self, worker: object) -> None:
        if isinstance(worker, _LibraryWorker):
            self._settle_worker(worker, show_error=True)

    def _release_worker(self, worker: _LibraryWorker) -> None:
        if worker is not self._worker:
            return
        self._worker = None
        self._task_kind = None
        self._task_context = {}
        worker.release()

    def _settle_worker(self, worker: _LibraryWorker, *, show_error: bool) -> bool:
        if worker is not self._worker or worker.outcome is _UNSET:
            return False
        outcome = worker.outcome
        kind = self._task_kind
        context = dict(self._task_context)
        self._release_worker(worker)
        self._set_busy(False)

        if isinstance(outcome, Exception):
            title = {
                "load": "读取失败",
                "update": "保存失败",
                "delete": "删除失败",
                "export": "导出失败",
            }.get(kind, "操作失败")
            if kind == "delete":
                detail = "删除未完整完成；部分证据截图可能已清理，将刷新列表供核对。"
            else:
                detail = f"{title}；现有资料未被改动。"
            self.status_label.setText(detail)
            if show_error:
                QMessageBox.critical(
                    self,
                    title,
                    f"{detail}\n{outcome}",
                )
            if kind == "delete":
                self.refresh()
            return True

        if kind == "load":
            if not isinstance(outcome, list) or not all(
                isinstance(card, Card) for card in outcome
            ):
                self._handle_invalid_outcome("读取")
                return True
            self._apply_cards(outcome, context)
        elif kind == "update" and isinstance(outcome, Card):
            self._current_card = outcome
            self._populate_detail(outcome)
            self._reload_after_mutation(outcome.id, "修改已安全保存。")
        elif kind == "delete" and outcome is True:
            self._current_card = None
            self._clear_detail()
            self._reload_after_mutation(None, "卡片和两张证据截图已彻底删除。")
        elif kind == "export" and isinstance(outcome, Path):
            self.status_label.setText(f"导出完成：{outcome}")
        else:
            self._handle_invalid_outcome("后台")
        return True

    def _handle_invalid_outcome(self, operation: str) -> None:
        self.status_label.setText(f"{operation}操作返回了无效结果。")
        QMessageBox.critical(self, "操作失败", f"{operation}操作返回了无效结果。")

    def _reload_after_mutation(self, selected_id: str | None, prefix: str) -> None:
        query = " ".join(self.search_edit.text().split())
        stance = self.stance_filter.currentData()
        source = self.source_filter.currentData()
        store = self.store

        def load() -> list[Card]:
            return store.query_cards(
                query,
                stance=stance,
                text_source=source,
                limit=MAX_VISIBLE_CARDS,
            )

        self._start_task(
            "load",
            load,
            {"selected_id": selected_id, "prefix": prefix},
            prefix + " 正在刷新列表……",
        )

    def _apply_cards(
        self,
        cards: list[Card],
        context: dict[str, object],
    ) -> None:
        self._cards = list(cards)
        self._cards_by_id = {card.id: card for card in cards}
        selected_id = context.get("selected_id")
        chosen_row = 0
        self._changing_selection = True
        try:
            self.card_list.clear()
            for index, card in enumerate(cards):
                item = QListWidgetItem(self._card_item_text(card))
                item.setData(Qt.ItemDataRole.UserRole, card.id)
                item.setToolTip(html.escape(card.source_title or "无来源标题"))
                self.card_list.addItem(item)
                if card.id == selected_id:
                    chosen_row = index
            if cards:
                self.card_list.setCurrentRow(chosen_row)
        finally:
            self._changing_selection = False

        if cards:
            self._populate_detail(cards[chosen_row])
        else:
            self._current_card = None
            self._clear_detail()
        prefix = str(context.get("prefix") or "")
        limit_note = (
            f"；最多显示前 {MAX_VISIBLE_CARDS} 条"
            if len(cards) == MAX_VISIBLE_CARDS
            else ""
        )
        self.status_label.setText(f"{prefix}当前显示 {len(cards)} 条卡片{limit_note}")
        self._set_busy(False)

    @staticmethod
    def _card_item_text(card: Card) -> str:
        title = " ".join((card.source_title or "无来源标题").split())
        body = " ".join((card.edited_text or card.text).split())
        if len(title) > 42:
            title = title[:41] + "…"
        if len(body) > 70:
            body = body[:69] + "…"
        return (
            f"【{STANCE_LABELS[card.stance]}】{title}\n"
            f"{body or '（没有整理文字）'}"
        )

    def _on_current_item_changed(
        self,
        current: QListWidgetItem | None,
        previous: QListWidgetItem | None,
    ) -> None:
        if self._changing_selection or self._busy:
            return
        new_id = current.data(Qt.ItemDataRole.UserRole) if current else None
        old_id = self._current_card.id if self._current_card is not None else None
        if new_id == old_id:
            return
        if not self._confirm_discard_changes():
            self._changing_selection = True
            try:
                self.card_list.setCurrentItem(previous)
            finally:
                self._changing_selection = False
            return
        card = self._cards_by_id.get(str(new_id))
        if card is None:
            self._clear_detail()
        else:
            self._populate_detail(card)

    def _populate_detail(self, card: Card) -> None:
        self._current_card = card
        self.original_text.setPlainText(card.text)
        self.edited_text.setPlainText(
            card.edited_text if card.edited_text is not None else card.text
        )
        stance_index = self.stance_edit.findData(card.stance)
        self.stance_edit.setCurrentIndex(max(0, stance_index))
        self.note_edit.setPlainText(card.note)
        self.source_title_label.setText(card.source_title or "未取得标题")
        self.source_url_label.setText(card.source_url or "未取得网址")
        self.created_at_label.setText(self._format_timestamp(card.created_at))
        self.video_time_label.setText(
            f"{card.video_time:.2f} 秒"
            if card.video_time is not None
            else "无视频时间码"
        )
        self.confidence_label.setText(f"{card.confidence:.1%}")
        self.text_source_label.setText(
            SOURCE_LABELS.get(card.text_source, "未知来源")
        )
        self._load_screenshot(
            self.selected_screenshot,
            card.screenshot_path,
            "选区截图",
        )
        self._load_screenshot(
            self.full_screenshot,
            card.full_screenshot_path,
            "完整画面",
        )
        self._set_detail_enabled(True)

    @staticmethod
    def _format_timestamp(value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
        return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S")

    def _load_screenshot(
        self,
        label: QLabel,
        relative_path: str,
        name: str,
    ) -> None:
        label.clear()
        path = _controlled_screenshot_file(self.data_dir, relative_path)
        if path is None:
            label.setText(f"{name}不存在、过大或路径不安全")
            return
        reader = QImageReader(str(path))
        reader.setAutoTransform(True)
        size = reader.size()
        if (
            not size.isValid()
            or size.width() <= 0
            or size.height() <= 0
            or size.width() * size.height() > MAX_SCREENSHOT_PIXELS
        ):
            label.setText(f"{name}尺寸无效或过大")
            return
        reader.setScaledSize(
            size.scaled(
                QSize(620, 360),
                Qt.AspectRatioMode.KeepAspectRatio,
            )
        )
        image = reader.read()
        if image.isNull():
            label.setText(f"{name}无法读取")
            return
        label.setPixmap(QPixmap.fromImage(image))

    def _clear_detail(self) -> None:
        self._current_card = None
        self.original_text.clear()
        self.edited_text.clear()
        self.note_edit.clear()
        self.source_title_label.setText("尚未选择卡片")
        self.source_url_label.setText("—")
        self.created_at_label.setText("—")
        self.video_time_label.setText("—")
        self.confidence_label.setText("—")
        self.text_source_label.setText("—")
        self.selected_screenshot.clear()
        self.selected_screenshot.setText("尚未选择卡片")
        self.full_screenshot.clear()
        self.full_screenshot.setText("尚未选择卡片")
        self._set_detail_enabled(False)

    def _has_unsaved_changes(self) -> bool:
        card = self._current_card
        if card is None:
            return False
        edited_value = self.edited_text.toPlainText()
        edited = None if edited_value == card.text else edited_value
        return (
            edited != card.edited_text
            or self.stance_edit.currentData() != card.stance
            or self.note_edit.toPlainText() != card.note
        )

    def _confirm_discard_changes(self) -> bool:
        if not self._has_unsaved_changes():
            return True
        answer = QMessageBox.warning(
            self,
            "尚未保存",
            "当前卡片还有未保存的修改。要放弃这些修改吗？",
            QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Discard

    def _save_current(self) -> None:
        card = self._current_card
        if card is None or self._busy:
            return
        edited_value = self.edited_text.toPlainText()
        changes = {
            "edited_text": None if edited_value == card.text else edited_value,
            "stance": self.stance_edit.currentData(),
            "note": self.note_edit.toPlainText(),
        }
        store = self.store
        self._start_task(
            "update",
            lambda: store.update_card(card.id, **changes),
            {"card_id": card.id},
            "正在安全保存修改……",
        )

    def _delete_current(self) -> None:
        card = self._current_card
        if card is None or self._busy:
            return
        answer = QMessageBox.warning(
            self,
            "确认彻底删除",
            (
                "这会永久删除这张卡片、全文索引、选区截图和完整截图，"
                "且无法撤销。\n\n确定继续吗？"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        store = self.store
        self._start_task(
            "delete",
            lambda: store.delete_card(card.id),
            {"card_id": card.id},
            "正在彻底删除卡片和证据截图……",
        )

    def _open_source(self) -> None:
        card = self._current_card
        url = _safe_http_url(card.source_url if card is not None else None)
        if url is None:
            QMessageBox.information(self, "无法打开", "这张卡片没有安全的网页来源。")
            return
        if not QDesktopServices.openUrl(url):
            QMessageBox.warning(self, "无法打开", "系统未能打开这个网页来源。")

    def _choose_export(self, format_name: Literal["json", "md"]) -> None:
        if self._busy or not self._cards:
            return
        if self._has_unsaved_changes():
            QMessageBox.information(
                self,
                "请先保存修改",
                "当前卡片有未保存修改；请先保存或放弃，再导出当前结果。",
            )
            return
        try:
            export_dir = self._controlled_export_dir()
        except Exception as exc:
            QMessageBox.critical(self, "导出失败", str(exc))
            return
        extension = ".json" if format_name == "json" else ".md"
        label = "JSON 文件 (*.json)" if format_name == "json" else "Markdown 文件 (*.md)"
        default_name = export_dir / (
            "观点库导出_" + datetime.now().strftime("%Y%m%d_%H%M%S") + extension
        )
        selected, _ = QFileDialog.getSaveFileName(
            self,
            "选择导出位置",
            str(default_name),
            label,
        )
        if not selected:
            return
        try:
            target = self._validate_export_target(Path(selected), extension)
        except Exception as exc:
            QMessageBox.critical(self, "导出失败", str(exc))
            return
        cards = tuple(self._cards)

        def export() -> Path:
            from .exporting import cards_to_json, cards_to_markdown

            content = (
                cards_to_json(cards)
                if format_name == "json"
                else cards_to_markdown(cards)
            )
            return _atomic_write_text(target, content)

        self._start_task(
            "export",
            export,
            {"target": target},
            "正在导出当前结果……",
        )

    def _controlled_export_dir(self) -> Path:
        root = self.data_dir.resolve()
        export_dir = root / "exports"
        try:
            export_dir.mkdir(parents=True, exist_ok=True)
            resolved = export_dir.resolve()
        except OSError as exc:
            raise RuntimeError("无法创建 D 盘本地导出目录。") from exc
        if not _is_descendant(resolved, root):
            raise RuntimeError("本地导出目录超出受控数据目录。")
        return resolved

    def _validate_export_target(self, target: Path, extension: str) -> Path:
        if "\x00" in str(target):
            raise RuntimeError("导出文件名无效。")
        candidate = target.expanduser()
        if (
            str(candidate).startswith("\\\\")
            or candidate.drive.startswith("\\\\")
        ):
            raise RuntimeError("请把导出文件保存到本机磁盘，而不是网络路径。")
        if candidate.is_symlink():
            raise RuntimeError("不能把导出文件写入符号链接。")
        try:
            raw_resolved = candidate.resolve()
            raw_database = self.store.db_path.resolve()
            raw_screenshot_root = (self.data_dir.resolve() / "screenshots").resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            raise RuntimeError("无法安全解析导出路径。") from exc
        if raw_resolved == raw_database or _is_descendant(
            raw_resolved, raw_screenshot_root
        ):
            raise RuntimeError("不能用导出文件覆盖本地数据库或证据截图。")
        if candidate.suffix.casefold() != extension:
            candidate = candidate.with_name(candidate.name + extension)
        if candidate.is_symlink():
            raise RuntimeError("不能把导出文件写入符号链接。")
        try:
            resolved = candidate.resolve()
            root = self.data_dir.resolve()
            screenshot_root = (root / "screenshots").resolve()
            export_root = self._controlled_export_dir()
            database = self.store.db_path.resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            raise RuntimeError("无法安全解析导出路径。") from exc
        if resolved == database or _is_descendant(resolved, screenshot_root):
            raise RuntimeError("不能用导出文件覆盖本地数据库或证据截图。")
        if (resolved == root or _is_descendant(resolved, root)) and not _is_descendant(
            resolved, export_root
        ):
            raise RuntimeError("数据目录内只能导出到 exports 文件夹。")
        return resolved

    def wait_for_idle(self, timeout_ms: int = -1) -> bool:
        """等待当前任务及其后续列表刷新完成；供测试和退出阶段使用。"""

        if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool):
            raise TypeError("timeout_ms 必须是整数")
        deadline = None if timeout_ms < 0 else monotonic() + timeout_ms / 1000.0
        while self._worker is not None:
            worker = self._worker
            if deadline is None:
                remaining = -1
            else:
                remaining = max(0, int((deadline - monotonic()) * 1000))
            if not worker.wait(remaining):
                return False
            self._settle_worker(worker, show_error=False)
        return True

    def closeEvent(self, event: QCloseEvent) -> None:
        """操作进行中或有未保存编辑时避免误关闭；正常关闭仅隐藏窗口。"""

        if self._busy:
            self.status_label.setText("本地操作完成前暂不能关闭观点库。")
            event.ignore()
            return
        if not self._confirm_discard_changes():
            event.ignore()
            return
        if self._current_card is not None:
            self._populate_detail(self._current_card)
        else:
            self._clear_detail()
        event.accept()
