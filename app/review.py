"""观点卡片的本地人工审核窗口。

窗口可以直接展示纯内存候选；用户明确保存后才短暂建立数据库草稿、写入截图并
原子转为正式卡片。丢弃、Esc 或关闭内存候选不会写入磁盘。模块导入本身不会创建
QApplication、访问数据库或读取截图。
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from collections.abc import Callable
from threading import Event, Lock
from typing import Final, Literal

from PIL import Image
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Signal, Slot
from PySide6.QtGui import QCloseEvent, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
    QMessageBox,
)

from .config import DATA_DIR
from .capture import save_image
from .models import Card, Stance
from .store import Store

STANCE_LABELS: Final[dict[Stance, str]] = {
    "unknown": "暂不判断",
    "agree": "认同",
    "disagree": "反对",
    "doubt": "存疑",
    "useful": "只是有用",
}


class _DraftRollbackError(RuntimeError):
    """保存失败且后台无法确认短暂草稿已经清理。"""


class _CommitSignals(QObject):
    completed = Signal(object)


class _CommitWorker(QRunnable):
    """只执行磁盘与数据库事务，不接触任何 GUI 对象。"""

    def __init__(self, operation: Callable[[], Card]) -> None:
        super().__init__()
        self.signals = _CommitSignals()
        self._operation: Callable[[], Card] | None = operation
        self._completed = Event()
        self._outcome_lock = Lock()
        self._outcome: Card | Exception | None = None
        self.setAutoDelete(False)

    @Slot()
    def run(self) -> None:
        operation = self._operation
        if operation is None:
            return
        try:
            result = operation()
        except Exception as exc:
            outcome: Card | Exception = exc
        else:
            outcome = result
        with self._outcome_lock:
            self._outcome = outcome
        self._completed.set()
        self.signals.completed.emit(self)

    @property
    def outcome(self) -> Card | Exception | None:
        """返回后台结果；完成事件设置前为 ``None``。"""

        with self._outcome_lock:
            return self._outcome

    def wait(self, timeout_ms: int) -> bool:
        timeout = None if timeout_ms < 0 else timeout_ms / 1000.0
        return self._completed.wait(timeout)

    def release(self) -> None:
        self._operation = None


def _controlled_file(data_dir: Path, relative_path: str) -> Path | None:
    """解析受控数据文件；任何越界、目录或不存在的目标都返回 ``None``。

    ``resolve`` 同时处理 ``..`` 和目录内指向外部的符号链接，避免审核窗口被一条
    损坏的数据库记录诱导去读取数据目录之外的文件。
    """

    root = data_dir.expanduser().resolve()
    try:
        screenshot_root = (root / "screenshots").resolve()
        candidate = (root / relative_path).resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    if (
        screenshot_root == root
        or root not in screenshot_root.parents
        or candidate == screenshot_root
        or screenshot_root not in candidate.parents
    ):
        return None
    try:
        return candidate if candidate.is_file() else None
    except OSError:
        return None


def _controlled_target_file(data_dir: Path, relative_path: str) -> Path:
    """解析严格位于受控截图根内的目标路径。"""

    root = data_dir.expanduser().resolve()
    try:
        screenshot_root = (root / "screenshots").resolve()
        candidate = (root / relative_path).resolve()
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise RuntimeError("截图目标路径无效") from exc
    if (
        screenshot_root == root
        or root not in screenshot_root.parents
        or candidate == screenshot_root
        or screenshot_root not in candidate.parents
    ):
        raise RuntimeError("截图目标超出受控截图目录")
    return candidate


def _unlink_expected_files(paths: tuple[Path, Path]) -> bool:
    """不借助数据库，尽力删除本次规范目标；无法确认完整清理时返回 False。"""

    confirmed = True
    for path in set(paths):
        try:
            if path.is_dir():
                confirmed = False
                continue
            path.unlink(missing_ok=True)
        except OSError:
            confirmed = False
    return confirmed


def _rollback_database_state(
    store: Store,
    card_id: str,
) -> tuple[Literal["absent", "saved", "unknown"], Card | None]:
    """尝试删除草稿，并区分可清文件、正式证据和无法确认三种状态。"""

    try:
        deleted = store.delete_draft(card_id)
    except Exception:
        try:
            saved = store.get_card(card_id)
            return ("saved", saved) if saved is not None else ("unknown", None)
        except Exception:
            return "unknown", None
    if deleted:
        return "absent", None
    try:
        saved = store.get_card(card_id)
        return ("saved", saved) if saved is not None else ("absent", None)
    except Exception:
        return "unknown", None


def _commit_memory_candidate(
    store: Store,
    data_dir: Path,
    card: Card,
    selected_image: Image.Image,
    full_image: Image.Image,
    *,
    clear_pending_draft: bool,
) -> Card:
    """在后台线程提交内存候选；任何失败都尽力按草稿状态回滚。"""

    selected_path = _controlled_target_file(data_dir, card.screenshot_path)
    full_path = _controlled_target_file(data_dir, card.full_screenshot_path)
    if selected_path == full_path:
        raise RuntimeError("选区截图和完整截图不能使用同一路径")
    paths = (selected_path, full_path)

    if clear_pending_draft:
        try:
            pending_cleared = store.delete_draft(card.id)
        except Exception as exc:
            try:
                saved = store.get_card(card.id)
            except Exception:
                saved = None
            if saved is not None:
                if saved == card:
                    return saved
                raise _DraftRollbackError(
                    "同一标识已存在内容不同的正式卡片；已拒绝冒充本次保存"
                ) from exc
            raise _DraftRollbackError(
                "上次失败的短暂草稿状态无法确认；请停止采集并检查数据目录"
            ) from exc
        if not pending_cleared:
            try:
                saved = store.get_card(card.id)
            except Exception as exc:
                raise _DraftRollbackError(
                    "上次失败的短暂草稿状态无法确认；请停止采集并检查数据目录"
                ) from exc
            if saved is not None:
                if saved == card:
                    return saved
                raise _DraftRollbackError(
                    "同一标识已存在内容不同的正式卡片；已拒绝冒充本次保存"
                )
            if not _unlink_expected_files(paths):
                raise _DraftRollbackError("上次失败的候选截图无法确认清理")

    if selected_path.exists() or full_path.exists():
        raise RuntimeError("截图目标已存在，已拒绝覆盖")

    try:
        store.add_draft(card)
        save_image(selected_image, selected_path)
        save_image(full_image, full_path)
        return store.finalize_draft(card.id)
    except Exception as exc:
        database_state, saved = _rollback_database_state(store, card.id)
        if database_state == "saved":
            if saved == card:
                return saved
            raise _DraftRollbackError(
                "同一标识已存在内容不同的正式卡片；已拒绝冒充本次保存"
            ) from exc
        files_clean = (
            _unlink_expected_files(paths) if database_state == "absent" else False
        )
        if database_state != "absent" or not files_clean:
            raise _DraftRollbackError(
                "保存失败，且短暂草稿未能完整回滚；请停止采集并检查数据目录"
            ) from exc
        raise


class CardReviewDialog(QDialog):
    """让用户审核纯内存候选或兼容的旧磁盘草稿。

    ``saved`` 仅在数据库更新成功后为 ``True``；``finalized_card`` 随后保存 Store
    返回的最终卡片。所有取消路径都先完成删除，删除失败时窗口会继续保持打开。
    """

    def __init__(
        self,
        card: Card,
        store: Store,
        *,
        data_dir: str | Path = DATA_DIR,
        selected_image: Image.Image | None = None,
        full_image: Image.Image | None = None,
        selected_preview_png: bytes | None = None,
        full_preview_png: bytes | None = None,
        thread_pool: QThreadPool | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        if (selected_image is None) != (full_image is None):
            raise ValueError("内存审核候选必须同时提供选区和完整画面")
        for preview in (selected_preview_png, full_preview_png):
            if preview is not None and not isinstance(preview, bytes):
                raise TypeError("截图预览必须是 PNG bytes")
        self.card = card
        self.store = store
        self.data_dir = Path(data_dir).expanduser().resolve()
        if store.data_dir != self.data_dir:
            raise ValueError("审核窗口数据目录与 Store 数据根目录不一致")
        self.selected_image = selected_image
        self.full_image = full_image
        self._memory_candidate = selected_image is not None
        if self._memory_candidate:
            expected_selected = f"screenshots/{card.id}.png"
            expected_full = f"screenshots/full_{card.id}.png"
            if (
                card.screenshot_path != expected_selected
                or card.full_screenshot_path != expected_full
            ):
                raise ValueError("内存候选截图路径不符合安全文件名规范")
        self.thread_pool = (
            thread_pool if thread_pool is not None else QThreadPool.globalInstance()
        )
        self._pending_disk_draft = False
        self._saving = False
        self._save_worker: _CommitWorker | None = None
        self.saved = False
        self.finalized_card: Card | None = None
        self._finalized = False

        self.setWindowTitle("审核观点卡片")
        self.setModal(True)
        self.setMinimumSize(720, 760)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)

        evidence_widget = QWidget()
        evidence_layout = QHBoxLayout(evidence_widget)
        evidence_layout.setContentsMargins(0, 0, 0, 0)

        selection_widget = QWidget()
        selection_layout = QVBoxLayout(selection_widget)
        selection_layout.setContentsMargins(0, 0, 0, 0)
        selection_layout.addWidget(QLabel("选区截图"))
        self.screenshot_label = self._new_screenshot_label("selectionScreenshot")
        selection_layout.addWidget(self.screenshot_label)

        full_widget = QWidget()
        full_layout = QVBoxLayout(full_widget)
        full_layout.setContentsMargins(0, 0, 0, 0)
        self.full_screenshot_notice = QLabel("完整冻结画面（保存时会一并保留完整冻结画面）")
        self.full_screenshot_notice.setWordWrap(True)
        full_layout.addWidget(self.full_screenshot_notice)
        self.full_screenshot_label = self._new_screenshot_label("fullScreenshot")
        full_layout.addWidget(self.full_screenshot_label)

        evidence_layout.addWidget(selection_widget, 1)
        evidence_layout.addWidget(full_widget, 1)
        self._load_screenshot(
            self.screenshot_label,
            card.screenshot_path,
            name="选区截图",
            image=selected_image,
            preview_png=selected_preview_png,
        )
        self._load_screenshot(
            self.full_screenshot_label,
            card.full_screenshot_path,
            name="完整冻结画面",
            image=full_image,
            preview_png=full_preview_png,
        )

        self.captured_text_view = QPlainTextEdit(card.text)
        self.captured_text_view.setObjectName("capturedText")
        self.captured_text_view.setReadOnly(True)
        self.captured_text_view.setMaximumHeight(95)

        edited_text = getattr(card, "edited_text", None)
        self.text_edit = QPlainTextEdit(edited_text if edited_text is not None else card.text)
        self.text_edit.setObjectName("cardText")
        self.text_edit.setPlaceholderText("可选：校对或整理文字；初始提取原文不会被覆盖")
        self.text_edit.setMinimumHeight(145)

        source_box = QGroupBox("来源（只读）")
        source_form = QFormLayout(source_box)
        self.source_title_label = self._source_value(card.source_title or "未取得标题")
        self.source_url_label = self._source_value(card.source_url or "未取得网址")
        video_text = (
            f"{card.video_time:.2f} 秒" if card.video_time is not None else "无视频时间码"
        )
        self.video_time_label = self._source_value(video_text)
        self.confidence_label = self._source_value(f"{card.confidence:.1%}")
        source_form.addRow("标题：", self.source_title_label)
        source_form.addRow("网址：", self.source_url_label)
        source_form.addRow("视频位置：", self.video_time_label)
        source_form.addRow("文字置信度：", self.confidence_label)

        stance_box = QGroupBox("我的态度")
        stance_layout = QHBoxLayout(stance_box)
        self.stance_group = QButtonGroup(self)
        self.stance_buttons: dict[Stance, QRadioButton] = {}
        for stance, label in STANCE_LABELS.items():
            button = QRadioButton(label)
            button.setProperty("stance", stance)
            self.stance_group.addButton(button)
            self.stance_buttons[stance] = button
            stance_layout.addWidget(button)
        self.stance_buttons[card.stance].setChecked(True)

        self.note_edit = QPlainTextEdit(card.note)
        self.note_edit.setObjectName("cardNote")
        self.note_edit.setPlaceholderText("可选：写下为什么保存、哪里存疑或之后要做什么")
        self.note_edit.setMaximumHeight(100)

        self.button_box = QDialogButtonBox()
        self.save_button = QPushButton("保存卡片")
        self.save_button.setObjectName("saveCard")
        self.save_button.setDefault(True)
        self.discard_button = QPushButton("丢弃")
        self.discard_button.setObjectName("discardCard")
        self.button_box.addButton(
            self.save_button,
            QDialogButtonBox.ButtonRole.AcceptRole,
        )
        self.button_box.addButton(
            self.discard_button,
            QDialogButtonBox.ButtonRole.DestructiveRole,
        )
        self.save_button.clicked.connect(self._save)
        self.discard_button.clicked.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(evidence_widget)
        layout.addWidget(QLabel("初始提取原文（只读）"))
        layout.addWidget(self.captured_text_view)
        layout.addWidget(QLabel("整理文字（可编辑）"))
        layout.addWidget(self.text_edit)
        layout.addWidget(source_box)
        layout.addWidget(stance_box)
        layout.addWidget(QLabel("备注"))
        layout.addWidget(self.note_edit)
        layout.addWidget(self.button_box)

    @staticmethod
    def _source_value(text: str) -> QLabel:
        label = QLabel(text)
        # 标题和网址来自外部页面，只能作为纯文本资料显示，不能解释为富文本。
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        return label

    @staticmethod
    def _new_screenshot_label(object_name: str) -> QLabel:
        label = QLabel()
        label.setObjectName(object_name)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setMinimumHeight(160)
        label.setStyleSheet(
            "QLabel { background: #202124; color: #dddddd; padding: 10px; }"
        )
        return label

    def _load_screenshot(
        self,
        label: QLabel,
        relative_path: str,
        *,
        name: str,
        image: Image.Image | None = None,
        preview_png: bytes | None = None,
    ) -> None:
        if preview_png is not None:
            pixmap = QPixmap()
            pixmap.loadFromData(preview_png, "PNG")
        elif image is not None:
            try:
                buffer = BytesIO()
                image.save(buffer, format="PNG")
                pixmap = QPixmap()
                pixmap.loadFromData(buffer.getvalue(), "PNG")
            except (OSError, ValueError):
                label.setText(f"{name}无法从内存读取")
                return
        else:
            path = _controlled_file(self.data_dir, relative_path)
            if path is None:
                label.setText(f"{name}不存在或路径不安全")
                return
            pixmap = QPixmap(str(path))
        if pixmap.isNull():
            label.setText(f"{name}无法读取")
            return
        label.setPixmap(
            pixmap.scaled(
                300,
                200,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def _selected_stance(self) -> Stance:
        for stance, button in self.stance_buttons.items():
            if button.isChecked():
                return stance
        # 构造时总会选中一个按钮；保守降级，绝不把空选择解释成“认同”。
        return "unknown"

    def _show_error(self, title: str, message: str) -> None:
        QMessageBox.critical(self, title, message)

    def _save(self) -> None:
        if self._finalized or self._saving:
            return
        edited_value = self.text_edit.toPlainText()
        edited_text = None if edited_value == self.card.text else edited_value
        try:
            changes = {
                "edited_text": edited_text,
                "stance": self._selected_stance(),
                "note": self.note_edit.toPlainText(),
            }
            if self._memory_candidate:
                payload = self.card.model_dump()
                payload.update(changes)
                updated = Card.model_validate(payload)
                selected_image = self.selected_image
                full_image = self.full_image
                if selected_image is None or full_image is None:
                    raise RuntimeError("内存候选画面已经不可用")
                store = self.store
                data_dir = self.data_dir
                clear_pending = self._pending_disk_draft
                operation = lambda: _commit_memory_candidate(
                    store,
                    data_dir,
                    updated,
                    selected_image,
                    full_image,
                    clear_pending_draft=clear_pending,
                )
            else:
                store = self.store
                card_id = self.card.id
                operation = lambda: store.finalize_draft(card_id, **changes)
        except Exception as exc:
            self._show_error("保存失败", f"卡片尚未保存，请重试。\n{exc}")
            return

        try:
            self._start_commit(operation)
        except Exception as exc:
            self._release_save_worker()
            self._set_saving(False)
            self._show_error("保存失败", f"无法启动本地保存任务，请重试。\n{exc}")

    def _set_saving(self, saving: bool) -> None:
        self._saving = saving
        self.text_edit.setEnabled(not saving)
        self.note_edit.setEnabled(not saving)
        for button in self.stance_buttons.values():
            button.setEnabled(not saving)
        self.button_box.setEnabled(not saving)
        self.save_button.setText("正在保存……" if saving else "保存卡片")

    def _start_commit(self, operation: Callable[[], Card]) -> None:
        self._set_saving(True)
        worker = _CommitWorker(operation)
        worker.signals.completed.connect(
            self._on_worker_completed,
            Qt.ConnectionType.QueuedConnection,
        )
        self._save_worker = worker
        self.thread_pool.start(worker)

    def _release_save_worker(self, worker: _CommitWorker | None = None) -> None:
        if worker is not None and worker is not self._save_worker:
            return
        worker = self._save_worker
        self._save_worker = None
        if worker is not None:
            worker.release()

    @Slot(object)
    def _on_worker_completed(self, worker: object) -> None:
        if isinstance(worker, _CommitWorker):
            self._settle_worker(worker, show_error=True)

    def _settle_worker(self, worker: _CommitWorker, *, show_error: bool) -> bool:
        """只结算当前 worker 一次；等待路径与排队 signal 可安全竞争。"""

        if worker is not self._save_worker:
            return False
        outcome = worker.outcome
        if outcome is None:
            return False
        self._release_save_worker(worker)
        if isinstance(outcome, Card):
            self._pending_disk_draft = False
            self._saving = False
            self.card = outcome
            self.finalized_card = outcome
            self.saved = True
            self._finalized = True
            super().accept()
            return True

        error = (
            outcome
            if isinstance(outcome, Exception)
            else TypeError("后台保存返回了无效结果")
        )
        self._pending_disk_draft = isinstance(error, _DraftRollbackError)
        self._set_saving(False)
        if show_error:
            self._show_error("保存失败", f"卡片尚未保存，请重试。\n{error}")
        return True

    def wait_for_save(self, timeout_ms: int = -1) -> bool:
        """同步等待当前保存并直接结算；超时返回 False，失败也算已结算。

        该入口供主事件循环退出阶段使用，因此失败不会弹出阻塞消息框。随后到达的
        Qt 完成 signal 会因 worker 已不再是当前任务而被幂等忽略。
        """

        if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool):
            raise TypeError("timeout_ms 必须是整数")
        worker = self._save_worker
        if not self._saving or worker is None:
            return True
        if not worker.wait(timeout_ms):
            return False
        self._settle_worker(worker, show_error=False)
        return not self._saving

    @property
    def saving(self) -> bool:
        """当前是否仍在后台提交（只读）。"""

        return self._saving

    @property
    def pending_disk_draft(self) -> bool:
        """失败后是否仍有无法确认状态的磁盘草稿（只读）。"""

        return self._pending_disk_draft

    @property
    def finalized(self) -> bool:
        """数据库操作和窗口结局是否已经成功确定（只读）。"""

        return self._finalized

    @property
    def discarded(self) -> bool:
        """候选是否已经成功删除（只读）。"""

        return self._finalized and not self.saved

    def _delete_candidate(self) -> bool:
        if self._finalized:
            return True
        if self._saving:
            return False
        if self._memory_candidate and not self._pending_disk_draft:
            self.saved = False
            self.finalized_card = None
            self._finalized = True
            return True
        try:
            deleted = self.store.delete_draft(self.card.id)
            if not deleted:
                raise RuntimeError("候选卡片已不存在，无法确认截图是否已清理")
        except Exception as exc:  # 删除失败时不得让窗口表现为已经完成丢弃
            self._show_error("丢弃失败", f"卡片尚未完整删除，请重试。\n{exc}")
            return False

        self.saved = False
        self.finalized_card = None
        self._finalized = True
        return True

    def reject(self) -> None:
        """把按钮、Esc 和其他 QDialog 拒绝路径统一为“先删除再关闭”。"""

        if self._delete_candidate():
            super().reject()

    def done(self, result: int) -> None:
        """封住直接 ``done`` 路径，避免绕过保存或草稿清理。"""

        if result == int(QDialog.DialogCode.Accepted):
            if self._finalized and self.saved:
                super().done(result)
            else:
                self._save()
            return
        if self._delete_candidate():
            super().done(result)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        """关闭按钮也必须删除候选；失败时忽略关闭事件。"""

        if self._saving:
            event.ignore()
        elif self._finalized or self._delete_candidate():
            event.accept()
        else:
            event.ignore()
