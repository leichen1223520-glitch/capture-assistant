"""半自动观察会话的授权、稳定性和隐私暂停测试。"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402
from PySide6.QtCore import QRect  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.bridge import BrowserContext  # noqa: E402
from app.capture import CaptureMeta, ForegroundWindowSnapshot  # noqa: E402
from app.observation import ObservationCoordinator  # noqa: E402
from app.pipeline import PreparedCard  # noqa: E402


class _FakeTray:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, object, int]] = []

    def showMessage(self, title: str, message: str, icon: object, msecs: int) -> None:
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


class ObservationCoordinatorTests(unittest.TestCase):
    application: QApplication

    @classmethod
    def setUpClass(cls) -> None:
        instance = QApplication.instance()
        cls.application = instance if isinstance(instance, QApplication) else QApplication([])

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temporary.name) / "data"
        self.screenshot_dir = self.data_dir / "screenshots"
        self.tray = _FakeTray()
        self.window = ForegroundWindowSnapshot(
            handle=101,
            app_name="chrome.exe",
            bounds=(0, 0, 20, 12),
        )
        self.meta = CaptureMeta(
            monitor_index=1,
            left=0,
            top=0,
            width=20,
            height=12,
            scale=1.0,
            device_name=r"\\.\DISPLAY1",
            app_name="chrome.exe",
            captured_at="2026-07-27T10:00:00+08:00",
        )
        self.context = BrowserContext(
            url="https://example.test/watch",
            title="测试视频",
            selection="页面上遗留的选中文字",
            video_time=3.5,
            sensitive_input=False,
            tab_id=7,
            observation_text="真正进入收件箱的字幕",
            observation_kind="caption",
            video_key="video-1:0",
        )
        self.current_context: BrowserContext | None = self.context
        self.current_window: ForegroundWindowSnapshot | None = self.window
        self.received: list[tuple[PreparedCard, dict[str, object]]] = []
        self.pool = _ImmediatePool()
        self.coordinator = ObservationCoordinator(
            self.tray,
            self._sink,
            data_dir=self.data_dir,
            screenshot_dir=self.screenshot_dir,
            context_provider=lambda timeout: self.current_context,
            window_provider=lambda: self.current_window,
            initial_capture_provider=lambda: (
                Image.new("RGB", (20, 12), "white"),
                self.meta,
            ),
            capture_provider=lambda meta: (
                Image.new("RGB", (20, 12), "black"),
                meta,
            ),
            selection_provider=lambda image, capture_meta: QRect(1, 2, 8, 4),
            thread_pool=self.pool,
            poll_interval_ms=60_000,
        )

    def tearDown(self) -> None:
        self.coordinator.shutdown()
        for prepared, _metadata in self.received:
            prepared.close()
        self.application.processEvents()
        self.temporary.cleanup()

    def _sink(self, prepared: PreparedCard, **metadata: object) -> bool:
        self.received.append((prepared, metadata))
        return True

    def _start_without_timer(self) -> None:
        self.coordinator.request_start()
        self.coordinator.timer.stop()
        self.assertTrue(self.coordinator.active)

    def _tick_and_flush(self) -> None:
        self.coordinator._tick()
        for _ in range(4):
            self.application.processEvents()

    def test_two_stable_observations_create_one_memory_candidate_with_current_evidence(
        self,
    ) -> None:
        self._start_without_timer()

        self._tick_and_flush()
        self.assertEqual(self.received, [])
        self._tick_and_flush()

        self.assertEqual(len(self.received), 1)
        prepared, metadata = self.received[0]
        self.assertEqual(prepared.card.text, "真正进入收件箱的字幕")
        self.assertEqual(prepared.card.text_source, "dom")
        self.assertEqual(prepared.card.stance, "unknown")
        self.assertEqual(prepared.card.video_time, 3.5)
        self.assertEqual(prepared.selected_image.size, (8, 4))
        self.assertFalse(self.data_dir.exists())
        self.assertIn("session_id", metadata)
        self.assertIn("source_key", metadata)
        self.assertNotIn("\x00", str(metadata["source_key"]))

        self._tick_and_flush()
        self.assertEqual(len(self.received), 1)

    def test_switching_tabs_stops_session_before_any_capture(self) -> None:
        self._start_without_timer()
        self.current_context = BrowserContext(
            url="https://example.test/other",
            title="另一个标签页",
            selection="",
            video_time=None,
            tab_id=8,
            observation_text="不应采集",
            observation_kind="selection",
            video_key="",
        )

        self._tick_and_flush()

        self.assertFalse(self.coordinator.active)
        self.assertEqual(self.received, [])
        self.assertEqual(self.tray.messages[-1][0], "观察已安全暂停")
        self.assertIn("标签页已切换", self.tray.messages[-1][1])

    def test_sensitive_input_stops_session_and_discards_observation_text(self) -> None:
        self._start_without_timer()
        self.current_context = BrowserContext(
            url=self.context.url,
            title=self.context.title,
            selection="",
            video_time=4.0,
            sensitive_input=True,
            tab_id=7,
            observation_text="",
            observation_kind="none",
            video_key="video-1:0",
        )

        self._tick_and_flush()

        self.assertFalse(self.coordinator.active)
        self.assertEqual(self.received, [])
        self.assertIn("密码、验证码或支付", self.tray.messages[-1][1])

    def test_other_foreground_app_only_waits_and_never_grabs_in_background(self) -> None:
        self._start_without_timer()
        self.current_window = ForegroundWindowSnapshot(
            handle=202,
            app_name="notepad.exe",
            bounds=(0, 0, 20, 12),
        )

        self._tick_and_flush()

        self.assertTrue(self.coordinator.active)
        self.assertEqual(self.received, [])
        self.assertEqual(len(self.pool.started), 0)

    def test_sensitive_state_appearing_between_probe_and_grab_discards_frame(self) -> None:
        calls = 0
        sensitive = BrowserContext(
            url=self.context.url,
            title=self.context.title,
            selection="",
            video_time=3.6,
            sensitive_input=True,
            tab_id=7,
            observation_text="",
            observation_kind="none",
            video_key="video-1:0",
        )

        def context_provider(timeout: float) -> BrowserContext:
            nonlocal calls
            del timeout
            calls += 1
            return sensitive if calls >= 5 else self.context

        self.coordinator.context_provider = context_provider
        self._start_without_timer()
        self._tick_and_flush()
        self._tick_and_flush()

        self.assertGreaterEqual(calls, 5)
        self.assertEqual(self.received, [])
        self.assertFalse(self.coordinator.active)
        self.assertEqual(self.tray.messages[-1][0], "观察已安全暂停")

    def test_moving_bound_chrome_window_stops_before_next_probe(self) -> None:
        self._start_without_timer()
        self.current_window = ForegroundWindowSnapshot(
            handle=101,
            app_name="chrome.exe",
            bounds=(10, 0, 30, 12),
        )

        self._tick_and_flush()

        self.assertFalse(self.coordinator.active)
        self.assertEqual(self.received, [])
        self.assertEqual(len(self.pool.started), 0)
        self.assertIn("窗口位置或大小", self.tray.messages[-1][1])

    def test_stopped_probe_must_finish_before_a_new_session_can_start(self) -> None:
        deferred = _DeferredPool()
        coordinator = ObservationCoordinator(
            self.tray,
            self._sink,
            data_dir=self.data_dir,
            screenshot_dir=self.screenshot_dir,
            context_provider=lambda timeout: self.current_context,
            window_provider=lambda: self.current_window,
            initial_capture_provider=lambda: (
                Image.new("RGB", (20, 12), "white"),
                self.meta,
            ),
            selection_provider=lambda image, capture_meta: QRect(1, 2, 8, 4),
            thread_pool=deferred,
            poll_interval_ms=60_000,
        )
        try:
            coordinator.request_start()
            coordinator.timer.stop()
            coordinator._tick()
            self.assertTrue(coordinator.busy)
            self.assertEqual(len(deferred.started), 1)

            coordinator.request_stop()
            self.assertFalse(coordinator.active)
            coordinator.request_start()

            self.assertFalse(coordinator.active)
            self.assertIn("上一会话仍在收尾", self.tray.messages[-1][0])

            deferred.started[0].run()  # type: ignore[attr-defined]
            for _ in range(3):
                self.application.processEvents()
            self.assertFalse(coordinator.busy)

            coordinator.request_start()
            coordinator.timer.stop()
            self.assertTrue(coordinator.active)
        finally:
            coordinator.shutdown()

    def test_unexpected_probe_error_clears_busy_and_stops_session(self) -> None:
        self._start_without_timer()

        def broken_context(_timeout: float) -> BrowserContext:
            raise ValueError("测试提供方故障")

        self.coordinator.context_provider = broken_context
        self._tick_and_flush()

        self.assertFalse(self.coordinator.busy)
        self.assertFalse(self.coordinator.active)
        self.assertEqual(self.received, [])
        self.assertEqual(self.tray.messages[-1][0], "观察已安全暂停")

    def test_extension_missing_at_start_never_opens_region_selector(self) -> None:
        calls: list[str] = []
        coordinator = ObservationCoordinator(
            self.tray,
            self._sink,
            data_dir=self.data_dir,
            screenshot_dir=self.screenshot_dir,
            context_provider=lambda timeout: None,
            window_provider=lambda: self.window,
            initial_capture_provider=lambda: calls.append("capture") or (
                Image.new("RGB", (20, 12), "white"),
                self.meta,
            ),
            selection_provider=lambda image, capture_meta: calls.append("selection") or QRect(),
            thread_pool=_ImmediatePool(),
            poll_interval_ms=60_000,
        )
        try:
            coordinator.request_start()
            self.assertFalse(coordinator.active)
            self.assertEqual(calls, [])
            self.assertIn("扩展未响应", self.tray.messages[-1][1])
        finally:
            coordinator.shutdown()

    def test_sensitive_state_after_initial_grab_never_opens_region_selector(
        self,
    ) -> None:
        calls = 0
        frozen = Image.new("RGB", (20, 12), "white")
        sensitive = BrowserContext(
            url=self.context.url,
            title=self.context.title,
            selection="",
            video_time=self.context.video_time,
            sensitive_input=True,
            tab_id=self.context.tab_id,
            observation_text="",
            observation_kind="none",
            video_key=self.context.video_key,
        )

        def context_provider(_timeout: float) -> BrowserContext:
            nonlocal calls
            calls += 1
            return self.context if calls == 1 else sensitive

        coordinator = ObservationCoordinator(
            self.tray,
            self._sink,
            data_dir=self.data_dir,
            screenshot_dir=self.screenshot_dir,
            context_provider=context_provider,
            window_provider=lambda: self.window,
            initial_capture_provider=lambda: (frozen, self.meta),
            selection_provider=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("抓帧后敏感状态变化时不应打开选区")
            ),
            thread_pool=_ImmediatePool(),
            poll_interval_ms=60_000,
        )
        try:
            coordinator.request_start()

            self.assertFalse(coordinator.active)
            self.assertEqual(self.received, [])
            self.assertEqual(calls, 2)
            with self.assertRaises(ValueError):
                frozen.getpixel((0, 0))
            self.assertIn("未显示或保存", self.tray.messages[-1][1])
        finally:
            coordinator.shutdown()

    def test_stopping_running_capture_worker_eventually_clears_busy(self) -> None:
        deferred = _DeferredPool()
        coordinator = ObservationCoordinator(
            self.tray,
            self._sink,
            data_dir=self.data_dir,
            screenshot_dir=self.screenshot_dir,
            context_provider=lambda timeout: self.current_context,
            window_provider=lambda: self.current_window,
            initial_capture_provider=lambda: (
                Image.new("RGB", (20, 12), "white"),
                self.meta,
            ),
            capture_provider=lambda meta: (
                Image.new("RGB", (20, 12), "black"),
                meta,
            ),
            selection_provider=lambda image, capture_meta: QRect(1, 2, 8, 4),
            thread_pool=deferred,
            poll_interval_ms=60_000,
        )
        try:
            coordinator.request_start()
            coordinator.timer.stop()
            coordinator._tick()
            deferred.started[0].run()  # type: ignore[attr-defined]
            for _ in range(3):
                self.application.processEvents()
            coordinator._tick()
            deferred.started[1].run()  # type: ignore[attr-defined]
            for _ in range(3):
                self.application.processEvents()

            self.assertEqual(len(deferred.started), 3)
            self.assertTrue(coordinator.busy)
            coordinator.request_stop()
            deferred.started[2].run()  # type: ignore[attr-defined]
            for _ in range(3):
                self.application.processEvents()

            self.assertFalse(coordinator.active)
            self.assertFalse(coordinator.busy)
            self.assertEqual(self.received, [])
            coordinator.request_start()
            coordinator.timer.stop()
            self.assertTrue(coordinator.active)
        finally:
            coordinator.shutdown()


if __name__ == "__main__":
    unittest.main()
