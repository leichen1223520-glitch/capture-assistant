"""单次框选生成卡片的 DOM/OCR 降级、持久化与回滚测试。"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from shutil import rmtree
from unittest.mock import Mock, patch
from uuid import UUID

from PIL import Image
from PySide6.QtCore import QRect

from app import pipeline as pipeline_module
from app.bridge import BrowserContext
from app.capture import CaptureError, CaptureMeta, save_image as real_save_image
from app.config import DB_PATH
from app.models import Card
from app.ocr import OCRError
from app.pipeline import PipelineError, build_card_from_selection
from app.store import Store, StoreError


class _FailingStore(Store):
    def add_card(self, card):  # type: ignore[no-untyped-def]
        raise StoreError("测试用数据库写入失败")


class _UnexpectedFailingStore(Store):
    def add_card(self, card):  # type: ignore[no-untyped-def]
        raise RuntimeError("测试用非预期写入异常")


class PipelineTests(unittest.TestCase):
    """每项测试使用隔离数据目录，不接触真实屏幕、浏览器或 OCR 模型。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temporary.name) / "data"
        self.screenshot_dir = self.data_dir / "screenshots"
        self.store = Store(
            db_path=self.data_dir / "cards.sqlite3",
            data_dir=self.data_dir,
        )
        self.store.init_db()
        self.image = Image.new("RGB", (12, 8), color="white")
        self.image.putpixel((2, 2), (10, 20, 30))
        self.rect = QRect(2, 2, 5, 3)
        self.meta = CaptureMeta(
            monitor_index=1,
            left=-100,
            top=0,
            width=12,
            height=8,
            scale=1.25,
            device_name=r"\\.\DISPLAY1",
            app_name="chrome.exe",
            captured_at="2026-07-22T10:20:30+08:00",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _build(self, **overrides):  # type: ignore[no-untyped-def]
        arguments = {
            "store": self.store,
            "data_dir": self.data_dir,
            "screenshot_dir": self.screenshot_dir,
        }
        arguments.update(overrides)
        return build_card_from_selection(
            self.image,
            self.meta,
            self.rect,
            **arguments,
        )

    def _context(self, *, selection: str) -> BrowserContext:
        return BrowserContext(
            url="https://example.com/watch?v=local",
            title="本地视频标题",
            selection=selection,
            video_time=42.25,
        )

    def _assert_no_artifacts(self, store: Store | None = None) -> None:
        target_store = store or self.store
        self.assertEqual(target_store.list_recent(), [])
        if self.screenshot_dir.exists():
            self.assertEqual(list(self.screenshot_dir.iterdir()), [])

    def test_dom_selection_has_priority_and_persists_matching_evidence(self) -> None:
        def forbidden_ocr(_image):  # type: ignore[no-untyped-def]
            raise AssertionError("DOM 分支不应执行 OCR")

        card = self._build(
            context_provider=lambda: self._context(selection="  DOM 原文  "),
            ocr_provider=forbidden_ocr,
        )

        self.assertEqual(card.text, "DOM 原文")
        self.assertEqual(card.text_source, "dom")
        self.assertEqual(card.confidence, 0.99)
        self.assertEqual(card.source_url, "https://example.com/watch?v=local")
        self.assertEqual(card.source_title, "本地视频标题")
        self.assertEqual(card.video_time, 42.25)
        self.assertEqual(card.app_name, "chrome.exe")
        self.assertEqual(card.monitor, {"width": 12, "height": 8, "scale": 1.25})
        self.assertEqual(card.created_at, self.meta.captured_at)
        self.assertEqual(card.stance, "unknown")
        self.assertEqual(card.screenshot_path, f"screenshots/{card.id}.png")
        self.assertEqual(
            card.full_screenshot_path,
            f"screenshots/full_{card.id}.png",
        )
        self.assertEqual(self.store.get_card(card.id), card)

        with Image.open(self.data_dir / card.screenshot_path) as selected:
            self.assertEqual(selected.size, (5, 3))
            self.assertEqual(selected.getpixel((0, 0)), (10, 20, 30))
        with Image.open(self.data_dir / card.full_screenshot_path) as full:
            self.assertEqual(full.size, self.image.size)
            self.assertEqual(full.convert("RGB").tobytes(), self.image.tobytes())

    def test_blank_dom_selection_uses_only_cropped_image_for_ocr(self) -> None:
        observed_sizes: list[tuple[int, int]] = []

        def fake_ocr(image):  # type: ignore[no-untyped-def]
            observed_sizes.append(image.size)
            return " OCR 原文 ", 0.73, []

        card = self._build(
            context_provider=lambda: self._context(selection=" \n "),
            ocr_provider=fake_ocr,
        )

        self.assertEqual(observed_sizes, [(5, 3)])
        self.assertEqual(card.text, "OCR 原文")
        self.assertEqual(card.text_source, "ocr")
        self.assertEqual(card.confidence, 0.73)
        self.assertEqual(card.source_title, "本地视频标题")
        self.assertEqual(card.video_time, 42.25)

    def test_public_default_store_is_lazily_initialized(self) -> None:
        default_data = Path(self.temporary.name) / "default-data"
        default_screenshots = default_data / "screenshots"

        card = build_card_from_selection(
            self.image,
            self.meta,
            self.rect,
            context_provider=lambda: self._context(selection="默认仓库卡片"),
            ocr_provider=lambda _image: (_ for _ in ()).throw(
                AssertionError("DOM 分支不应执行 OCR")
            ),
            data_dir=default_data,
            screenshot_dir=default_screenshots,
        )

        reopened = Store(
            db_path=default_data / Path(DB_PATH).name,
            data_dir=default_data,
        )
        self.assertEqual(reopened.get_card(card.id), card)
        self.assertTrue((default_data / card.screenshot_path).is_file())
        self.assertTrue((default_data / card.full_screenshot_path).is_file())

    def test_default_store_never_uses_configured_database_parent(self) -> None:
        default_data = Path(self.temporary.name) / "isolated-default-data"
        outside_database = Path(self.temporary.name) / "outside" / "leak.sqlite3"

        with patch("app.pipeline.DB_PATH", outside_database):
            card = build_card_from_selection(
                self.image,
                self.meta,
                self.rect,
                context_provider=lambda: self._context(selection="隔离默认仓库"),
                data_dir=default_data,
                screenshot_dir=default_data / "screenshots",
            )

        local_database = default_data / outside_database.name
        self.assertTrue(local_database.is_file())
        self.assertFalse(outside_database.exists())
        reopened = Store(db_path=local_database, data_dir=default_data)
        self.assertEqual(reopened.get_card(card.id), card)

    def test_default_store_cache_recovers_after_database_is_cleared(self) -> None:
        for damage in ("empty", "missing-parent"):
            with self.subTest(damage=damage):
                default_data = Path(self.temporary.name) / f"recover-{damage}"
                screenshot_dir = default_data / "screenshots"
                first = build_card_from_selection(
                    self.image,
                    self.meta,
                    self.rect,
                    context_provider=lambda: self._context(selection="损坏前卡片"),
                    data_dir=default_data,
                    screenshot_dir=screenshot_dir,
                )
                database = default_data / Path(DB_PATH).name
                self.assertTrue(database.is_file())

                if damage == "empty":
                    for suffix in ("-wal", "-shm"):
                        Path(f"{database}{suffix}").unlink(missing_ok=True)
                    database.write_bytes(b"")
                else:
                    rmtree(default_data)

                second = build_card_from_selection(
                    self.image,
                    self.meta,
                    self.rect,
                    context_provider=lambda: self._context(selection="自愈后卡片"),
                    data_dir=default_data,
                    screenshot_dir=screenshot_dir,
                )

                reopened = Store(db_path=database, data_dir=default_data)
                self.assertEqual(reopened.get_card(second.id), second)
                self.assertIsNone(reopened.get_card(first.id))

    def test_default_store_error_invalidates_cached_instance(self) -> None:
        default_data = (Path(self.temporary.name) / "invalidate-cache").resolve()
        screenshot_dir = default_data / "screenshots"
        first = build_card_from_selection(
            self.image,
            self.meta,
            self.rect,
            context_provider=lambda: self._context(selection="已有卡片"),
            data_dir=default_data,
            screenshot_dir=screenshot_dir,
        )
        cached = pipeline_module._DEFAULT_STORES[default_data]
        existing_files = set(screenshot_dir.iterdir())
        cached.add_card = Mock(side_effect=StoreError("测试用缓存仓库失败"))

        with self.assertRaises(PipelineError):
            build_card_from_selection(
                self.image,
                self.meta,
                self.rect,
                context_provider=lambda: self._context(selection="失败卡片"),
                data_dir=default_data,
                screenshot_dir=screenshot_dir,
            )

        self.assertNotIn(default_data, pipeline_module._DEFAULT_STORES)
        self.assertEqual(set(screenshot_dir.iterdir()), existing_files)

        recovered = build_card_from_selection(
            self.image,
            self.meta,
            self.rect,
            context_provider=lambda: self._context(selection="恢复卡片"),
            data_dir=default_data,
            screenshot_dir=screenshot_dir,
        )
        self.assertIsNot(pipeline_module._DEFAULT_STORES[default_data], cached)
        reopened = Store(
            db_path=default_data / Path(DB_PATH).name,
            data_dir=default_data,
        )
        self.assertEqual(reopened.get_card(first.id), first)
        self.assertEqual(reopened.get_card(recovered.id), recovered)

    def test_non_browser_foreground_ignores_background_dom_context(self) -> None:
        context_calls = 0

        def should_not_read_context() -> BrowserContext:
            nonlocal context_calls
            context_calls += 1
            return self._context(selection="错误关联的后台浏览器文字")

        card = build_card_from_selection(
            self.image,
            replace(self.meta, app_name="notepad.exe"),
            self.rect,
            store=self.store,
            context_provider=should_not_read_context,
            ocr_provider=lambda _image: ("记事本选区 OCR", 0.76, []),
            data_dir=self.data_dir,
            screenshot_dir=self.screenshot_dir,
        )

        self.assertEqual(context_calls, 0)
        self.assertEqual(card.text, "记事本选区 OCR")
        self.assertEqual(card.text_source, "ocr")
        self.assertIsNone(card.source_url)
        self.assertIsNone(card.source_title)
        self.assertIsNone(card.video_time)

    def test_chromium_process_match_is_case_insensitive_and_exe_optional(self) -> None:
        for app_name in (
            "MsEdGe",
            "BRAVE.EXE",
            "vivaldi.exe",
            "opera_gx",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        ):
            with self.subTest(app_name=app_name):
                card = build_card_from_selection(
                    self.image,
                    replace(self.meta, app_name=app_name),
                    self.rect,
                    store=self.store,
                    context_provider=lambda: self._context(selection="浏览器原文"),
                    ocr_provider=lambda _image: (_ for _ in ()).throw(
                        AssertionError("支持的 Chromium 进程不应执行 OCR")
                    ),
                    data_dir=self.data_dir,
                    screenshot_dir=self.screenshot_dir,
                )
                self.assertEqual(card.text_source, "dom")
                self.store.delete_card(card.id)

    def test_bridge_timeout_or_error_degrades_to_ocr(self) -> None:
        for bridge_error in (TimeoutError("timeout"), RuntimeError("closed")):
            with self.subTest(error=type(bridge_error).__name__):
                def unavailable():
                    raise bridge_error

                card = self._build(
                    context_provider=unavailable,
                    ocr_provider=lambda _image: ("离线结果", 0.81, []),
                )
                self.assertEqual(card.text_source, "ocr")
                self.assertIsNone(card.source_url)
                self.store.delete_card(card.id)

    def test_cancel_empty_rect_and_empty_ocr_leave_no_artifacts(self) -> None:
        cases = (
            (None, lambda _image: ("不会调用", 1.0, [])),
            (QRect(), lambda _image: ("不会调用", 1.0, [])),
            (QRect(50, 50, 2, 2), lambda _image: ("不会调用", 1.0, [])),
            (self.rect, lambda _image: (" \n ", 0.0, [])),
        )
        for rect, ocr_provider in cases:
            with self.subTest(rect=rect):
                with self.assertRaises(PipelineError):
                    build_card_from_selection(
                        self.image,
                        self.meta,
                        rect,
                        store=self.store,
                        context_provider=lambda: None,
                        ocr_provider=ocr_provider,
                        data_dir=self.data_dir,
                        screenshot_dir=self.screenshot_dir,
                    )
                self._assert_no_artifacts()

    def test_ocr_error_leaves_no_artifacts(self) -> None:
        def failing_ocr(_image):  # type: ignore[no-untyped-def]
            raise OCRError("测试用 OCR 失败")

        with self.assertRaises(OCRError):
            self._build(
                context_provider=lambda: None,
                ocr_provider=failing_ocr,
            )

        self._assert_no_artifacts()

    def test_reversed_and_partly_outside_rects_use_clipped_pixels(self) -> None:
        cases = (
            (QRect(-3, -2, 5, 4), (2, 2)),
            (QRect(7, 5, -5, -3), (5, 3)),
            (QRect(10, 6, 10, 10), (2, 2)),
        )
        for rect, expected_size in cases:
            with self.subTest(rect=rect):
                observed_sizes: list[tuple[int, int]] = []

                def fake_ocr(image):  # type: ignore[no-untyped-def]
                    observed_sizes.append(image.size)
                    return "边界选区", 0.88, []

                card = build_card_from_selection(
                    self.image,
                    self.meta,
                    rect,
                    store=self.store,
                    context_provider=lambda: None,
                    ocr_provider=fake_ocr,
                    data_dir=self.data_dir,
                    screenshot_dir=self.screenshot_dir,
                )
                self.assertEqual(observed_sizes, [expected_size])
                with Image.open(self.data_dir / card.screenshot_path) as selected:
                    self.assertEqual(selected.size, expected_size)
                self.store.delete_card(card.id)

    def test_rejects_image_and_capture_metadata_size_mismatch(self) -> None:
        context_called = False

        def forbidden_context() -> None:
            nonlocal context_called
            context_called = True
            return None

        with self.assertRaises(PipelineError):
            build_card_from_selection(
                self.image,
                replace(self.meta, width=self.meta.width + 1),
                self.rect,
                store=self.store,
                context_provider=forbidden_context,
                ocr_provider=lambda _image: ("不会调用", 1.0, []),
                data_dir=self.data_dir,
                screenshot_dir=self.screenshot_dir,
            )

        self.assertFalse(context_called)
        self._assert_no_artifacts()

    def test_partial_file_save_failure_removes_both_targets(self) -> None:
        for fail_at in (1, 2):
            for error_type in (CaptureError, RuntimeError, ValueError):
                with self.subTest(fail_at=fail_at, error=error_type.__name__):
                    calls = 0

                    def flaky_save(image, path):  # type: ignore[no-untyped-def]
                        nonlocal calls
                        calls += 1
                        if calls != fail_at:
                            return real_save_image(image, path)
                        destination = Path(path)
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        destination.write_bytes(b"partial")
                        raise error_type("测试用截图失败")

                    with patch("app.pipeline.save_image", side_effect=flaky_save):
                        with self.assertRaises(PipelineError):
                            self._build(
                                context_provider=lambda: self._context(
                                    selection="DOM 原文"
                                ),
                            )

                    self._assert_no_artifacts()

    def test_database_uuid_collision_preserves_existing_record(self) -> None:
        fixed_id = UUID("87654321-1234-4234-9234-123456789abc")
        existing = Card(
            id=str(fixed_id),
            text="必须保留的旧卡片",
            text_source="ocr",
            confidence=0.66,
            screenshot_path="screenshots/legacy-selected.png",
            full_screenshot_path="screenshots/legacy-full-missing.png",
            app_name="notepad.exe",
            monitor={"width": 12, "height": 8, "scale": 1.25},
            created_at="2026-07-20T08:00:00+08:00",
        )
        legacy_selected = self.data_dir / existing.screenshot_path
        legacy_selected.parent.mkdir(parents=True, exist_ok=True)
        legacy_selected.write_bytes(b"legacy-evidence")
        self.store.add_card(existing)

        with patch("app.pipeline.uuid4", return_value=fixed_id):
            with self.assertRaises(PipelineError):
                self._build(
                    context_provider=lambda: self._context(selection="新 DOM 原文"),
                )

        self.assertEqual(self.store.get_card(str(fixed_id)), existing)
        self.assertEqual(legacy_selected.read_bytes(), b"legacy-evidence")
        self.assertFalse((self.data_dir / existing.full_screenshot_path).exists())
        self.assertFalse((self.screenshot_dir / f"{fixed_id}.png").exists())
        self.assertFalse((self.screenshot_dir / f"full_{fixed_id}.png").exists())

    def test_database_failures_only_remove_new_screenshots(self) -> None:
        for store_type in (_FailingStore, _UnexpectedFailingStore):
            with self.subTest(store=store_type.__name__):
                store = store_type(
                    db_path=self.data_dir / f"{store_type.__name__}.sqlite3",
                    data_dir=self.data_dir,
                )
                store.init_db()
                with self.assertRaises(PipelineError):
                    self._build(
                        store=store,
                        context_provider=lambda: self._context(selection="DOM 原文"),
                    )
                self._assert_no_artifacts(store)

    def test_rejects_database_path_outside_or_equal_to_data_root(self) -> None:
        outside_database = Path(self.temporary.name) / "outside" / "cards.sqlite3"
        stores = (
            Store(db_path=outside_database, data_dir=self.data_dir),
            Store(db_path=self.data_dir, data_dir=self.data_dir),
        )
        for invalid_store in stores:
            with self.subTest(db_path=invalid_store.db_path):
                with self.assertRaises(PipelineError):
                    self._build(
                        store=invalid_store,
                        context_provider=lambda: self._context(selection="DOM 原文"),
                    )

        self.assertFalse(outside_database.exists())
        self._assert_no_artifacts()

    def test_uuid_collision_never_overwrites_existing_screenshot(self) -> None:
        fixed_id = UUID("12345678-1234-4234-9234-123456789abc")
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        existing = self.screenshot_dir / f"{fixed_id}.png"
        existing.write_bytes(b"existing-user-evidence")

        with patch("app.pipeline.uuid4", return_value=fixed_id):
            with self.assertRaises(PipelineError):
                self._build(
                    context_provider=lambda: self._context(selection="DOM 原文"),
                )

        self.assertEqual(existing.read_bytes(), b"existing-user-evidence")
        self.assertFalse((self.screenshot_dir / f"full_{fixed_id}.png").exists())
        self.assertEqual(self.store.list_recent(), [])

    def test_rejects_screenshot_directory_outside_data_boundary(self) -> None:
        outside = Path(self.temporary.name) / "outside"

        with self.assertRaises(PipelineError):
            self._build(
                screenshot_dir=outside,
                context_provider=lambda: self._context(selection="DOM 原文"),
            )

        self.assertFalse(outside.exists())
        self._assert_no_artifacts()

    def test_rejects_store_and_pipeline_data_directory_mismatch(self) -> None:
        other_data = Path(self.temporary.name) / "other-data"
        mismatched = Store(db_path=other_data / "cards.sqlite3", data_dir=other_data)
        mismatched.init_db()

        with self.assertRaises(PipelineError):
            self._build(
                store=mismatched,
                context_provider=lambda: self._context(selection="DOM 原文"),
            )

        self._assert_no_artifacts()
        self.assertEqual(mismatched.list_recent(), [])


if __name__ == "__main__":
    unittest.main()
