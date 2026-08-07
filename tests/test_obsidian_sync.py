"""Obsidian 后台归档调度、合并请求与退出对账测试。"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.obsidian import (  # noqa: E402
    ObsidianError,
    ObsidianSettings,
    ObsidianSettingsError,
    SyncResult,
)
from app.obsidian_sync import ObsidianArchiveManager  # noqa: E402


def _result(root: Path, *, created: int = 0) -> SyncResult:
    return SyncResult(
        card_count=created,
        created_notes=created,
        updated_notes=0,
        unchanged_notes=0,
        copied_attachments=0,
        unchanged_attachments=0,
        conflicts=(),
        managed_root=root,
    )


class _FakeStore:
    def __init__(self) -> None:
        self.listeners: list[object] = []
        self.snapshot_calls = 0
        self.snapshot_items: list[object] = []

    def subscribe_saved_changes(self, callback: object):  # type: ignore[no-untyped-def]
        self.listeners.append(callback)
        active = True

        def unsubscribe() -> None:
            nonlocal active
            if active:
                active = False
                self.listeners.remove(callback)

        return unsubscribe

    def list_saved_snapshot(self, limit: int):  # type: ignore[no-untyped-def]
        self.snapshot_calls += 1
        if limit != 10_001:
            raise AssertionError("同步快照必须有固定上限")
        return self.snapshot_items[:limit]

    def announce_change(self) -> None:
        for callback in tuple(self.listeners):
            callback()  # type: ignore[operator]


class _FakeSettingsStore:
    def __init__(
        self,
        settings: ObsidianSettings | None = None,
        *,
        load_error: ObsidianError | None = None,
    ) -> None:
        self.settings = settings or ObsidianSettings()
        self.load_error = load_error
        self.saved: list[ObsidianSettings] = []

    def load(self) -> ObsidianSettings:
        if self.load_error is not None:
            raise self.load_error
        return self.settings

    def save(self, settings: ObsidianSettings) -> None:
        self.settings = settings
        self.saved.append(settings)


class _FakeMirror:
    def __init__(self, vault: Path) -> None:
        self.vault = vault.resolve()
        self.sync_calls = 0
        self.error: Exception | None = None

    def validate_vault(self, vault_path: str | Path) -> Path:
        candidate = Path(vault_path).resolve()
        if candidate != self.vault:
            raise ObsidianError("测试 Vault 不匹配")
        return candidate

    def managed_root(self, vault_path: str | Path) -> Path:
        return self.validate_vault(vault_path) / "Capture Assistant"

    def sync(self, cards: object, settings: ObsidianSettings) -> SyncResult:
        del cards
        if self.error is not None:
            raise self.error
        self.sync_calls += 1
        return _result(self.managed_root(settings.vault_path or ""), created=1)


class _ImmediatePool:
    def start(self, worker: object, priority: int = 0) -> None:
        del priority
        worker.run()  # type: ignore[attr-defined]


class _DeferredPool:
    def __init__(self) -> None:
        self.workers: list[object] = []

    def start(self, worker: object, priority: int = 0) -> None:
        del priority
        self.workers.append(worker)


class ObsidianArchiveManagerTests(unittest.TestCase):
    application: QApplication

    @classmethod
    def setUpClass(cls) -> None:
        instance = QApplication.instance()
        cls.application = instance if isinstance(instance, QApplication) else QApplication([])

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data_dir = self.root / "data"
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.store = _FakeStore()
        self.mirror = _FakeMirror(self.vault)

    def tearDown(self) -> None:
        self.application.processEvents()
        self.temporary.cleanup()

    def _events(self) -> None:
        for _ in range(4):
            self.application.processEvents()

    def _manager(
        self,
        settings_store: _FakeSettingsStore,
        *,
        pool: object | None = None,
    ) -> ObsidianArchiveManager:
        return ObsidianArchiveManager(
            self.store,  # type: ignore[arg-type]
            data_dir=self.data_dir,
            interval_ms=60_000,
            settings_store=settings_store,  # type: ignore[arg-type]
            mirror=self.mirror,  # type: ignore[arg-type]
            thread_pool=pool or _ImmediatePool(),  # type: ignore[arg-type]
        )

    def test_default_disabled_does_not_touch_vault_or_snapshot(self) -> None:
        manager = self._manager(_FakeSettingsStore())
        manager.start()
        self.store.announce_change()
        self._events()

        self.assertFalse(manager.settings.enabled)
        self.assertEqual(self.store.snapshot_calls, 0)
        self.assertEqual(self.mirror.sync_calls, 0)
        self.assertTrue(manager.shutdown())

    def test_configure_enables_and_saved_change_triggers_immediate_sync(self) -> None:
        settings_store = _FakeSettingsStore()
        manager = self._manager(settings_store)
        completed: list[tuple[SyncResult, bool]] = []
        manager.sync_completed.connect(
            lambda result, manual: completed.append((result, manual))
        )
        manager.start()

        self.assertEqual(manager.configure(self.vault), self.vault.resolve())
        self._events()
        self.store.announce_change()
        self._events()

        self.assertTrue(settings_store.saved[-1].enabled)
        self.assertEqual(self.mirror.sync_calls, 2)
        self.assertEqual([manual for _result_value, manual in completed], [True, False])
        manager.set_enabled(False)
        self.assertTrue(manager.shutdown())

    def test_busy_requests_are_coalesced_into_one_followup(self) -> None:
        deferred = _DeferredPool()
        settings = ObsidianSettings(enabled=True, vault_path=str(self.vault))
        manager = self._manager(_FakeSettingsStore(settings), pool=deferred)
        manager.start()
        self.assertEqual(len(deferred.workers), 1)

        with self.assertRaisesRegex(ObsidianError, "归档尚未完成"):
            manager.set_enabled(False)
        with self.assertRaisesRegex(ObsidianError, "归档尚未完成"):
            manager.set_copy_attachments(True)
        self.assertTrue(manager.settings.enabled)
        self.assertFalse(manager.settings.copy_attachments)

        self.assertFalse(manager.request_sync())
        self.assertFalse(manager.request_sync(manual=True))
        deferred.workers[0].run()  # type: ignore[attr-defined]
        self._events()

        self.assertEqual(len(deferred.workers), 2)
        deferred.workers[1].run()  # type: ignore[attr-defined]
        self._events()
        self.assertEqual(self.mirror.sync_calls, 2)
        manager.set_enabled(False)
        self.assertTrue(manager.shutdown())

    def test_attachment_permission_is_reset_when_switching_vaults(self) -> None:
        settings_store = _FakeSettingsStore(
            ObsidianSettings(
                enabled=False,
                vault_path=str(self.vault.resolve()),
                copy_attachments=True,
            )
        )
        manager = self._manager(settings_store)

        manager.configure(self.vault, enabled=False)
        self.assertTrue(manager.settings.copy_attachments)

        other_vault = self.root / "other-vault"
        other_vault.mkdir()
        self.mirror.vault = other_vault.resolve()
        manager.configure(other_vault, enabled=False)
        self.assertFalse(manager.settings.copy_attachments)
        self.assertTrue(manager.shutdown())

    def test_corrupt_settings_fail_closed_without_writing(self) -> None:
        error = ObsidianSettingsError("Obsidian 设置文件已损坏。")
        manager = self._manager(_FakeSettingsStore(load_error=error))
        failures: list[tuple[str, bool]] = []
        manager.sync_failed.connect(
            lambda message, manual: failures.append((message, manual))
        )
        manager.start()

        self.assertFalse(manager.request_sync(manual=True))
        self.assertFalse(manager.settings.enabled)
        self.assertEqual(manager.configuration_error, str(error))
        self.assertEqual(failures, [(str(error), True)])
        self.assertTrue(manager.shutdown())

    def test_shutdown_performs_final_reconciliation_and_unsubscribes(self) -> None:
        settings = ObsidianSettings(enabled=True, vault_path=str(self.vault))
        manager = self._manager(_FakeSettingsStore(settings))
        manager.start()
        self._events()
        before_shutdown = self.mirror.sync_calls

        self.assertTrue(manager.shutdown())

        self.assertEqual(self.mirror.sync_calls, before_shutdown + 1)
        self.assertEqual(self.store.listeners, [])

    def test_card_limit_fails_before_writing_partial_index(self) -> None:
        self.store.snapshot_items = [object()] * 10_001
        settings = ObsidianSettings(enabled=True, vault_path=str(self.vault))
        manager = self._manager(_FakeSettingsStore(settings))
        failures: list[str] = []
        manager.sync_failed.connect(
            lambda message, _manual: failures.append(message)
        )
        manager.start()
        self._events()

        self.assertEqual(self.mirror.sync_calls, 0)
        self.assertEqual(len(failures), 1)
        self.assertIn("10000", failures[0])
        manager.set_enabled(False)
        self.assertTrue(manager.shutdown())


if __name__ == "__main__":
    unittest.main()
