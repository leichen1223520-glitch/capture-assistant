"""用户显式启动的半自动浏览器观察会话。

首版只接收 Chrome 扩展提供的选中文字与已启用 HTML5 字幕轨道。观察器在同一
候选连续稳定出现后，才抓取用户预先框定的区域；候选交给纯内存收件箱，模块自身
不写数据库、截图或日志正文。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
import hashlib
import time
from pathlib import Path
from threading import Lock
from typing import Protocol
import unicodedata
from uuid import uuid4

from PIL import Image
from PySide6.QtCore import QObject, QRect, QRunnable, QThreadPool, QTimer, Signal, Slot, Qt
from PySide6.QtWidgets import QSystemTrayIcon

from .bridge import BrowserContext, get_browser_context
from .capture import (
    CaptureMeta,
    ForegroundWindowSnapshot,
    foreground_window_intersects_capture,
    foreground_window_snapshot,
    grab_active_monitor,
    grab_monitor,
)
from .config import DATA_DIR, SCREENSHOT_DIR
from .overlay import select_region
from .pipeline import PreparedCard, prepare_card_from_selection
from .safety import capture_block_reason, is_chromium_application


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


CandidateSink = Callable[..., bool]
ContextProvider = Callable[[float], BrowserContext | None]
CaptureProvider = Callable[[CaptureMeta], tuple[Image.Image, CaptureMeta]]
InitialCaptureProvider = Callable[[], tuple[Image.Image, CaptureMeta]]
WindowProvider = Callable[[], ForegroundWindowSnapshot | None]
SelectionProvider = Callable[..., QRect | None]
CardBuilder = Callable[..., PreparedCard]


class _ObservationSafetyError(RuntimeError):
    """观察源身份或敏感状态失去确认时中止当前会话。"""


@dataclass(frozen=True, slots=True)
class ObservationSession:
    """一次明确授权的标签页、窗口、显示器与固定选区。"""

    session_id: str
    tab_id: int
    window_handle: int
    window_bounds: tuple[int, int, int, int]
    monitor_meta: CaptureMeta
    region: QRect
    region_key: str


@dataclass(frozen=True, slots=True)
class _CandidateMeta:
    session_id: str
    source_key: str
    region_key: str
    signature: str
    seen_at: float


def _normalized_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _source_key(context: BrowserContext) -> str:
    """生成仅在当前进程内使用的来源键，不写日志或磁盘。"""

    material = f"{context.tab_id}\x00{context.url}\x00{context.video_key}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _signature(context: BrowserContext) -> str:
    return (
        f"{_source_key(context)}\x00{context.observation_kind}\x00"
        f"{_normalized_text(context.observation_text)}"
    )


class _WorkerSignals(QObject):
    succeeded = Signal(object, object)
    failed = Signal(object, object)


class _ProbeWorker(QRunnable):
    def __init__(self, context_provider: ContextProvider, timeout: float = 0.4) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self.signals = _WorkerSignals()
        self.context_provider = context_provider
        self.timeout = timeout

    @Slot()
    def run(self) -> None:
        try:
            context = self.context_provider(self.timeout)
        except Exception as exc:  # QRunnable 边界不能让异常逃逸并卡死 busy 状态
            self.signals.failed.emit(self, exc)
            return
        self.signals.succeeded.emit(self, context)


class _CaptureWorker(QRunnable):
    """复查同一浏览器状态并在工作线程构造纯内存候选。"""

    def __init__(
        self,
        session: ObservationSession,
        expected_signature: str,
        *,
        context_provider: ContextProvider,
        window_provider: WindowProvider,
        capture_provider: CaptureProvider,
        card_builder: CardBuilder,
        data_dir: Path,
        screenshot_dir: Path,
    ) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self.signals = _WorkerSignals()
        self.session = session
        self.expected_signature = expected_signature
        self.context_provider = context_provider
        self.window_provider = window_provider
        self.capture_provider = capture_provider
        self.card_builder = card_builder
        self.data_dir = data_dir
        self.screenshot_dir = screenshot_dir
        self._lock = Lock()
        self._abandoned = False
        self._prepared: PreparedCard | None = None

    @Slot()
    def run(self) -> None:
        frozen: Image.Image | None = None
        prepared: PreparedCard | None = None
        try:
            before = self.window_provider()
            if (
                before is None
                or before.handle != self.session.window_handle
                or before.bounds != self.session.window_bounds
            ):
                self.signals.succeeded.emit(self, None)
                return
            context = self.context_provider(0.4)
            if (
                context is None
                or context.sensitive_input
                or context.tab_id != self.session.tab_id
            ):
                raise _ObservationSafetyError("观察源安全状态发生变化")
            if _signature(context) != self.expected_signature:
                self.signals.succeeded.emit(self, None)
                return
            frozen, meta = self.capture_provider(self.session.monitor_meta)
            # 抓屏后立即复查扩展状态。若在第一次响应与抓帧之间进入了密码、
            # 验证码、支付输入或切换了标签/字幕，本帧只在内存中立即释放。
            context_after = self.context_provider(0.4)
            after = self.window_provider()
            if (
                context_after is None
                or context_after.sensitive_input
                or context_after.tab_id != self.session.tab_id
            ):
                raise _ObservationSafetyError("抓屏后观察源安全状态发生变化")
            if (
                _signature(context_after) != self.expected_signature
                or after is None
                or after.handle != self.session.window_handle
                or after.bounds != self.session.window_bounds
                or not foreground_window_intersects_capture(after, meta)
                or not is_chromium_application(meta.app_name)
            ):
                self.signals.succeeded.emit(self, None)
                return

            evidence_context = replace(
                context_after,
                selection=context_after.observation_text,
            )
            prepared = self.card_builder(
                frozen,
                meta,
                self.session.region,
                context_provider=lambda: evidence_context,
                data_dir=self.data_dir,
                screenshot_dir=self.screenshot_dir,
            )
            if not isinstance(prepared, PreparedCard):
                raise TypeError("观察流水线必须返回 PreparedCard。")
            metadata = _CandidateMeta(
                session_id=self.session.session_id,
                source_key=_source_key(context_after),
                region_key=self.session.region_key,
                signature=self.expected_signature,
                seen_at=time.monotonic(),
            )
            abandoned = False
            with self._lock:
                if self._abandoned:
                    abandoned = True
                else:
                    self._prepared = prepared
            if abandoned:
                prepared.close()
                prepared = None
                self.signals.succeeded.emit(self, None)
                return
            prepared = None
            self.signals.succeeded.emit(self, metadata)
        except Exception as exc:  # 工作线程边界必须把故障交回 GUI 线程
            self.signals.failed.emit(self, exc)
        finally:
            if frozen is not None:
                frozen.close()
            if prepared is not None:
                prepared.close()

    def take_prepared(self) -> PreparedCard | None:
        with self._lock:
            prepared = self._prepared
            self._prepared = None
            return prepared

    def abandon(self) -> None:
        with self._lock:
            self._abandoned = True
            prepared = self._prepared
            self._prepared = None
        if prepared is not None:
            prepared.close()


class ObservationCoordinator(QObject):
    """管理显式观察会话、稳定性判断和内存候选移交。"""

    state_changed = Signal(bool)
    candidate_added = Signal()

    def __init__(
        self,
        tray_icon: _TrayLike,
        candidate_sink: CandidateSink,
        *,
        data_dir: str | Path = DATA_DIR,
        screenshot_dir: str | Path = SCREENSHOT_DIR,
        context_provider: ContextProvider = get_browser_context,
        window_provider: WindowProvider = foreground_window_snapshot,
        initial_capture_provider: InitialCaptureProvider = grab_active_monitor,
        capture_provider: CaptureProvider = grab_monitor,
        selection_provider: SelectionProvider = select_region,
        card_builder: CardBuilder = prepare_card_from_selection,
        thread_pool: _ThreadPoolLike | None = None,
        poll_interval_ms: int = 900,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        if poll_interval_ms < 250:
            raise ValueError("观察轮询间隔不能短于 250 毫秒。")
        self.tray_icon = tray_icon
        self.candidate_sink = candidate_sink
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.screenshot_dir = Path(screenshot_dir).expanduser().resolve()
        self.context_provider = context_provider
        self.window_provider = window_provider
        self.initial_capture_provider = initial_capture_provider
        self.capture_provider = capture_provider
        self.selection_provider = selection_provider
        self.card_builder = card_builder
        self.thread_pool = thread_pool or QThreadPool(self)
        if isinstance(self.thread_pool, QThreadPool):
            self.thread_pool.setMaxThreadCount(1)
        self.timer = QTimer(self)
        self.timer.setInterval(poll_interval_ms)
        self.timer.timeout.connect(self._tick)
        self._session: ObservationSession | None = None
        self._worker: _ProbeWorker | _CaptureWorker | None = None
        self._pending_signature: str | None = None
        self._captured_signature: str | None = None
        self._shutting_down = False

    @property
    def active(self) -> bool:
        return self._session is not None

    @property
    def busy(self) -> bool:
        return self._worker is not None

    @Slot()
    def request_start(self) -> None:
        """验证前台 Chrome，冻结一次画面并让用户框定观察区域。"""

        if self._shutting_down:
            return
        if self._worker is not None:
            self._notify(
                "上一会话仍在收尾",
                "请稍候片刻，再重新开始半自动观察。",
                warning=True,
            )
            return
        if self.active:
            self._notify("观察已在运行", "请先停止当前观察会话。")
            return

        frozen: Image.Image | None = None
        try:
            window = self.window_provider()
            if window is None or not is_chromium_application(window.app_name):
                self._notify(
                    "无法开始观察",
                    "请先把要观察的普通 Chrome 网页放到前台。",
                    warning=True,
                )
                return
            reason = capture_block_reason(window.app_name)
            if reason is not None:
                self._notify("已暂停观察", reason, warning=True)
                return
            context = self.context_provider(0.4)
            if context is None or context.tab_id is None:
                self._notify(
                    "无法开始观察",
                    "Chrome 扩展未响应；请重新加载 0.3.0 扩展后重试。",
                    warning=True,
                )
                return
            if context.sensitive_input:
                self._notify(
                    "已暂停观察",
                    "检测到密码、验证码或支付输入状态。",
                    warning=True,
                )
                return

            frozen, meta = self.initial_capture_provider()
            if (
                not is_chromium_application(meta.app_name)
                or not foreground_window_intersects_capture(window, meta)
            ):
                self._notify(
                    "无法开始观察",
                    "前台 Chrome 与当前捕获显示器不一致。",
                    warning=True,
                )
                return
            # 首次检查与实际抓帧之间也可能切入密码、验证码或支付输入，
            # 或切换窗口/标签。抓帧后、展示冻结画面前必须再次确认；失败时
            # 立即释放内存图像，绝不把该画面交给选区浮层。
            window_after_capture = self.window_provider()
            context_after_capture = self.context_provider(0.4)
            if (
                window_after_capture is None
                or window_after_capture.handle != window.handle
                or window_after_capture.bounds != window.bounds
                or not is_chromium_application(window_after_capture.app_name)
                or not foreground_window_intersects_capture(window_after_capture, meta)
                or context_after_capture is None
                or context_after_capture.sensitive_input
                or context_after_capture.tab_id != context.tab_id
            ):
                self._notify(
                    "已暂停观察",
                    "抓帧后无法再次确认同一安全的 Chrome 页面；未显示或保存该画面。",
                    warning=True,
                )
                return
            region = self.selection_provider(frozen, capture_meta=meta)
            if region is None:
                self._notify("已取消", "没有开始观察，也没有保存任何内容。")
                return

            region_copy = QRect(region)
            region_key = (
                f"{meta.monitor_index}:{region_copy.x()}:{region_copy.y()}:"
                f"{region_copy.width()}:{region_copy.height()}"
            )
            self._session = ObservationSession(
                session_id=str(uuid4()),
                tab_id=context.tab_id,
                window_handle=window.handle,
                window_bounds=window.bounds,
                monitor_meta=meta,
                region=region_copy,
                region_key=region_key,
            )
            self._pending_signature = None
            self._captured_signature = None
            self.timer.start()
            self.state_changed.emit(True)
            self._notify(
                "半自动观察已开始",
                "请切回原 Chrome 标签页；稳定的选中文字或原生字幕会进入内存收件箱。",
            )
        except Exception:
            self._notify(
                "无法开始观察",
                "本机检查或框选失败，没有保存任何内容。",
                warning=True,
            )
        finally:
            if frozen is not None:
                frozen.close()

    @Slot()
    def request_stop(self) -> None:
        if not self.active:
            self._notify("观察未运行", "当前没有正在运行的观察会话。")
            return
        self._stop_session("半自动观察已停止", "收件箱中的候选仍只保留在内存中。")

    @Slot()
    def _tick(self) -> None:
        session = self._session
        if session is None or self._worker is not None or self._shutting_down:
            return
        try:
            window = self.window_provider()
        except Exception:
            self._stop_session(
                "观察已安全暂停",
                "无法确认前台窗口状态；未继续抓屏。",
                warning=True,
            )
            return
        if window is None or window.handle != session.window_handle:
            # 用户切去其他应用或打开收件箱时仅等待，不在后台抓屏。
            return
        if window.bounds != session.window_bounds:
            self._stop_session(
                "观察已安全暂停",
                "Chrome 窗口位置或大小发生变化；请重新框定证据区域。",
                warning=True,
            )
            return
        worker = _ProbeWorker(self.context_provider)
        self._worker = worker
        worker.signals.succeeded.connect(
            self._on_probe_ready,
            Qt.ConnectionType.QueuedConnection,
        )
        worker.signals.failed.connect(
            self._on_worker_failed,
            Qt.ConnectionType.QueuedConnection,
        )
        try:
            self.thread_pool.start(worker)
        except Exception:
            self._worker = None
            self._stop_session(
                "观察已安全暂停",
                "无法启动本地观察工作线程；未继续抓屏。",
                warning=True,
            )

    @Slot(object, object)
    def _on_probe_ready(self, worker: object, result: object) -> None:
        if worker is not self._worker:
            return
        self._worker = None
        session = self._session
        if session is None:
            return
        if not isinstance(result, BrowserContext):
            self._stop_session(
                "观察已安全暂停",
                "Chrome 在前台但扩展未响应，请检查扩展后重新开始。",
                warning=True,
            )
            return
        if result.sensitive_input:
            self._stop_session(
                "观察已安全暂停",
                "检测到密码、验证码或支付输入状态；未采集该画面。",
                warning=True,
            )
            return
        if result.tab_id != session.tab_id:
            self._stop_session(
                "观察已安全暂停",
                "检测到 Chrome 标签页已切换；请在目标标签页重新开始。",
                warning=True,
            )
            return
        if result.observation_kind == "none" or not result.observation_text.strip():
            self._pending_signature = None
            self._captured_signature = None
            return

        signature = _signature(result)
        if signature == self._captured_signature:
            return
        if signature != self._pending_signature:
            self._pending_signature = signature
            return
        self._start_capture(session, signature)

    def _start_capture(self, session: ObservationSession, signature: str) -> None:
        worker = _CaptureWorker(
            session,
            signature,
            context_provider=self.context_provider,
            window_provider=self.window_provider,
            capture_provider=self.capture_provider,
            card_builder=self.card_builder,
            data_dir=self.data_dir,
            screenshot_dir=self.screenshot_dir,
        )
        self._worker = worker
        worker.signals.succeeded.connect(
            self._on_capture_ready,
            Qt.ConnectionType.QueuedConnection,
        )
        worker.signals.failed.connect(
            self._on_worker_failed,
            Qt.ConnectionType.QueuedConnection,
        )
        try:
            self.thread_pool.start(worker)
        except Exception:
            self._worker = None
            worker.abandon()
            self._stop_session(
                "观察已安全暂停",
                "无法启动本地证据抓取线程；未保存当前画面。",
                warning=True,
            )

    @Slot(object, object)
    def _on_capture_ready(self, worker: object, result: object) -> None:
        if worker is not self._worker or not isinstance(worker, _CaptureWorker):
            if isinstance(worker, _CaptureWorker):
                worker.abandon()
            return
        self._worker = None
        prepared = worker.take_prepared()
        if prepared is None or not isinstance(result, _CandidateMeta):
            return
        session = self._session
        if session is None or result.session_id != session.session_id:
            prepared.close()
            return
        self._captured_signature = result.signature
        try:
            accepted = bool(
                self.candidate_sink(
                    prepared,
                    session_id=result.session_id,
                    source_key=result.source_key,
                    region_key=result.region_key,
                    seen_at=result.seen_at,
                )
            )
        except Exception:
            prepared.close()
            self._stop_session(
                "观察已安全暂停",
                "候选收件箱发生本地错误，未保存该内容。",
                warning=True,
            )
            return
        if accepted:
            self.candidate_added.emit()
            self._notify("发现新候选", "已放入内存收件箱，尚未写入数据库或截图目录。")

    @Slot(object, object)
    def _on_worker_failed(self, worker: object, error: object) -> None:
        del error
        if worker is not self._worker:
            if isinstance(worker, _CaptureWorker):
                worker.abandon()
            return
        self._worker = None
        if isinstance(worker, _CaptureWorker):
            worker.abandon()
        if self._session is None:
            return
        self._stop_session(
            "观察已安全暂停",
            "本地观察检查失败；未保存当前画面，请重新开始。",
            warning=True,
        )

    def _stop_session(self, title: str, message: str, *, warning: bool = False) -> None:
        was_active = self._session is not None
        self.timer.stop()
        self._session = None
        self._pending_signature = None
        self._captured_signature = None
        worker = self._worker
        if isinstance(worker, _CaptureWorker):
            worker.abandon()
        if was_active:
            self.state_changed.emit(False)
        self._notify(title, message, warning=warning)

    def shutdown(self, timeout_ms: int = 10_000) -> bool:
        self._shutting_down = True
        self.timer.stop()
        self._session = None
        worker = self._worker
        if isinstance(worker, _CaptureWorker):
            worker.abandon()
        self.thread_pool.clear()
        completed = self.thread_pool.waitForDone(timeout_ms)
        if completed:
            self._worker = None
        return completed

    def _notify(self, title: str, message: str, *, warning: bool = False) -> None:
        icon = (
            QSystemTrayIcon.MessageIcon.Warning
            if warning
            else QSystemTrayIcon.MessageIcon.Information
        )
        self.tray_icon.showMessage(title, message, icon, 6_000)


__all__ = ["ObservationCoordinator", "ObservationSession"]
