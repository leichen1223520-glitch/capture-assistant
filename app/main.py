"""Windows 桌面端入口、托盘生命周期与一次主动采集编排。"""

from __future__ import annotations

from collections.abc import Callable
import logging
from pathlib import Path
import sys
from threading import Lock
from typing import Protocol

from PIL import Image
from PySide6.QtCore import QObject, QRect, QRunnable, QThreadPool, QUrl, Signal, Slot, Qt
from PySide6.QtGui import QAction, QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QMenu,
    QMessageBox,
    QStyle,
    QSystemTrayIcon,
)

from .bridge import (
    BrowserBridgeError,
    BrowserContext,
    get_browser_context,
    start_browser_bridge,
    stop_browser_bridge,
)
from .capture import (
    CaptureError,
    CaptureMeta,
    ForegroundWindowSnapshot,
    enable_per_monitor_dpi_awareness,
    foreground_app_name,
    foreground_window_intersects_capture,
    foreground_window_snapshot,
    grab_active_monitor,
)
from .config import API_PORT, DATA_DIR, DB_PATH, HOTKEY, SCREENSHOT_DIR, ensure_data_dirs
from .hotkey import HotkeyError, HotkeyManager
from .inbox import CandidateInbox
from .inbox_window import CandidateInboxWindow
from .library import LibraryWindow
from .models import Card
from .observation import ObservationCoordinator
from .ocr import OCRError
from .overlay import OverlayError, select_region
from .pipeline import PipelineError, PreparedCard, prepare_card_from_selection
from .review import CardReviewDialog
from .safety import (
    capture_block_reason,
    is_chromium_application,
    normalize_process_name,
)
from .server import LocalApiServer
from .store import Store, StoreError

LOGGER = logging.getLogger(__name__)

_KNOWN_OPERATION_ERRORS = (
    BrowserBridgeError,
    CaptureError,
    HotkeyError,
    OCRError,
    OverlayError,
    PipelineError,
    StoreError,
)


class _TrayLike(Protocol):
    def showMessage(
        self,
        title: str,
        message: str,
        icon: QSystemTrayIcon.MessageIcon = QSystemTrayIcon.MessageIcon.NoIcon,
        msecs: int = 10_000,
    ) -> None: ...


class _ThreadPoolLike(Protocol):
    def start(self, runnable: QRunnable, priority: int = 0) -> None: ...

    def clear(self) -> None: ...

    def waitForDone(self, msecs: int = -1) -> bool: ...


class _ApiServerLike(Protocol):
    def start(self, timeout: float = 5.0) -> object: ...

    def stop(self, timeout: float = 5.0) -> None: ...


class _CardBuildSignals(QObject):
    """把工作线程结果安全送回 Qt GUI 线程。"""

    succeeded = Signal(object, object)
    failed = Signal(object, object)


def _contexts_match(
    before: BrowserContext | None,
    after: BrowserContext | None,
) -> bool:
    """捕获前后的页面身份与选中文字都稳定时返回 ``True``。"""

    if before is None or after is None:
        return False
    return (
        not before.sensitive_input
        and not after.sensitive_input
        and before.url == after.url
        and before.title == after.title
        and before.selection == after.selection
    )


class CardBuildWorker(QRunnable):
    """在 Qt 线程池执行一次可能包含离线 OCR 的卡片流水线。"""

    def __init__(
        self,
        frozen_image: Image.Image,
        meta: CaptureMeta,
        rect: QRect,
        *,
        browser_context: BrowserContext | None,
        data_dir: Path,
        screenshot_dir: Path,
        builder: Callable[..., PreparedCard] = prepare_card_from_selection,
    ) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self.signals = _CardBuildSignals()
        self.frozen_image: Image.Image | None = frozen_image
        self.meta = meta
        self.rect = QRect(rect)
        self.browser_context = browser_context
        self.data_dir = data_dir
        self.screenshot_dir = screenshot_dir
        self.builder = builder
        self.prepared: PreparedCard | None = None
        self.error: Exception | None = None
        self._state_lock = Lock()
        self._abandoned = False

    @Slot()
    def run(self) -> None:
        """构建卡片；所有异常都作为数据返回，避免工作线程无声终止。"""

        try:
            frozen_image = self.frozen_image
            if frozen_image is None:
                raise PipelineError("后台捕获画面已释放，未生成卡片。")
            prepared = self.builder(
                frozen_image,
                self.meta,
                self.rect,
                context_provider=lambda: self.browser_context,
                data_dir=self.data_dir,
                screenshot_dir=self.screenshot_dir,
            )
            if not isinstance(prepared, PreparedCard):
                raise TypeError("卡片流水线必须返回 PreparedCard。")
        except Exception as exc:  # 线程边界必须把意外故障交还主线程并触发清理
            self.error = exc
            self._release_capture_inputs()
            with self._state_lock:
                if self._abandoned:
                    self.error = None
                else:
                    self.signals.failed.emit(self, exc)
            return
        self._release_capture_inputs()
        with self._state_lock:
            if self._abandoned:
                prepared.close()
                return
            self.prepared = prepared
            # 在锁内排入 Qt 队列，确保 shutdown 要么先标记 abandoned，要么能
            # 在 waitForDone 成功后从 worker 取得并释放这个候选。
            self.signals.succeeded.emit(self, prepared)

    def _release_capture_inputs(self) -> None:
        """识别结束后立即释放原始整屏帧与浏览器正文副本。"""

        frozen_image = self.frozen_image
        self.frozen_image = None
        if frozen_image is not None:
            frozen_image.close()
        self.browser_context = None

    def release_sensitive_resources(self) -> None:
        """释放工作对象持有的整屏像素、DOM 正文和卡片副本。"""

        self._release_capture_inputs()
        with self._state_lock:
            prepared = self.prepared
            self.prepared = None
            self.error = None
        if isinstance(prepared, PreparedCard):
            prepared.close()

    def abandon_result(self) -> None:
        """通知运行中的任务：完成后在工作线程释放结果，不再依赖 GUI 队列。"""

        with self._state_lock:
            self._abandoned = True


class CaptureCoordinator(QObject):
    """串联敏感检查、抓帧、框选、后台识别和人工审核。"""

    def __init__(
        self,
        store: Store,
        tray_icon: _TrayLike,
        *,
        data_dir: str | Path = DATA_DIR,
        screenshot_dir: str | Path = SCREENSHOT_DIR,
        foreground_provider: Callable[[], str | None] = foreground_app_name,
        window_provider: Callable[[], ForegroundWindowSnapshot | None] | None = None,
        context_provider: Callable[[float], BrowserContext | None] = get_browser_context,
        capture_provider: Callable[[], tuple[Image.Image, CaptureMeta]] = grab_active_monitor,
        selection_provider: Callable[..., QRect | None] = select_region,
        card_builder: Callable[..., PreparedCard] = prepare_card_from_selection,
        review_factory: Callable[..., CardReviewDialog] = CardReviewDialog,
        thread_pool: _ThreadPoolLike | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.store = store
        self.tray_icon = tray_icon
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.screenshot_dir = Path(screenshot_dir).expanduser().resolve()
        self.foreground_provider = foreground_provider
        if window_provider is not None:
            self.window_provider = window_provider
        elif foreground_provider is foreground_app_name:
            self.window_provider = foreground_window_snapshot
        else:
            # 注入测试或替代前台进程读取器时，不混入真实桌面窗口状态。
            self.window_provider = lambda: None
        self.context_provider = context_provider
        self.capture_provider = capture_provider
        self.selection_provider = selection_provider
        self.card_builder = card_builder
        self.review_factory = review_factory
        self.thread_pool = thread_pool or QThreadPool(self)
        if isinstance(self.thread_pool, QThreadPool):
            self.thread_pool.setMaxThreadCount(1)

        self._busy = False
        self._shutting_down = False
        self._workers: set[CardBuildWorker] = set()
        self._active_dialog: CardReviewDialog | None = None

    @property
    def busy(self) -> bool:
        """捕获、识别或审核尚未完成时返回 ``True``。"""

        return self._busy

    @Slot()
    def request_capture(self) -> None:
        """由全局快捷键或托盘菜单触发一次用户主动采集。"""

        if self._shutting_down:
            return
        if self._busy:
            self._notify("正在处理", "请先完成当前框选或卡片审核。")
            return

        self._busy = True
        worker_started = False
        frozen_image: Image.Image | None = None
        worker: CardBuildWorker | None = None
        try:
            initial_window = self.window_provider()
            initial_app = (
                initial_window.app_name
                if initial_window is not None and initial_window.app_name is not None
                else self.foreground_provider()
            )
            reason = capture_block_reason(initial_app)
            if reason is not None:
                self._notify("已暂停捕获", reason, warning=True)
                return

            browser_context = self._browser_context()
            reason = capture_block_reason(
                initial_app,
                browser_sensitive_input=bool(
                    browser_context is not None and browser_context.sensitive_input
                ),
            )
            if reason is not None:
                self._notify("已暂停捕获", reason, warning=True)
                return

            frozen_image, meta = self.capture_provider()
            reason = capture_block_reason(meta.app_name)
            if reason is not None:
                self._notify("已暂停捕获", f"{reason} 画面未保存。", warning=True)
                return

            # 截图后、浮层取得焦点前复查一次。任一次检测到敏感输入都立即释放
            # 内存帧；页面或标签在两次查询间变化时不复用旧 DOM 元数据。
            window_after_capture = self.window_provider()
            context_after_capture = self._browser_context()
            reason = capture_block_reason(
                meta.app_name,
                browser_sensitive_input=bool(
                    context_after_capture is not None
                    and context_after_capture.sensitive_input
                ),
            )
            if reason is not None:
                self._notify("已暂停捕获", f"{reason} 画面未保存。", warning=True)
                return

            context_degraded_reason: str | None = None
            if not _contexts_match(browser_context, context_after_capture):
                if browser_context is None or context_after_capture is None:
                    context_degraded_reason = (
                        "扩展未响应，无法确认浏览器密码、验证码或支付输入状态"
                    )
                else:
                    context_degraded_reason = "页面状态发生变化"
                browser_context = None

            if (
                browser_context is not None
                and initial_window is not None
                and window_after_capture is not None
                and initial_window.handle != window_after_capture.handle
            ):
                browser_context = None
                context_degraded_reason = "捕获期间切换了浏览器窗口"

            capture_window = window_after_capture or initial_window
            if (
                browser_context is not None
                and capture_window is not None
                and not foreground_window_intersects_capture(capture_window, meta)
            ):
                browser_context = None
                context_degraded_reason = "前台浏览器与鼠标所在显示器不一致"

            if context_degraded_reason and (
                is_chromium_application(meta.app_name)
                or is_chromium_application(initial_app)
            ):
                self._notify(
                    "浏览器保护已降级",
                    f"{context_degraded_reason}；本次不使用网页原文，只对框选区域离线 OCR。",
                    warning=True,
                )

            if (
                initial_app is not None
                and meta.app_name is not None
                and normalize_process_name(initial_app)
                != normalize_process_name(meta.app_name)
            ):
                # 在检查与抓帧之间发生了前台应用切换，宁可失去 DOM 元数据也不
                # 把另一个浏览器窗口的选区错误关联到当前截图。
                browser_context = None

            rect = self.selection_provider(frozen_image, capture_meta=meta)
            if rect is None:
                self._notify("已取消", "本次框选已取消，没有保存任何内容。")
                return

            worker = CardBuildWorker(
                frozen_image,
                meta,
                rect,
                browser_context=browser_context,
                data_dir=self.data_dir,
                screenshot_dir=self.screenshot_dir,
                builder=self.card_builder,
            )
            # 直接连接到属于主线程的 QObject 槽，确保后台 OCR 完成后不会在
            # 工作线程里创建或操作任何 Qt 窗口。
            worker.signals.succeeded.connect(
                self._on_card_ready,
                Qt.ConnectionType.QueuedConnection,
            )
            worker.signals.failed.connect(
                self._on_card_failed,
                Qt.ConnectionType.QueuedConnection,
            )
            self._workers.add(worker)
            self._notify("正在识别", "正在本机整理文字和截图，请稍候。")
            self.thread_pool.start(worker)
            worker_started = True
            frozen_image = None
        except _KNOWN_OPERATION_ERRORS as exc:
            self._notify("无法完成捕获", str(exc), warning=True)
        except (OSError, RuntimeError, TypeError, ValueError):
            LOGGER.exception("主动捕获流程发生未预期错误（未记录屏幕正文）")
            self._notify("无法完成捕获", "发生未预期的本地错误，请查看终端。", warning=True)
        finally:
            if not worker_started:
                if worker is not None:
                    self._workers.discard(worker)
                    worker.release_sensitive_resources()
                    frozen_image = None
                if frozen_image is not None:
                    frozen_image.close()
                self._busy = False

    def _browser_context(self) -> BrowserContext | None:
        """在任何应用浮层取得焦点之前读取浏览器上下文。"""

        try:
            return self.context_provider(0.3)
        except (OSError, RuntimeError):
            return None

    @Slot(object, object)
    def _on_card_ready(self, worker: CardBuildWorker, candidate: object) -> None:
        if not isinstance(candidate, PreparedCard):
            self._on_card_failed(worker, TypeError("流水线返回了无效内存候选"))
            return
        if self._shutting_down:
            self._finish_worker(worker)
            return

        card = candidate.card
        dialog: CardReviewDialog | None = None
        preserve_for_save = False
        try:
            dialog = self.review_factory(
                card,
                self.store,
                data_dir=self.data_dir,
                selected_image=candidate.selected_image,
                full_image=candidate.full_image,
                selected_preview_png=candidate.selected_preview_png or None,
                full_preview_png=candidate.full_preview_png or None,
            )
            self._active_dialog = dialog
            dialog.exec()
            if not self._wait_for_review_save(dialog, 10_000):
                preserve_for_save = True
                notification = (
                    "仍在安全保存",
                    "退出等待保存超时；程序不会并发删除或释放这张卡片。",
                    True,
                )
            elif bool(getattr(dialog, "finalized", dialog.saved) and dialog.saved):
                notification = ("卡片已保存", "观点卡片已保存到本机资料库。", False)
            elif bool(getattr(dialog, "discarded", not dialog.saved)):
                notification = (
                    "卡片已丢弃",
                    "未保留数据库记录或截图。",
                    False,
                )
            elif self._delete_draft(card):
                notification = (
                    "卡片已丢弃",
                    "保存阶段的未完成草稿已由主程序清理。",
                    False,
                )
            else:
                notification = (
                    "审核状态异常",
                    "无法确认候选卡片是否已完整清理，请不要继续采集并检查终端。",
                    True,
                )
        except Exception:  # GUI 生命周期边界：任何失败都必须清理已落盘候选卡片
            LOGGER.exception("审核窗口失败（未记录卡片正文）")
            if dialog is not None and not self._wait_for_review_save(dialog, 10_000):
                preserve_for_save = True
                notification = (
                    "审核退出异常",
                    "后台保存尚未结束；程序已保留内存证据，未与保存线程并发清理。",
                    True,
                )
            else:
                # 正常审核前没有磁盘草稿；若异常发生在保存提交阶段，只允许
                # 删除仍为 draft 的记录，正式卡片永远不受异常清理影响。
                cleanup_succeeded = self._delete_draft(card)
                notification = (
                    "审核失败",
                    (
                        "审核窗口无法完成，保存阶段草稿已清理。"
                        if cleanup_succeeded
                        else "审核窗口无法完成；未发现可清理草稿或清理失败，请检查终端。"
                    ),
                    True,
                )
        finally:
            if not preserve_for_save:
                self._active_dialog = None
                self._finish_worker(worker)
        try:
            self._notify(notification[0], notification[1], warning=notification[2])
        except Exception:
            # 通知只是附属 UI；正式卡片已经保存后，通知故障绝不能触发数据删除。
            LOGGER.exception("无法显示审核结果通知")

    @staticmethod
    def _wait_for_review_save(dialog: object, timeout_ms: int) -> bool:
        """若审核窗正在提交，等待它在同一线程安全归并后台结果。"""

        if not bool(getattr(dialog, "saving", False)):
            return True
        waiter = getattr(dialog, "wait_for_save", None)
        if not callable(waiter):
            return False
        try:
            return bool(waiter(timeout_ms))
        except Exception:
            LOGGER.exception("等待审核保存任务时发生错误")
            return False

    @Slot(object, object)
    def _on_card_failed(self, worker: CardBuildWorker, error: object) -> None:
        if self._shutting_down:
            self._finish_worker(worker)
            return
        if isinstance(error, _KNOWN_OPERATION_ERRORS):
            message = str(error)
        else:
            LOGGER.error("后台卡片流水线发生未预期错误：%s", type(error).__name__)
            message = "本地识别或保存发生未预期错误。"
        self._notify("没有生成卡片", message, warning=True)
        self._finish_worker(worker)

    def _finish_worker(self, worker: CardBuildWorker) -> None:
        self._workers.discard(worker)
        worker.release_sensitive_resources()
        if not self._workers and self._active_dialog is None:
            self._busy = False

    def _delete_draft(self, card: Card) -> bool:
        try:
            return self.store.delete_draft(card.id)
        except StoreError:
            LOGGER.exception("无法清理未审核候选卡片 %s", card.id)
            return False

    def _notify(self, title: str, message: str, *, warning: bool = False) -> None:
        icon = (
            QSystemTrayIcon.MessageIcon.Warning
            if warning
            else QSystemTrayIcon.MessageIcon.Information
        )
        self.tray_icon.showMessage(title, message, icon, 5_000)

    def shutdown(self, timeout_ms: int = 10_000) -> bool:
        """停止接收新捕获并清理尚未进入审核的候选卡片。

        退出应用时最多等待指定时间。审核前候选只在内存，因此超时不会留下数据库
        记录或截图；本方法返回 ``False`` 并保留工作对象，供进程结束时释放。
        """

        self._shutting_down = True
        if self._active_dialog is not None:
            if not self._wait_for_review_save(self._active_dialog, timeout_ms):
                LOGGER.error("退出等待审核保存任务超时")
                return False
            if not self._active_dialog.saved:
                self._active_dialog.reject()
            finalized = bool(
                getattr(self._active_dialog, "finalized", self._active_dialog.saved)
            )
            if not finalized:
                LOGGER.error("退出时无法清理审核窗口中的保存阶段草稿")
                return False
            self._active_dialog = None
        for worker in tuple(self._workers):
            worker.abandon_result()
        self.thread_pool.clear()
        completed = self.thread_pool.waitForDone(timeout_ms)
        if not completed:
            return False
        for worker in tuple(self._workers):
            self._workers.discard(worker)
            worker.release_sensitive_resources()
        self._busy = False
        return completed


class DesktopRuntime(QObject):
    """拥有托盘、快捷键、浏览器桥和采集协调器的应用生命周期。"""

    def __init__(
        self,
        application: QApplication,
        *,
        data_dir: str | Path = DATA_DIR,
        screenshot_dir: str | Path = SCREENSHOT_DIR,
        db_path: str | Path = DB_PATH,
        bridge_start: Callable[..., None] = start_browser_bridge,
        bridge_stop: Callable[..., None] = stop_browser_bridge,
        api_server_factory: Callable[[Store], _ApiServerLike] = LocalApiServer,
        library_factory: Callable[..., LibraryWindow] = LibraryWindow,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.application = application
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.screenshot_dir = Path(screenshot_dir).expanduser().resolve()
        self.store = Store(db_path=db_path, data_dir=self.data_dir)
        self.inbox = CandidateInbox()
        self.bridge_start = bridge_start
        self.bridge_stop = bridge_stop
        self.api_server_factory = api_server_factory
        self.library_factory = library_factory
        self.api_server: _ApiServerLike | None = None
        self._library_window: LibraryWindow | None = None
        self._inbox_window: CandidateInboxWindow | None = None
        self.hotkey = HotkeyManager(HOTKEY, parent=self)
        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setToolTip("本地屏幕内容与观点采集助手")
        self.tray_icon.setIcon(
            application.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
        )
        self.menu = QMenu()
        self.observe_start_action = QAction("开始半自动观察……", self.menu)
        self.observe_stop_action = QAction("停止半自动观察", self.menu)
        self.observe_stop_action.setEnabled(False)
        self.inbox_action = QAction("候选收件箱（0）", self.menu)
        self.capture_action = QAction(f"开始捕获（{HOTKEY}）", self.menu)
        self.search_action = QAction("打开观点库", self.menu)
        self.quit_action = QAction("退出", self.menu)
        self.readonly_search_action = QAction("打开只读检索网页", self.menu)
        self.menu.addAction(self.observe_start_action)
        self.menu.addAction(self.observe_stop_action)
        self.menu.addAction(self.inbox_action)
        self.menu.addSeparator()
        self.menu.addAction(self.capture_action)
        self.menu.addAction(self.search_action)
        self.menu.addAction(self.readonly_search_action)
        self.menu.addSeparator()
        self.menu.addAction(self.quit_action)
        self.tray_icon.setContextMenu(self.menu)

        self.coordinator = CaptureCoordinator(
            self.store,
            self.tray_icon,
            data_dir=self.data_dir,
            screenshot_dir=self.screenshot_dir,
            parent=self,
        )
        self.observation = ObservationCoordinator(
            self.tray_icon,
            self._offer_observation_candidate,
            data_dir=self.data_dir,
            screenshot_dir=self.screenshot_dir,
            parent=self,
        )
        self.hotkey.activated.connect(self._request_capture)
        self.capture_action.triggered.connect(self._request_capture)
        self.observe_start_action.triggered.connect(self._request_observation_start)
        self.observe_stop_action.triggered.connect(self.observation.request_stop)
        self.observation.state_changed.connect(self._observation_state_changed)
        self.observation.candidate_added.connect(self._refresh_inbox)
        self.inbox_action.triggered.connect(self._show_inbox)
        self.search_action.triggered.connect(self._show_library)
        self.readonly_search_action.triggered.connect(self._open_readonly_search)
        self.quit_action.triggered.connect(self.request_quit)
        self.tray_icon.activated.connect(self._tray_activated)
        self.application.aboutToQuit.connect(self.shutdown)
        self._started = False
        self._stopped = False

    @Slot()
    def _request_capture(self) -> None:
        if self.observation.active or self.observation.busy:
            self._notify(
                "观察尚未完全停止",
                "请先停止半自动观察，并等待当前本地检查结束后再主动捕获。",
                warning=True,
            )
            return
        self.coordinator.request_capture()

    @Slot()
    def _request_observation_start(self) -> None:
        if self.coordinator.busy:
            self._notify(
                "暂不能开始观察",
                "请先完成当前框选、识别或卡片审核。",
                warning=True,
            )
            return
        self.observation.request_start()

    def _offer_observation_candidate(
        self,
        prepared: PreparedCard,
        *,
        session_id: str,
        source_key: str,
        region_key: str,
        seen_at: float,
    ) -> bool:
        result = self.inbox.offer(
            prepared,
            session_id=session_id,
            source_key=source_key,
            region_key=region_key,
            now=seen_at,
        )
        # inbox.offer 是不可逆的所有权移交点。此后的 GUI 刷新或托盘通知即使
        # 失败，也不能让上层误以为移交失败并关闭收件箱仍持有的 PreparedCard。
        try:
            self._refresh_inbox()
            if result.evicted_entry_ids:
                self._notify(
                    "收件箱已达到上限",
                    "已从内存丢弃最旧的未审核候选；没有删除已保存的卡片。",
                    warning=True,
                )
            elif result.status == "too_large":
                self._notify(
                    "候选未加入收件箱",
                    "这张候选超过内存上限，已安全释放且没有落盘。",
                    warning=True,
                )
        except Exception:
            LOGGER.exception("候选已经安全移交，但收件箱界面刷新失败（未记录正文）")
        return result.status == "added"

    @Slot(bool)
    def _observation_state_changed(self, active: bool) -> None:
        self.observe_start_action.setEnabled(not active)
        self.observe_stop_action.setEnabled(active)
        self.capture_action.setEnabled(not active)

    @Slot()
    def _refresh_inbox(self) -> None:
        count = len(self.inbox)
        self.inbox_action.setText(f"候选收件箱（{count}）")
        window = self._inbox_window
        if window is not None and window.isVisible():
            window.refresh()

    @Slot(int)
    def _set_inbox_count(self, count: int) -> None:
        self.inbox_action.setText(f"候选收件箱（{count}）")

    @Slot()
    def _show_inbox(self) -> None:
        try:
            if self._inbox_window is None:
                self._inbox_window = CandidateInboxWindow(
                    self.inbox,
                    self.store,
                    data_dir=self.data_dir,
                )
                self._inbox_window.count_changed.connect(self._set_inbox_count)
            window = self._inbox_window
            window.refresh()
            window.show()
            window.raise_()
            window.activateWindow()
        except (OSError, RuntimeError, TypeError, ValueError):
            LOGGER.exception("无法打开候选收件箱（未记录候选正文）")
            self._notify(
                "无法打开候选收件箱",
                "本地候选窗口暂时无法打开，请查看终端后重试。",
                warning=True,
            )

    def _notify(self, title: str, message: str, *, warning: bool = False) -> None:
        icon = (
            QSystemTrayIcon.MessageIcon.Warning
            if warning
            else QSystemTrayIcon.MessageIcon.Information
        )
        self.tray_icon.showMessage(title, message, icon, 6_000)

    def start(self) -> None:
        """初始化数据库、桥、只读服务与热键，失败时回滚已启动资源。"""

        if self._started:
            return
        self.store.init_db()
        removed_drafts = self.store.cleanup_drafts()
        if removed_drafts:
            LOGGER.info("启动时清理了 %d 张未审核候选卡片", removed_drafts)
        bridge_started = False
        try:
            self.bridge_start()
            bridge_started = True
            self.api_server = self.api_server_factory(self.store)
            self.api_server.start()
            self.hotkey.start()
        except Exception:
            if self.api_server is not None:
                try:
                    self.api_server.stop()
                except RuntimeError:
                    LOGGER.exception("启动回滚时无法停止本机只读检索服务")
                else:
                    self.api_server = None
            if bridge_started:
                try:
                    self.bridge_stop()
                except BrowserBridgeError:
                    LOGGER.exception("启动回滚时无法停止浏览器桥")
            raise

        self.tray_icon.show()
        self.tray_icon.showMessage(
            "采集助手已启动",
            f"按 {HOTKEY} 主动框选，或从托盘开始半自动观察。",
            QSystemTrayIcon.MessageIcon.Information,
            6_000,
        )
        self._started = True
        self._stopped = False

    @Slot()
    def request_quit(self) -> None:
        inbox_window = self._inbox_window
        if inbox_window is not None and inbox_window.busy:
            self._notify(
                "候选仍在审核",
                "请先在审核窗口保存或丢弃当前候选，再退出程序。",
                warning=True,
            )
            return
        if self.observation.busy:
            if self.observation.active:
                self.observation.request_stop()
            self._notify(
                "观察仍在收尾",
                "已停止继续观察；请等待当前本地检查或证据抓取结束后再退出程序。",
                warning=True,
            )
            return
        if self.coordinator.busy:
            self._notify(
                "暂不能退出",
                "请先完成当前识别或在审核窗口保存/丢弃卡片。",
                warning=True,
            )
            return
        library = self._library_window
        if library is not None and library.busy:
            self._notify(
                "观点库仍在处理",
                "请等待当前保存、删除或导出结束后再退出。",
                warning=True,
            )
            return
        if len(self.inbox) > 0:
            answer = QMessageBox.question(
                None,
                "仍有未审核候选",
                "退出会从内存丢弃所有未审核候选，已保存卡片不受影响。确定退出吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        if library is not None and not library.close():
            return
        if self.observation.active:
            self.observation.request_stop()
        if len(self.inbox) > 0:
            self.inbox.discard_all()
            self._refresh_inbox()
        self.application.quit()

    @Slot(QSystemTrayIcon.ActivationReason)
    def _tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._request_capture()

    @Slot()
    def _show_library(self) -> None:
        try:
            if self._library_window is None:
                self._library_window = self.library_factory(
                    self.store,
                    data_dir=self.data_dir,
                )
            window = self._library_window
            if not window.busy:
                window.request_refresh()
            window.show()
            window.raise_()
            window.activateWindow()
        except (
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            LOGGER.warning(
                "无法打开本地观点库：%s",
                type(exc).__name__,
            )
            self._notify(
                "无法打开观点库",
                "本地观点库暂时无法打开，请查看终端后重试。",
                warning=True,
            )

    @Slot()
    def _open_readonly_search(self) -> None:
        server = self.api_server
        if (
            server is None
            or not bool(getattr(server, "running", False))
        ):
            self._notify(
                "只读检索尚未启动",
                "请重新启动采集助手后再打开本地检索网页。",
                warning=True,
            )
            return
        url = QUrl(f"http://127.0.0.1:{API_PORT}/")
        if not QDesktopServices.openUrl(url):
            self._notify(
                "无法打开只读网页",
                "系统浏览器未能打开本机观点库地址。",
                warning=True,
            )

    @Slot()
    def shutdown(self) -> None:
        """按安全顺序停止观察、采集、内存候选与所有本机服务。"""

        if self._stopped:
            return
        self._stopped = True
        observation_stopped = self.observation.shutdown()
        if not observation_stopped:
            LOGGER.warning("退出前未能在限定时间内结束观察工作线程")
        workers_stopped = self.coordinator.shutdown()
        if not workers_stopped:
            LOGGER.warning(
                "退出前未能完整结束本地识别或清理保存阶段草稿；"
                "审核前候选仍只在内存，异常草稿会在下次启动时再次清理"
            )
        library = self._library_window
        if library is not None:
            if not library.wait_for_idle(10_000):
                LOGGER.warning("退出前未能在限定时间内结束观点库后台任务")
            else:
                library.hide()
                self._library_window = None
        inbox_window = self._inbox_window
        if inbox_window is not None:
            if inbox_window.shutdown():
                self._inbox_window = None
            else:
                LOGGER.warning("退出前未能安全结束候选审核窗口")
        self.inbox.close()

        if self.hotkey.is_started:
            try:
                self.hotkey.stop()
            except HotkeyError:
                LOGGER.exception("退出时无法注销全局快捷键")
        if self.api_server is not None:
            try:
                self.api_server.stop()
            except RuntimeError:
                LOGGER.exception("退出时无法停止本机只读检索服务")
            finally:
                self.api_server = None

        if self._started:
            try:
                self.bridge_stop()
            except BrowserBridgeError:
                LOGGER.exception("退出时无法停止浏览器桥")
        self.tray_icon.hide()
        self._started = False


def _create_application(argv: list[str]) -> QApplication:
    """在创建任何窗口前配置 Windows DPI 与 Qt 应用属性。"""

    enable_per_monitor_dpi_awareness()
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    application = QApplication(argv)
    application.setApplicationName("本地屏幕内容与观点采集助手")
    application.setQuitOnLastWindowClosed(False)
    return application


def main(argv: list[str] | None = None) -> int:
    """启动 T8 桌面应用并返回 Qt 退出码。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    application = _create_application(list(sys.argv if argv is None else argv))
    if not QSystemTrayIcon.isSystemTrayAvailable():
        LOGGER.error("当前 Windows 会话没有可用系统托盘")
        return 1

    data_dir, screenshot_dir = ensure_data_dirs()
    runtime = DesktopRuntime(
        application,
        data_dir=data_dir,
        screenshot_dir=screenshot_dir,
        db_path=DB_PATH,
    )
    try:
        runtime.start()
    except _KNOWN_OPERATION_ERRORS as exc:
        QMessageBox.critical(None, "启动失败", str(exc))
        runtime.shutdown()
        return 1
    except (OSError, RuntimeError, TypeError, ValueError):
        LOGGER.exception("桌面应用启动发生未预期错误")
        QMessageBox.critical(None, "启动失败", "发生未预期的本地错误，请查看终端。")
        runtime.shutdown()
        return 1

    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
