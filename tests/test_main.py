"""T7 托盘应用的主动捕获编排、隐私拦截和生命周期测试。"""

from __future__ import annotations

import gc
import json
import os
import tempfile
import time
import unittest
import weakref
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402
from PySide6.QtCore import (  # noqa: E402
    QEventLoop,
    QObject,
    QRect,
    QThread,
    QThreadPool,
    QTimer,
    Signal,
)
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from app.bridge import BrowserContext  # noqa: E402
from app.capture import CaptureMeta, ForegroundWindowSnapshot  # noqa: E402
from app.hotkey import HotkeyError  # noqa: E402
from app.main import CaptureCoordinator, DesktopRuntime  # noqa: E402
from app.models import Card  # noqa: E402
from app.pipeline import PipelineError, PreparedCard  # noqa: E402
from app.store import Store  # noqa: E402


class _FakeTray:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, object, int]] = []

    def showMessage(
        self,
        title: str,
        message: str,
        icon: object = None,
        msecs: int = 0,
    ) -> None:
        self.messages.append((title, message, icon, msecs))


class _ImmediatePool:
    def __init__(self) -> None:
        self.started: list[object] = []
        self.cleared = False

    def start(self, runnable: object, priority: int = 0) -> None:
        del priority
        self.started.append(runnable)
        runnable.run()  # type: ignore[attr-defined]

    def clear(self) -> None:
        self.cleared = True

    def waitForDone(self, msecs: int = -1) -> bool:
        del msecs
        return True


class _DeferredPool(_ImmediatePool):
    def start(self, runnable: object, priority: int = 0) -> None:
        del priority
        self.started.append(runnable)


class _TimedOutPool(_DeferredPool):
    def __init__(self) -> None:
        super().__init__()
        self.wait_results = [False, True]

    def waitForDone(self, msecs: int = -1) -> bool:
        del msecs
        return self.wait_results.pop(0)


class _ObservedPool:
    def __init__(self) -> None:
        self.pool = QThreadPool()
        self.worker_ref: weakref.ReferenceType[object] | None = None

    def start(self, runnable: object, priority: int = 0) -> None:
        self.worker_ref = weakref.ref(runnable)
        self.pool.start(runnable, priority)  # type: ignore[arg-type]

    def clear(self) -> None:
        self.pool.clear()

    def waitForDone(self, msecs: int = -1) -> bool:
        return self.pool.waitForDone(msecs)


class _FakeHotkey(QObject):
    activated = Signal()
    fail_on_start = False
    instances: list["_FakeHotkey"] = []

    def __init__(self, hotkey: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.hotkey = hotkey
        self.is_started = False
        self.stop_calls = 0
        type(self).instances.append(self)

    def start(self) -> None:
        if self.fail_on_start:
            raise HotkeyError("测试用快捷键冲突")
        self.is_started = True

    def stop(self) -> None:
        self.stop_calls += 1
        self.is_started = False


class _FakeApiServer:
    def __init__(self, store: Store, events: list[str]) -> None:
        self.store = store
        self.events = events
        self.running = False

    def start(self, timeout: float = 5.0) -> "_FakeApiServer":
        del timeout
        self.events.append("api_start")
        self.running = True
        return self

    def stop(self, timeout: float = 5.0) -> None:
        del timeout
        self.events.append("api_stop")
        self.running = False



class _FakeLibrary:
    instances: list["_FakeLibrary"] = []

    def __init__(self, store: Store, *, data_dir: Path) -> None:
        self.store = store
        self.data_dir = data_dir
        self.busy = False
        self.request_refresh_calls = 0
        self.show_calls = 0
        self.hidden = False
        self.wait_result = True
        type(self).instances.append(self)

    def request_refresh(self) -> bool:
        self.request_refresh_calls += 1
        return True

    def show(self) -> None:
        self.show_calls += 1

    def raise_(self) -> None:
        return None

    def activateWindow(self) -> None:
        return None

    def close(self) -> bool:
        return True

    def wait_for_idle(self, timeout_ms: int = -1) -> bool:
        del timeout_ms
        return self.wait_result

    def hide(self) -> None:
        self.hidden = True

class CaptureCoordinatorTests(unittest.TestCase):
    application: QApplication

    @classmethod
    def setUpClass(cls) -> None:
        instance = QApplication.instance()
        cls.application = instance if isinstance(instance, QApplication) else QApplication([])

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temporary.name) / "data"
        self.screenshot_dir = self.data_dir / "screenshots"
        self.store = Store(self.data_dir / "cards.sqlite3", self.data_dir)
        self.store.init_db()
        self.tray = _FakeTray()
        self.image = Image.new("RGB", (20, 12), "white")
        self.meta = CaptureMeta(
            monitor_index=1,
            left=0,
            top=0,
            width=20,
            height=12,
            scale=1.0,
            device_name=r"\\.\DISPLAY1",
            app_name="chrome.exe",
            captured_at="2026-07-22T10:20:30+08:00",
        )
        self.context = BrowserContext(
            url="https://example.test/video",
            title="测试视频",
            selection="测试选中文字",
            video_time=8.5,
        )

    def tearDown(self) -> None:
        self.application.processEvents()
        self.temporary.cleanup()

    def _prepared_card(self, context: BrowserContext | None) -> PreparedCard:
        card = Card(
            text=context.selection if context and context.selection else "OCR 文字",
            text_source="dom" if context and context.selection else "ocr",
            confidence=0.99 if context and context.selection else 0.8,
            screenshot_path="screenshots/selection.png",
            full_screenshot_path="screenshots/full.png",
            source_url=context.url if context else None,
            source_title=context.title if context else None,
            video_time=context.video_time if context else None,
            app_name=self.meta.app_name,
        )
        return PreparedCard(
            card=card,
            selected_image=Image.new("RGB", (5, 3), "white"),
            full_image=self.image.copy(),
        )

    def test_capture_reads_context_before_screen_and_reviews_memory_candidate(self) -> None:
        events: list[str] = []
        reviewed: list[Card] = []

        def context_provider(timeout: float) -> BrowserContext:
            self.assertEqual(timeout, 0.3)
            events.append("context")
            return self.context

        def capture_provider() -> tuple[Image.Image, CaptureMeta]:
            events.append("capture")
            return self.image, self.meta

        def selection_provider(
            image: Image.Image,
            *,
            capture_meta: CaptureMeta,
        ) -> QRect:
            self.assertIs(image, self.image)
            self.assertIs(capture_meta, self.meta)
            events.append("selection")
            return QRect(1, 1, 5, 3)

        def builder(
            image: Image.Image,
            meta: CaptureMeta,
            rect: QRect,
            **kwargs: Any,
        ) -> PreparedCard:
            self.assertIs(image, self.image)
            self.assertIs(meta, self.meta)
            self.assertEqual(rect, QRect(1, 1, 5, 3))
            self.assertNotIn("as_draft", kwargs)
            self.assertNotIn("store", kwargs)
            context = kwargs["context_provider"]()
            self.assertIs(context, self.context)
            events.append("builder")
            return self._prepared_card(context)

        class SavedReview:
            saved = True
            finalized = True
            discarded = False

            def __init__(dialog_self, card: Card, *_args: Any, **kwargs: Any) -> None:
                dialog_self.card = card
                self.assertIsInstance(kwargs["selected_image"], Image.Image)
                self.assertIsInstance(kwargs["full_image"], Image.Image)
                self.assertIsNone(kwargs["selected_preview_png"])
                self.assertIsNone(kwargs["full_preview_png"])

            def exec(dialog_self) -> int:
                events.append("review")
                reviewed.append(dialog_self.card)
                return 1

        pool = _ImmediatePool()
        coordinator = CaptureCoordinator(
            self.store,
            self.tray,
            data_dir=self.data_dir,
            screenshot_dir=self.screenshot_dir,
            foreground_provider=lambda: events.append("foreground") or "chrome.exe",
            context_provider=context_provider,
            capture_provider=capture_provider,
            selection_provider=selection_provider,
            card_builder=builder,
            review_factory=SavedReview,
            thread_pool=pool,
        )

        coordinator.request_capture()
        self.application.processEvents()

        self.assertEqual(
            events,
            [
                "foreground",
                "context",
                "capture",
                "context",
                "selection",
                "builder",
                "review",
            ],
        )
        self.assertFalse(coordinator.busy)
        self.assertEqual(len(reviewed), 1)
        self.assertEqual(self.store.list_recent(), [])
        self.assertEqual(self.tray.messages[-1][0], "卡片已保存")
        worker = pool.started[0]
        self.assertTrue(worker.autoDelete())  # type: ignore[attr-defined]
        self.assertIsNone(worker.frozen_image)  # type: ignore[attr-defined]
        self.assertIsNone(worker.browser_context)  # type: ignore[attr-defined]
        self.assertIsNone(worker.prepared)  # type: ignore[attr-defined]

    def test_invalid_builder_result_is_rejected_and_worker_is_released(self) -> None:
        coordinator = CaptureCoordinator(
            self.store,
            self.tray,
            data_dir=self.data_dir,
            screenshot_dir=self.screenshot_dir,
            foreground_provider=lambda: "notepad.exe",
            context_provider=lambda _timeout: None,
            capture_provider=lambda: (self.image, replace(self.meta, app_name="notepad.exe")),
            selection_provider=lambda *_args, **_kwargs: QRect(0, 0, 4, 4),
            card_builder=lambda *_args, **_kwargs: object(),  # type: ignore[arg-type,return-value]
            thread_pool=_ImmediatePool(),
        )

        coordinator.request_capture()
        self.application.processEvents()

        self.assertFalse(coordinator.busy)
        self.assertEqual(self.store.list_recent(), [])
        self.assertEqual(self.tray.messages[-1][0], "没有生成卡片")

    def test_notification_failure_cannot_delete_already_saved_card(self) -> None:
        class RaisingResultTray(_FakeTray):
            def showMessage(
                tray_self,
                title: str,
                message: str,
                icon: object = None,
                msecs: int = 0,
            ) -> None:
                if title == "卡片已保存":
                    raise RuntimeError("模拟托盘通知失败")
                super().showMessage(title, message, icon, msecs)

        store = self.store

        class PersistingReview:
            saved = True
            finalized = True
            discarded = False

            def __init__(dialog_self, card: Card, *_args: Any, **_kwargs: Any) -> None:
                dialog_self.card = card

            def exec(dialog_self) -> int:
                store.add_card(dialog_self.card)
                return 1

        coordinator = CaptureCoordinator(
            self.store,
            RaisingResultTray(),
            data_dir=self.data_dir,
            screenshot_dir=self.screenshot_dir,
            foreground_provider=lambda: "chrome.exe",
            context_provider=lambda _timeout: self.context,
            capture_provider=lambda: (self.image, self.meta),
            selection_provider=lambda *_args, **_kwargs: QRect(0, 0, 4, 4),
            card_builder=lambda *_args, **kwargs: self._prepared_card(
                kwargs["context_provider"]()
            ),
            review_factory=PersistingReview,
            thread_pool=_ImmediatePool(),
        )

        with self.assertLogs("app.main", level="ERROR") as logs:
            coordinator.request_capture()
            self.application.processEvents()

        self.assertFalse(coordinator.busy)
        self.assertEqual(len(self.store.list_recent()), 1)
        self.assertTrue(any("无法显示审核结果通知" in line for line in logs.output))

    def test_review_result_waits_for_inflight_save_before_releasing_candidate(self) -> None:
        wait_calls: list[int] = []

        class SavingReview:
            saved = False
            finalized = False
            discarded = False
            saving = True

            def __init__(dialog_self, *_args: Any, **_kwargs: Any) -> None:
                pass

            def exec(dialog_self) -> int:
                return 0

            def wait_for_save(dialog_self, timeout_ms: int) -> bool:
                wait_calls.append(timeout_ms)
                dialog_self.saving = False
                dialog_self.saved = True
                dialog_self.finalized = True
                return True

        pool = _ImmediatePool()
        coordinator = CaptureCoordinator(
            self.store,
            self.tray,
            data_dir=self.data_dir,
            screenshot_dir=self.screenshot_dir,
            foreground_provider=lambda: "chrome.exe",
            context_provider=lambda _timeout: self.context,
            capture_provider=lambda: (self.image, self.meta),
            selection_provider=lambda *_args, **_kwargs: QRect(0, 0, 4, 4),
            card_builder=lambda *_args, **kwargs: self._prepared_card(
                kwargs["context_provider"]()
            ),
            review_factory=SavingReview,
            thread_pool=pool,
        )

        coordinator.request_capture()
        self.application.processEvents()

        self.assertEqual(wait_calls, [10_000])
        self.assertFalse(coordinator.busy)
        self.assertIsNone(pool.started[0].prepared)  # type: ignore[attr-defined]
        self.assertEqual(self.tray.messages[-1][0], "卡片已保存")

    def test_shutdown_waits_for_active_review_save_before_cleanup(self) -> None:
        wait_calls: list[int] = []

        class ActiveSavingDialog:
            saved = False
            finalized = False
            saving = True

            def wait_for_save(dialog_self, timeout_ms: int) -> bool:
                wait_calls.append(timeout_ms)
                dialog_self.saving = False
                dialog_self.saved = True
                dialog_self.finalized = True
                return True

            def reject(dialog_self) -> None:
                raise AssertionError("保存成功后不应丢弃")

        coordinator = CaptureCoordinator(
            self.store,
            self.tray,
            data_dir=self.data_dir,
            screenshot_dir=self.screenshot_dir,
            thread_pool=_ImmediatePool(),
        )
        coordinator._active_dialog = ActiveSavingDialog()  # type: ignore[assignment]
        coordinator._busy = True

        self.assertTrue(coordinator.shutdown(timeout_ms=321))

        self.assertEqual(wait_calls, [321])
        self.assertIsNone(coordinator._active_dialog)
        self.assertFalse(coordinator.busy)

    def test_known_sensitive_application_stops_before_browser_or_capture(self) -> None:
        coordinator = CaptureCoordinator(
            self.store,
            self.tray,
            data_dir=self.data_dir,
            screenshot_dir=self.screenshot_dir,
            foreground_provider=lambda: r"C:\Program Files\Bitwarden.exe",
            context_provider=lambda _timeout: (_ for _ in ()).throw(
                AssertionError("敏感应用不应读取浏览器上下文")
            ),
            capture_provider=lambda: (_ for _ in ()).throw(
                AssertionError("敏感应用不应截屏")
            ),
            thread_pool=_ImmediatePool(),
        )

        coordinator.request_capture()
        self.application.processEvents()

        self.assertFalse(coordinator.busy)
        self.assertEqual(self.store.list_recent(), [])
        self.assertIn("密码管理器", self.tray.messages[-1][1])

    def test_sensitive_browser_field_stops_before_screen_capture(self) -> None:
        sensitive = replace(
            self.context,
            selection="",
            sensitive_input=True,
        )
        coordinator = CaptureCoordinator(
            self.store,
            self.tray,
            data_dir=self.data_dir,
            screenshot_dir=self.screenshot_dir,
            foreground_provider=lambda: "chrome.exe",
            context_provider=lambda _timeout: sensitive,
            capture_provider=lambda: (_ for _ in ()).throw(
                AssertionError("敏感浏览器输入不应截屏")
            ),
            thread_pool=_ImmediatePool(),
        )

        coordinator.request_capture()

        self.assertFalse(coordinator.busy)
        self.assertEqual(self.store.list_recent(), [])
        self.assertIn("验证码或支付", self.tray.messages[-1][1])

    def test_sensitive_field_detected_after_capture_releases_frame_before_overlay(self) -> None:
        safe = self.context
        sensitive = replace(self.context, selection="", sensitive_input=True)
        contexts = iter((safe, sensitive))
        frame = Image.new("RGB", (20, 12), "white")
        coordinator = CaptureCoordinator(
            self.store,
            self.tray,
            data_dir=self.data_dir,
            screenshot_dir=self.screenshot_dir,
            foreground_provider=lambda: "chrome.exe",
            context_provider=lambda _timeout: next(contexts),
            capture_provider=lambda: (frame, self.meta),
            selection_provider=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("截图后发现敏感输入时不应显示框选层")
            ),
            thread_pool=_ImmediatePool(),
        )

        coordinator.request_capture()

        self.assertFalse(coordinator.busy)
        self.assertEqual(self.store.list_recent(), [])
        with self.assertRaises(ValueError):
            frame.getpixel((0, 0))
        self.assertIn("验证码或支付", self.tray.messages[-1][1])

    def test_unavailable_extension_warns_that_sensitive_state_is_unknown(self) -> None:
        coordinator = CaptureCoordinator(
            self.store,
            self.tray,
            data_dir=self.data_dir,
            screenshot_dir=self.screenshot_dir,
            foreground_provider=lambda: "chrome.exe",
            context_provider=lambda _timeout: None,
            capture_provider=lambda: (self.image, self.meta),
            selection_provider=lambda *_args, **_kwargs: None,
            thread_pool=_ImmediatePool(),
        )

        coordinator.request_capture()

        self.assertTrue(
            any(
                "无法确认浏览器密码" in message
                for _title, message, _icon, _ms in self.tray.messages
            )
        )
        self.assertFalse(coordinator.busy)

    def test_browser_window_switch_or_other_monitor_disables_dom_context(self) -> None:
        cases = (
            (
                "窗口切换",
                ForegroundWindowSnapshot(1, "chrome.exe", (0, 0, 20, 12)),
                ForegroundWindowSnapshot(2, "chrome.exe", (0, 0, 20, 12)),
                "切换了浏览器窗口",
            ),
            (
                "显示器错配",
                ForegroundWindowSnapshot(1, "chrome.exe", (100, 0, 120, 12)),
                ForegroundWindowSnapshot(1, "chrome.exe", (100, 0, 120, 12)),
                "显示器不一致",
            ),
        )
        for label, before_window, after_window, warning_text in cases:
            with self.subTest(label=label):
                windows = iter((before_window, after_window))
                observed: list[BrowserContext | None] = []

                def builder(*_args: Any, **kwargs: Any) -> PreparedCard:
                    context = kwargs["context_provider"]()
                    observed.append(context)
                    return self._prepared_card(context)

                class SavedReview:
                    saved = True
                    finalized = True
                    discarded = False

                    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
                        pass

                    def exec(self) -> int:
                        return 1

                coordinator = CaptureCoordinator(
                    self.store,
                    self.tray,
                    data_dir=self.data_dir,
                    screenshot_dir=self.screenshot_dir,
                    foreground_provider=lambda: "不应使用.exe",
                    window_provider=lambda: next(windows),
                    context_provider=lambda _timeout: self.context,
                    capture_provider=lambda: (self.image.copy(), self.meta),
                    selection_provider=lambda *_args, **_kwargs: QRect(0, 0, 4, 4),
                    card_builder=builder,
                    review_factory=SavedReview,
                    thread_pool=_ImmediatePool(),
                )

                coordinator.request_capture()
                self.application.processEvents()

                self.assertEqual(observed, [None])
                self.assertTrue(
                    any(warning_text in message for _title, message, _icon, _ms in self.tray.messages)
                )
                self.assertEqual(self.store.list_recent(), [])

    def test_foreground_change_discards_stale_browser_context(self) -> None:
        notepad_meta = replace(self.meta, app_name="notepad.exe")
        observed_contexts: list[BrowserContext | None] = []

        prepared_cards: list[PreparedCard] = []

        def builder(*_args: Any, **kwargs: Any) -> PreparedCard:
            context = kwargs["context_provider"]()
            observed_contexts.append(context)
            prepared = self._prepared_card(context)
            prepared_cards.append(prepared)
            return prepared

        class SavedReview:
            saved = True
            finalized = True
            discarded = False

            def __init__(self, *_args: Any, **_kwargs: Any) -> None:
                pass

            def exec(self) -> int:
                return 1

        coordinator = CaptureCoordinator(
            self.store,
            self.tray,
            data_dir=self.data_dir,
            screenshot_dir=self.screenshot_dir,
            foreground_provider=lambda: "chrome.exe",
            context_provider=lambda _timeout: self.context,
            capture_provider=lambda: (self.image, notepad_meta),
            selection_provider=lambda *_args, **_kwargs: QRect(0, 0, 4, 4),
            card_builder=builder,
            review_factory=SavedReview,
            thread_pool=_ImmediatePool(),
        )

        coordinator.request_capture()
        self.application.processEvents()

        self.assertEqual(observed_contexts, [None])
        self.assertEqual(len(prepared_cards), 1)
        self.assertIsNone(prepared_cards[0].card.source_url)
        self.assertEqual(self.store.list_recent(), [])

    def test_cancelled_selection_never_starts_pipeline(self) -> None:
        pool = _ImmediatePool()
        coordinator = CaptureCoordinator(
            self.store,
            self.tray,
            data_dir=self.data_dir,
            screenshot_dir=self.screenshot_dir,
            foreground_provider=lambda: "notepad.exe",
            context_provider=lambda _timeout: None,
            capture_provider=lambda: (self.image, self.meta),
            selection_provider=lambda *_args, **_kwargs: None,
            card_builder=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("取消框选不应构建卡片")
            ),
            thread_pool=pool,
        )

        coordinator.request_capture()

        self.assertFalse(coordinator.busy)
        self.assertEqual(pool.started, [])
        self.assertEqual(self.store.list_recent(), [])
        self.assertEqual(self.tray.messages[-1][0], "已取消")

    def test_review_construction_failure_releases_memory_without_persistence(self) -> None:
        built: list[PreparedCard] = []

        def builder(*_args: Any, **kwargs: Any) -> PreparedCard:
            prepared = self._prepared_card(kwargs["context_provider"]())
            built.append(prepared)
            return prepared

        def broken_review(*_args: Any, **_kwargs: Any) -> object:
            raise TypeError("测试用审核窗口构造失败")

        coordinator = CaptureCoordinator(
            self.store,
            self.tray,
            data_dir=self.data_dir,
            screenshot_dir=self.screenshot_dir,
            foreground_provider=lambda: "chrome.exe",
            context_provider=lambda _timeout: self.context,
            capture_provider=lambda: (self.image, self.meta),
            selection_provider=lambda *_args, **_kwargs: QRect(0, 0, 4, 4),
            card_builder=builder,
            review_factory=broken_review,
            thread_pool=_ImmediatePool(),
        )

        with self.assertLogs("app.main", level="ERROR"):
            coordinator.request_capture()
            self.application.processEvents()

        self.assertEqual(len(built), 1)
        self.assertTrue(built[0].is_closed)
        self.assertEqual(self.store.list_recent(), [])
        self.assertFalse(self.screenshot_dir.exists())
        self.assertFalse(coordinator.busy)
        self.assertEqual(self.tray.messages[-1][0], "审核失败")

    def test_real_thread_pool_keeps_ocr_off_gui_and_review_on_gui(self) -> None:
        builder_was_on_gui: list[bool] = []
        review_was_on_gui: list[bool] = []

        def builder(*_args: Any, **kwargs: Any) -> PreparedCard:
            builder_was_on_gui.append(
                QThread.currentThread() is self.application.thread()
            )
            return self._prepared_card(kwargs["context_provider"]())

        class SavedReview:
            saved = True
            finalized = True
            discarded = False

            def __init__(dialog_self, *_args: Any, **_kwargs: Any) -> None:
                pass

            def exec(dialog_self) -> int:
                review_was_on_gui.append(
                    QThread.currentThread() is self.application.thread()
                )
                return 1

        coordinator = CaptureCoordinator(
            self.store,
            self.tray,
            data_dir=self.data_dir,
            screenshot_dir=self.screenshot_dir,
            foreground_provider=lambda: "chrome.exe",
            context_provider=lambda _timeout: self.context,
            capture_provider=lambda: (self.image, self.meta),
            selection_provider=lambda *_args, **_kwargs: QRect(0, 0, 4, 4),
            card_builder=builder,
            review_factory=SavedReview,
        )
        coordinator.request_capture()

        loop = QEventLoop()
        poll = QTimer()
        poll.setInterval(10)
        poll.timeout.connect(lambda: loop.quit() if not coordinator.busy else None)
        poll.start()
        QTimer.singleShot(3_000, loop.quit)
        loop.exec()
        poll.stop()

        self.assertFalse(coordinator.busy, "后台卡片流水线在超时前未完成")
        self.assertEqual(builder_was_on_gui, [False])
        self.assertEqual(review_was_on_gui, [True])

    def test_finished_worker_is_released_by_real_thread_pool(self) -> None:
        pool = _ObservedPool()

        def failing_builder(*_args: Any, **_kwargs: Any) -> PreparedCard:
            raise PipelineError("测试完成后释放")

        coordinator = CaptureCoordinator(
            self.store,
            self.tray,
            data_dir=self.data_dir,
            screenshot_dir=self.screenshot_dir,
            foreground_provider=lambda: "chrome.exe",
            context_provider=lambda _timeout: self.context,
            capture_provider=lambda: (Image.new("RGB", (20, 12), "white"), self.meta),
            selection_provider=lambda *_args, **_kwargs: QRect(0, 0, 4, 4),
            card_builder=failing_builder,
            thread_pool=pool,
        )
        coordinator.request_capture()

        loop = QEventLoop()
        poll = QTimer()
        poll.setInterval(10)
        poll.timeout.connect(lambda: loop.quit() if not coordinator.busy else None)
        poll.start()
        QTimer.singleShot(3_000, loop.quit)
        loop.exec()
        poll.stop()
        self.assertFalse(coordinator.busy)
        self.assertIsNotNone(pool.worker_ref)

        for _ in range(3):
            self.application.processEvents()
            gc.collect()
        assert pool.worker_ref is not None
        self.assertIsNone(pool.worker_ref())

    def test_shutdown_clears_queued_work_without_running_ocr(self) -> None:
        pool = _DeferredPool()
        coordinator = CaptureCoordinator(
            self.store,
            self.tray,
            data_dir=self.data_dir,
            screenshot_dir=self.screenshot_dir,
            foreground_provider=lambda: "notepad.exe",
            context_provider=lambda _timeout: None,
            capture_provider=lambda: (self.image, self.meta),
            selection_provider=lambda *_args, **_kwargs: QRect(0, 0, 4, 4),
            card_builder=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("已清空的排队任务不应运行")
            ),
            thread_pool=pool,
        )
        coordinator.request_capture()
        self.assertTrue(coordinator.busy)

        completed = coordinator.shutdown()

        self.assertTrue(completed)
        self.assertTrue(pool.cleared)
        self.assertFalse(coordinator.busy)
        self.assertEqual(self.store.list_recent(), [])

    def test_timed_out_shutdown_keeps_worker_owned_and_blocks_exit(self) -> None:
        pool = _TimedOutPool()
        coordinator = CaptureCoordinator(
            self.store,
            self.tray,
            data_dir=self.data_dir,
            screenshot_dir=self.screenshot_dir,
            foreground_provider=lambda: "notepad.exe",
            context_provider=lambda _timeout: None,
            capture_provider=lambda: (self.image, self.meta),
            selection_provider=lambda *_args, **_kwargs: QRect(0, 0, 4, 4),
            thread_pool=pool,
        )
        coordinator.request_capture()

        self.assertFalse(coordinator.shutdown(timeout_ms=1))
        self.assertTrue(coordinator.busy)
        self.assertEqual(len(coordinator._workers), 1)

        self.assertTrue(coordinator.shutdown(timeout_ms=-1))
        self.assertFalse(coordinator.busy)
        self.assertEqual(len(coordinator._workers), 0)

    def test_shutdown_waits_for_running_builder_then_releases_memory_candidate(self) -> None:
        built: list[PreparedCard] = []

        def slow_builder(*_args: Any, **kwargs: Any) -> PreparedCard:
            time.sleep(0.05)
            prepared = self._prepared_card(kwargs["context_provider"]())
            built.append(prepared)
            return prepared

        coordinator = CaptureCoordinator(
            self.store,
            self.tray,
            data_dir=self.data_dir,
            screenshot_dir=self.screenshot_dir,
            foreground_provider=lambda: "chrome.exe",
            context_provider=lambda _timeout: self.context,
            capture_provider=lambda: (self.image, self.meta),
            selection_provider=lambda *_args, **_kwargs: QRect(0, 0, 4, 4),
            card_builder=slow_builder,
        )
        coordinator.request_capture()

        self.assertTrue(coordinator.shutdown())

        self.assertEqual(len(built), 1)
        self.assertTrue(built[0].is_closed)
        self.assertEqual(self.store.list_recent(), [])
        self.assertFalse(self.screenshot_dir.exists())
        self.assertFalse(coordinator.busy)


class DesktopRuntimeTests(unittest.TestCase):
    application: QApplication

    @classmethod
    def setUpClass(cls) -> None:
        instance = QApplication.instance()
        cls.application = instance if isinstance(instance, QApplication) else QApplication([])

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temporary.name) / "data"
        self.events: list[str] = []
        _FakeHotkey.instances.clear()
        _FakeHotkey.fail_on_start = False
        _FakeLibrary.instances.clear()

    def tearDown(self) -> None:
        self.application.processEvents()
        self.temporary.cleanup()

    def _runtime(self) -> DesktopRuntime:
        return DesktopRuntime(
            self.application,
            data_dir=self.data_dir,
            screenshot_dir=self.data_dir / "screenshots",
            db_path=self.data_dir / "cards.sqlite3",
            bridge_start=lambda: self.events.append("bridge_start"),
            bridge_stop=lambda: self.events.append("bridge_stop"),
            api_server_factory=lambda store: _FakeApiServer(store, self.events),
            library_factory=_FakeLibrary,
        )

    def test_observation_actions_and_memory_inbox_are_wired_without_disk_write(self) -> None:
        with patch("app.main.HotkeyManager", _FakeHotkey):
            runtime = self._runtime()
        card_id = str(uuid4())
        prepared = PreparedCard(
            card=Card(
                id=card_id,
                text="等待审核的原生字幕",
                text_source="dom",
                confidence=0.99,
                screenshot_path=f"screenshots/{card_id}.png",
                full_screenshot_path=f"screenshots/full_{card_id}.png",
                stance="unknown",
            ),
            selected_image=Image.new("RGB", (5, 3), "white"),
            full_image=Image.new("RGB", (20, 12), "black"),
        )
        try:
            self.assertEqual(runtime.observe_start_action.text(), "开始半自动观察……")
            self.assertFalse(runtime.observe_stop_action.isEnabled())
            self.assertEqual(runtime.inbox_action.text(), "候选收件箱（0）")

            added = runtime._offer_observation_candidate(
                prepared,
                session_id="session-one",
                source_key="a" * 64,
                region_key="1:0:0:5:3",
                seen_at=10.0,
            )

            self.assertTrue(added)
            self.assertEqual(len(runtime.inbox), 1)
            self.assertEqual(runtime.inbox_action.text(), "候选收件箱（1）")
            self.assertFalse(self.data_dir.exists())
            runtime._observation_state_changed(True)
            self.assertFalse(runtime.observe_start_action.isEnabled())
            self.assertTrue(runtime.observe_stop_action.isEnabled())
            self.assertFalse(runtime.capture_action.isEnabled())
        finally:
            runtime.shutdown()
        self.assertTrue(prepared.is_closed)

    def test_request_quit_is_blocked_while_inbox_review_owns_candidate(self) -> None:
        class BusyInboxWindow:
            busy = True

            def shutdown(self) -> bool:
                return True

        with patch("app.main.HotkeyManager", _FakeHotkey):
            runtime = self._runtime()
        runtime._inbox_window = BusyInboxWindow()  # type: ignore[assignment]

        try:
            with patch("app.main.QApplication.quit") as quit_application:
                runtime.request_quit()

            quit_application.assert_not_called()
            self.assertEqual(runtime.tray_icon.toolTip(), "本地屏幕内容与观点采集助手")
            self.assertEqual(runtime._stopped, False)
        finally:
            runtime.shutdown()

    def test_candidate_remains_owned_if_ui_refresh_fails_after_offer(self) -> None:
        with patch("app.main.HotkeyManager", _FakeHotkey):
            runtime = self._runtime()
        card_id = str(uuid4())
        prepared = PreparedCard(
            card=Card(
                id=card_id,
                text="移交后界面刷新失败也必须保留",
                text_source="dom",
                confidence=0.99,
                screenshot_path=f"screenshots/{card_id}.png",
                full_screenshot_path=f"screenshots/full_{card_id}.png",
                stance="unknown",
            ),
            selected_image=Image.new("RGB", (5, 3), "white"),
            full_image=Image.new("RGB", (20, 12), "black"),
        )
        try:
            with (
                patch.object(
                    runtime,
                    "_refresh_inbox",
                    side_effect=RuntimeError("测试界面刷新失败"),
                ),
                self.assertLogs("app.main", level="ERROR"),
            ):
                added = runtime._offer_observation_candidate(
                    prepared,
                    session_id="session-one",
                    source_key="c" * 64,
                    region_key="1:0:0:5:3",
                    seen_at=10.0,
                )

            self.assertTrue(added)
            self.assertEqual(len(runtime.inbox), 1)
            self.assertFalse(prepared.is_closed)
            self.assertEqual(runtime.inbox.snapshot()[0].card_id, card_id)
        finally:
            runtime.shutdown()
        self.assertTrue(prepared.is_closed)

    def test_request_quit_waits_for_observation_worker_to_finish(self) -> None:
        with patch("app.main.HotkeyManager", _FakeHotkey):
            runtime = self._runtime()
        runtime.observation._worker = object()  # type: ignore[assignment]

        try:
            with patch("app.main.QApplication.quit") as quit_application:
                runtime.request_quit()

            quit_application.assert_not_called()
            self.assertFalse(runtime._stopped)
        finally:
            runtime.shutdown()

    def test_start_and_shutdown_own_bridge_hotkey_tray_and_database(self) -> None:
        with patch("app.main.HotkeyManager", _FakeHotkey):
            runtime = self._runtime()
            runtime.start()
            self.assertTrue((self.data_dir / "cards.sqlite3").is_file())
            self.assertTrue(runtime.tray_icon.isVisible())
            self.assertTrue(_FakeHotkey.instances[-1].is_started)

            runtime.shutdown()

        self.assertEqual(self.events, ["bridge_start", "api_start", "api_stop", "bridge_stop"])
        self.assertEqual(_FakeHotkey.instances[-1].stop_calls, 1)
        self.assertFalse(runtime.tray_icon.isVisible())

    def test_hotkey_start_failure_rolls_back_started_bridge(self) -> None:
        _FakeHotkey.fail_on_start = True
        with patch("app.main.HotkeyManager", _FakeHotkey):
            runtime = self._runtime()
            with self.assertRaisesRegex(HotkeyError, "快捷键冲突"):
                runtime.start()

        self.assertEqual(self.events, ["bridge_start", "api_start", "api_stop", "bridge_stop"])
        self.assertFalse(runtime.tray_icon.isVisible())

    def test_tray_opens_one_library_window_and_readonly_browser(self) -> None:
        with (
            patch("app.main.HotkeyManager", _FakeHotkey),
            patch("app.main.QDesktopServices.openUrl", return_value=True) as open_url,
        ):
            runtime = self._runtime()
            runtime.start()
            runtime._show_library()
            runtime._show_library()
            runtime._open_readonly_search()

            self.assertEqual(len(_FakeLibrary.instances), 1)
            library = _FakeLibrary.instances[0]
            self.assertIs(runtime._library_window, library)
            self.assertEqual(library.request_refresh_calls, 2)
            self.assertEqual(library.show_calls, 2)
            opened_url = open_url.call_args.args[0]
            self.assertEqual(opened_url.toString(), "http://127.0.0.1:8000/")
            runtime.shutdown()

        self.assertTrue(library.hidden)

    def test_tray_configures_obsidian_vault_and_creates_local_managed_index(self) -> None:
        vault = Path(self.temporary.name) / "obsidian-vault"
        (vault / ".obsidian").mkdir(parents=True)
        with (
            patch("app.main.HotkeyManager", _FakeHotkey),
            patch("app.main.QFileDialog.getExistingDirectory", return_value=str(vault)),
            patch(
                "app.main.QMessageBox.question",
                return_value=QMessageBox.StandardButton.Yes,
            ),
        ):
            runtime = self._runtime()
            runtime.start()
            self.assertTrue(runtime._choose_obsidian_vault())

            deadline = time.monotonic() + 5.0
            while runtime.obsidian.busy and time.monotonic() < deadline:
                self.application.processEvents()
                time.sleep(0.01)
            self.application.processEvents()

            settings_path = self.data_dir / "integrations" / "obsidian.json"
            self.assertTrue(settings_path.is_file())
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            self.assertTrue(settings["enabled"])
            self.assertEqual(Path(settings["vault_path"]), vault.resolve())
            self.assertFalse(settings["copy_attachments"])
            self.assertTrue((vault / "Capture Assistant" / "索引.md").is_file())
            self.assertEqual(
                runtime.obsidian_status_action.text(),
                "状态：自动归档已开启",
            )
            runtime.shutdown()

    def test_shutdown_keeps_library_reference_if_its_task_times_out(self) -> None:
        with patch("app.main.HotkeyManager", _FakeHotkey):
            runtime = self._runtime()
            runtime.start()
            runtime._show_library()
            library = _FakeLibrary.instances[0]
            library.wait_result = False

            runtime.shutdown()

        self.assertIs(runtime._library_window, library)
    def test_start_removes_crash_leftover_draft_and_both_images(self) -> None:
        screenshots = self.data_dir / "screenshots"
        screenshots.mkdir(parents=True)
        draft_id = str(uuid4())
        draft = Card(
            id=draft_id,
            text="异常中断候选",
            text_source="ocr",
            confidence=0.8,
            screenshot_path=f"screenshots/{draft_id}.png",
            full_screenshot_path=f"screenshots/full_{draft_id}.png",
        )
        Image.new("RGB", (4, 3), "white").save(
            self.data_dir / draft.screenshot_path
        )
        Image.new("RGB", (8, 6), "black").save(
            self.data_dir / draft.full_screenshot_path
        )
        store = Store(self.data_dir / "cards.sqlite3", self.data_dir)
        store.init_db()
        store.add_draft(draft)

        with patch("app.main.HotkeyManager", _FakeHotkey):
            runtime = self._runtime()
            runtime.start()
            runtime.shutdown()

        self.assertFalse((self.data_dir / draft.screenshot_path).exists())
        self.assertFalse((self.data_dir / draft.full_screenshot_path).exists())
        self.assertFalse(store.delete_card(draft.id))


if __name__ == "__main__":
    unittest.main()
