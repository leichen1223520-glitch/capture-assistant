"""Obsidian 单向归档的后台调度与应用生命周期管理。"""

from __future__ import annotations

from collections.abc import Callable
import logging
from pathlib import Path
from threading import Event, Lock
from time import monotonic
from typing import Final

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal, Slot, Qt

from .obsidian import (
    ObsidianError,
    ObsidianMirror,
    ObsidianSettings,
    ObsidianSettingsStore,
    SyncResult,
)
from .config import OBSIDIAN_RECONCILE_INTERVAL_MS
from .store import Store

DEFAULT_RECONCILE_INTERVAL_MS: Final = OBSIDIAN_RECONCILE_INTERVAL_MS
MAX_SYNC_CARDS: Final = 10_000
_UNSET: Final = object()

LOGGER = logging.getLogger(__name__)


class _SyncSignals(QObject):
    """把工作线程的完成事件送回拥有管理器的 Qt 线程。"""

    completed = Signal(object)


class _SyncWorker(QRunnable):
    """执行一次不接触 GUI 对象的有界归档操作。"""

    def __init__(self, operation: Callable[[], SyncResult], *, manual: bool) -> None:
        super().__init__()
        self.signals = _SyncSignals()
        self.manual = manual
        self._operation: Callable[[], SyncResult] | None = operation
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
        except Exception as exc:  # 由 GUI 线程统一转为不含卡片正文的状态信息
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


class ObsidianArchiveManager(QObject):
    """后台维护已保存卡片到 Obsidian Vault 的单向本地镜像。

    SQLite 始终是事实来源。管理器在已保存卡片变化、固定间隔以及退出前触发
    对账；同一时间最多运行一个工作线程，多次请求会合并为下一轮同步。
    """

    state_changed = Signal()
    sync_completed = Signal(object, bool)
    sync_failed = Signal(str, bool)
    _saved_change_received = Signal()

    def __init__(
        self,
        store: Store,
        *,
        data_dir: str | Path,
        interval_ms: int = DEFAULT_RECONCILE_INTERVAL_MS,
        settings_store: ObsidianSettingsStore | None = None,
        mirror: ObsidianMirror | None = None,
        thread_pool: QThreadPool | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        if (
            isinstance(interval_ms, bool)
            or not isinstance(interval_ms, int)
            or interval_ms < 1_000
        ):
            raise ValueError("Obsidian 对账间隔至少为 1000 毫秒")

        self.store = store
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.settings_store = settings_store or ObsidianSettingsStore(self.data_dir)
        self.mirror = mirror or ObsidianMirror(self.data_dir)
        self.thread_pool = thread_pool or QThreadPool.globalInstance()
        self.timer = QTimer(self)
        self.timer.setInterval(interval_ms)
        self.timer.setSingleShot(False)
        self.timer.timeout.connect(self._automatic_sync)
        self._saved_change_received.connect(
            self._automatic_sync,
            Qt.ConnectionType.QueuedConnection,
        )
        self._worker: _SyncWorker | None = None
        self._pending = False
        self._pending_manual = False
        self._unsubscribe: Callable[[], None] | None = None
        self._started = False
        self._stopping = False
        self._configuration_error: str | None = None
        self._last_sync_issue: str | None = None
        try:
            self._settings = self.settings_store.load()
        except ObsidianError as exc:
            self._settings = ObsidianSettings()
            self._configuration_error = str(exc)
        except (OSError, UnicodeError, ValueError) as exc:
            LOGGER.warning(
                "无法读取 Obsidian 设置：%s",
                type(exc).__name__,
            )
            self._settings = ObsidianSettings()
            self._configuration_error = "Obsidian 设置暂时无法读取，请重新选择 Vault。"

    @property
    def settings(self) -> ObsidianSettings:
        """返回当前内存中的不可变配置快照。"""

        return self._settings

    @property
    def configuration_error(self) -> str | None:
        """返回设置文件无法读取时的安全错误，不包含卡片正文。"""

        return self._configuration_error

    @property
    def last_sync_issue(self) -> str | None:
        """返回最近一次同步错误或冲突摘要；下一次完整成功后清除。"""

        return self._last_sync_issue

    @property
    def busy(self) -> bool:
        """当前是否有同步工作线程尚未归并。"""

        return self._worker is not None

    @property
    def configured(self) -> bool:
        """当前是否具有可供再次验证的 Vault 路径。"""

        return bool(self._settings.vault_path)

    @property
    def managed_root(self) -> Path | None:
        """返回受管目录的预期路径；未配置时返回 ``None``。"""

        if not self._settings.vault_path:
            return None
        try:
            return self.mirror.managed_root(self._settings.vault_path)
        except ObsidianError:
            return None

    def start(self) -> None:
        """订阅本进程保存事件，启动定时对账并补齐上次遗漏。"""

        if self._started:
            return
        self._started = True
        self._stopping = False
        self._unsubscribe = self.store.subscribe_saved_changes(
            self._saved_change_received.emit
        )
        self.timer.start()
        if self._settings.enabled and self._configuration_error is None:
            self.request_sync()

    def configure(self, vault_path: str | Path, *, enabled: bool = True) -> Path:
        """验证并保存用户明确选择的 Vault，不创建或修改 ``.obsidian``。"""

        if self.busy:
            raise ObsidianError("正在完成上一轮归档，请稍后再更换 Vault。")
        resolved = self.mirror.validate_vault(vault_path)
        same_vault = False
        if self._settings.vault_path:
            try:
                same_vault = (
                    Path(self._settings.vault_path).expanduser().resolve()
                    == resolved
                )
            except (OSError, RuntimeError):
                same_vault = False
        settings = ObsidianSettings(
            enabled=enabled,
            vault_path=str(resolved),
            # 截图授权按 Vault 隔离；换到另一个目录时必须由用户重新明确开启。
            copy_attachments=(
                self._settings.copy_attachments if same_vault else False
            ),
        )
        self.settings_store.save(settings)
        self._settings = settings
        self._configuration_error = None
        self._last_sync_issue = None
        self.state_changed.emit()
        if enabled and self._started:
            self.request_sync(manual=True)
        return resolved

    def set_enabled(self, enabled: bool) -> None:
        """持久化自动归档开关；启用时重新验证 Vault。"""

        if not isinstance(enabled, bool):
            raise TypeError("enabled 必须是布尔值")
        if self.busy:
            raise ObsidianError("当前归档尚未完成，请等待状态不再显示“正在同步”后再切换。")
        if enabled:
            if not self._settings.vault_path:
                raise ObsidianError("请先选择一个 Obsidian Vault。")
            self.mirror.validate_vault(self._settings.vault_path)
        settings = ObsidianSettings(
            enabled=enabled,
            vault_path=self._settings.vault_path,
            copy_attachments=self._settings.copy_attachments,
        )
        self.settings_store.save(settings)
        self._settings = settings
        self._configuration_error = None
        self._last_sync_issue = None
        self.state_changed.emit()
        if enabled and self._started:
            self.request_sync(manual=True)

    def set_copy_attachments(self, enabled: bool) -> None:
        """设置是否复制选区截图；关闭不会自动删除已有 Obsidian 副本。"""

        if not isinstance(enabled, bool):
            raise TypeError("enabled 必须是布尔值")
        if self.busy:
            raise ObsidianError("当前归档尚未完成，请等待状态不再显示“正在同步”后再更改截图设置。")
        if not self._settings.vault_path:
            raise ObsidianError("请先选择一个 Obsidian Vault。")
        settings = ObsidianSettings(
            enabled=self._settings.enabled,
            vault_path=self._settings.vault_path,
            copy_attachments=enabled,
        )
        self.settings_store.save(settings)
        self._settings = settings
        self._configuration_error = None
        self._last_sync_issue = None
        self.state_changed.emit()
        if settings.enabled and self._started:
            self.request_sync(manual=True)

    @Slot()
    def _automatic_sync(self) -> None:
        self.request_sync(manual=False)

    def request_sync(self, *, manual: bool = False) -> bool:
        """请求一次同步；忙碌时合并请求并返回 ``False``。"""

        if self._stopping:
            return False
        if self._configuration_error is not None:
            if manual:
                self.sync_failed.emit(self._configuration_error, True)
            return False
        if not self._settings.enabled:
            return False
        if self._worker is not None:
            self._pending = True
            self._pending_manual = self._pending_manual or manual
            return False
        return self._launch_worker(manual=manual)

    def _launch_worker(self, *, manual: bool) -> bool:
        settings = self._settings

        def operation() -> SyncResult:
            cards = self.store.list_saved_snapshot(MAX_SYNC_CARDS + 1)
            if len(cards) > MAX_SYNC_CARDS:
                raise ObsidianError(
                    "已保存卡片超过 Obsidian 首版的 10000 张同步上限；"
                    "为避免生成不完整索引，本轮没有写入。"
                )
            return self.mirror.sync(cards, settings)

        worker = _SyncWorker(operation, manual=manual)
        worker.signals.completed.connect(
            self._on_worker_completed,
            Qt.ConnectionType.QueuedConnection,
        )
        self._worker = worker
        self.state_changed.emit()
        try:
            self.thread_pool.start(worker)
        except (RuntimeError, TypeError) as exc:
            self._worker = None
            worker.release()
            LOGGER.warning(
                "无法启动 Obsidian 后台任务：%s",
                type(exc).__name__,
            )
            self._last_sync_issue = "无法启动 Obsidian 后台归档，请稍后重试。"
            self.sync_failed.emit(self._last_sync_issue, manual)
            self.state_changed.emit()
            return False
        return True

    @Slot(object)
    def _on_worker_completed(self, worker: object) -> None:
        if isinstance(worker, _SyncWorker):
            self._settle_worker(worker, allow_reschedule=not self._stopping)

    def _settle_worker(
        self,
        worker: _SyncWorker,
        *,
        allow_reschedule: bool,
    ) -> bool:
        if worker is not self._worker or worker.outcome is _UNSET:
            return False
        outcome = worker.outcome
        manual = worker.manual
        worker.release()
        self._worker = None

        pending = self._pending
        pending_manual = self._pending_manual
        self._pending = False
        self._pending_manual = False

        if isinstance(outcome, SyncResult):
            self._last_sync_issue = (
                f"有 {len(outcome.conflicts)} 个受管文件存在冲突。"
                if outcome.conflicts
                else None
            )
            self.sync_completed.emit(outcome, manual)
        else:
            LOGGER.warning(
                "Obsidian 后台归档失败：%s",
                type(outcome).__name__,
            )
            message = (
                str(outcome)
                if isinstance(outcome, ObsidianError)
                else "Obsidian 自动归档暂时失败，请查看终端并稍后重试。"
            )
            self._last_sync_issue = message
            self.sync_failed.emit(message, manual)
        self.state_changed.emit()

        if pending and self._settings.enabled:
            if allow_reschedule:
                self._launch_worker(manual=pending_manual)
            else:
                self._pending = True
                self._pending_manual = pending_manual
        return True

    def shutdown(self, timeout_ms: int = 10_000) -> bool:
        """停止通知与定时器，并在限时内完成退出前最后一次对账。"""

        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int):
            raise TypeError("timeout_ms 必须是整数")
        if timeout_ms < 0:
            raise ValueError("timeout_ms 不能为负数")
        self.timer.stop()
        unsubscribe = self._unsubscribe
        self._unsubscribe = None
        if unsubscribe is not None:
            unsubscribe()
        self._stopping = True

        # Runtime 会先等待保存、编辑和删除任务完成，再调用这里，因此这一轮能
        # 覆盖最后一笔已提交数据库的变化。
        if self._settings.enabled and self._configuration_error is None:
            self._pending = True

        deadline = monotonic() + timeout_ms / 1000.0
        while True:
            worker = self._worker
            if worker is None:
                if not self._pending:
                    break
                self._pending = False
                self._pending_manual = False
                if not self._launch_worker(manual=False):
                    return False
                worker = self._worker
            if worker is None:
                break
            remaining_ms = max(0, int((deadline - monotonic()) * 1000))
            if not worker.wait(remaining_ms):
                return False
            self._settle_worker(worker, allow_reschedule=False)
            if monotonic() >= deadline and self._pending:
                return False

        self._started = False
        self._stopping = False
        return True


__all__ = [
    "DEFAULT_RECONCILE_INTERVAL_MS",
    "MAX_SYNC_CARDS",
    "ObsidianArchiveManager",
]
