from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from app.models import Card
from app.obsidian import (
    ATTACHMENTS_FOLDER,
    CARDS_FOLDER,
    INDEX_FILENAME,
    MANAGED_FOLDER,
    MANIFEST_FILENAME,
    MARKER_FILENAME,
    ObsidianConflictError,
    ObsidianError,
    ObsidianMirror,
    ObsidianSettings,
    ObsidianSettingsError,
    ObsidianSettingsStore,
    ObsidianVaultError,
)

PNG = b"\x89PNG\r\n\x1a\nsmall-local-test-png"
PNG_OTHER = b"\x89PNG\r\n\x1a\nother-local-test-png"
USER_START = "<!-- capture-assistant:user:start -->"
USER_END = "<!-- capture-assistant:user:end -->"


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    return vault


def _data_dir(tmp_path: Path) -> Path:
    data_dir = tmp_path / "data"
    (data_dir / "screenshots").mkdir(parents=True)
    return data_dir


def _card(
    *,
    card_id: str | None = None,
    text: str = "真正值得保存的是证据。",
    screenshot_path: str = "screenshots/selection.png",
    full_screenshot_path: str = "screenshots/full.png",
    **updates: object,
) -> Card:
    values: dict[str, object] = {
        "id": card_id or str(uuid4()),
        "text": text,
        "edited_text": None,
        "text_source": "ocr",
        "confidence": 0.912345,
        "screenshot_path": screenshot_path,
        "full_screenshot_path": full_screenshot_path,
        "source_url": "https://example.com/article?token=secret#private",
        "source_title": "示例来源",
        "video_time": 12.5,
        "created_at": "2026-08-08T09:10:11+08:00",
        "stance": "unknown",
        "note": "",
    }
    values.update(updates)
    return Card(**values)


def _enabled(vault: Path, *, copy_attachments: bool = False) -> ObsidianSettings:
    return ObsidianSettings(
        enabled=True,
        vault_path=str(vault),
        copy_attachments=copy_attachments,
    )


def _note_path(vault: Path, card: Card) -> Path:
    return vault / MANAGED_FOLDER / CARDS_FOLDER / f"{card.id}.md"


def test_settings_default_is_disabled_and_missing_file_is_not_created(tmp_path: Path) -> None:
    store = ObsidianSettingsStore(tmp_path / "data")

    assert store.load() == ObsidianSettings()
    assert store.path == tmp_path / "data" / "integrations" / "obsidian.json"
    assert not store.path.exists()


def test_settings_round_trip_uses_expected_schema_and_atomic_file(tmp_path: Path) -> None:
    store = ObsidianSettingsStore(tmp_path / "data")
    settings = ObsidianSettings(
        enabled=True,
        vault_path=str(tmp_path / "我的资料库"),
        copy_attachments=True,
    )

    store.save(settings)

    assert store.load() == settings
    assert json.loads(store.path.read_text(encoding="utf-8")) == {
        "schema": 1,
        "enabled": True,
        "vault_path": str(tmp_path / "我的资料库"),
        "copy_attachments": True,
    }
    assert list(store.path.parent.glob("*.tmp")) == []


@pytest.mark.parametrize(
    "payload",
    [
        "{broken",
        json.dumps({"schema": 2, "enabled": False, "vault_path": None, "copy_attachments": False}),
        json.dumps({"schema": 1, "enabled": False, "vault_path": None}),
        json.dumps(
            {
                "schema": 1,
                "enabled": False,
                "vault_path": None,
                "copy_attachments": False,
                "unexpected": True,
            }
        ),
    ],
)
def test_settings_corruption_or_unknown_schema_fails_without_rewrite(
    tmp_path: Path, payload: str
) -> None:
    store = ObsidianSettingsStore(tmp_path / "data")
    store.path.parent.mkdir(parents=True)
    store.path.write_text(payload, encoding="utf-8")
    original = store.path.read_bytes()

    with pytest.raises(ObsidianSettingsError):
        store.load()

    assert store.path.read_bytes() == original


def test_disabled_sync_does_not_enumerate_cards_or_touch_vault(tmp_path: Path) -> None:
    mirror = ObsidianMirror(tmp_path / "data")

    def exploding_cards():
        raise AssertionError("disabled sync must not enumerate cards")
        yield  # pragma: no cover

    result = mirror.sync(exploding_cards(), ObsidianSettings())

    assert result.card_count == 0
    assert result.managed_root is None
    assert not result.changed


@pytest.mark.parametrize(
    "invalid_path",
    ["relative/vault", r"\\server\share\vault", "//server/share/vault", "bad\x00vault"],
)
def test_vault_rejects_relative_unc_and_nul_paths(tmp_path: Path, invalid_path: str) -> None:
    mirror = ObsidianMirror(tmp_path / "data")

    with pytest.raises(ObsidianVaultError):
        mirror.validate_vault(invalid_path)


def test_vault_must_exist_and_directly_contain_obsidian_directory(tmp_path: Path) -> None:
    mirror = ObsidianMirror(tmp_path / "data")
    plain = tmp_path / "plain"
    plain.mkdir()
    nested = tmp_path / "nested"
    (nested / "child" / ".obsidian").mkdir(parents=True)

    with pytest.raises(ObsidianVaultError):
        mirror.validate_vault(tmp_path / "missing")
    with pytest.raises(ObsidianVaultError):
        mirror.validate_vault(plain)
    with pytest.raises(ObsidianVaultError):
        mirror.validate_vault(nested)


def test_vault_and_metadata_symlinks_are_rejected_when_supported(tmp_path: Path) -> None:
    mirror = ObsidianMirror(tmp_path / "data")
    real = _vault(tmp_path)
    linked = tmp_path / "linked-vault"
    try:
        linked.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("当前 Windows 权限不允许创建测试符号链接")

    with pytest.raises(ObsidianVaultError):
        mirror.validate_vault(linked)

    other = tmp_path / "other"
    other.mkdir()
    (real / ".obsidian").rmdir()
    (real / ".obsidian").symlink_to(other, target_is_directory=True)
    with pytest.raises(ObsidianVaultError):
        mirror.validate_vault(real)


def test_nonempty_unmarked_managed_directory_is_never_claimed(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    data_dir = _data_dir(tmp_path)
    managed = vault / MANAGED_FOLDER
    managed.mkdir()
    foreign = managed / "我的原文件.md"
    foreign.write_text("不得覆盖", encoding="utf-8")

    with pytest.raises(ObsidianVaultError, match="缺少管理标记"):
        ObsidianMirror(data_dir).sync([], _enabled(vault))

    assert foreign.read_text(encoding="utf-8") == "不得覆盖"
    assert not (managed / MARKER_FILENAME).exists()


def test_first_sync_creates_marker_notes_and_index_without_touching_dot_obsidian(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    sentinel = vault / ".obsidian" / "app.json"
    sentinel.write_text('{"theme":"system"}', encoding="utf-8")
    data_dir = _data_dir(tmp_path)
    card = _card()

    result = ObsidianMirror(data_dir).sync([card], _enabled(vault))

    managed = vault / MANAGED_FOLDER
    assert result.card_count == 1
    assert result.created_notes == 1
    assert result.index_updated
    assert result.changed
    assert result.conflicts == ()
    assert result.managed_root == managed
    assert json.loads((managed / MARKER_FILENAME).read_text(encoding="utf-8")) == {
        "owner": "capture-assistant",
        "schema": 1,
    }
    assert (managed / MANIFEST_FILENAME).is_file()
    assert _note_path(vault, card).is_file()
    assert (managed / INDEX_FILENAME).is_file()
    assert sentinel.read_text(encoding="utf-8") == '{"theme":"system"}'
    assert {item.name for item in (vault / ".obsidian").iterdir()} == {"app.json"}


def test_untrusted_fields_are_fenced_frontmatter_is_fixed_and_url_is_sanitized(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    data_dir = _data_dir(tmp_path)
    card = _card(
        text="原文含 ``` 围栏\n</script>",
        edited_text="---\n恶意: true",
        source_title="标题\n---\nplugins: [evil]",
        note="![[不应执行]]",
        source_url="https://example.com/a path?q=top-secret#private-part",
    )

    ObsidianMirror(data_dir).sync([card], _enabled(vault))
    note = _note_path(vault, card).read_text(encoding="utf-8")
    assert note.startswith("---\n")
    frontmatter = note.split("---", 2)[1]
    assert "capture-assistant/stance/unknown" in frontmatter

    assert "标题" not in frontmatter
    assert "恶意" not in frontmatter
    assert "不应执行" not in frontmatter
    assert "top-secret" not in note
    assert "private-part" not in note
    assert "https://example.com/a%20path" in note
    assert "````text\n原文含 ``` 围栏" in note
    assert "```text\n---\n恶意: true\n```" in note
    assert "```text\n标题\n---\nplugins: [evil]\n```" in note
    assert "```text\n![[不应执行]]\n```" in note


def test_non_http_url_is_plain_fenced_data_not_a_link(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    card = _card(source_url="javascript:alert(1)")

    ObsidianMirror(_data_dir(tmp_path)).sync([card], _enabled(vault))
    note = _note_path(vault, card).read_text(encoding="utf-8")

    assert "[打开已去除查询参数的来源]" not in note
    assert "```text\njavascript:alert(1)\n```" in note


def test_second_identical_sync_is_idempotent_and_does_not_rewrite(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    data_dir = _data_dir(tmp_path)
    card = _card()
    mirror = ObsidianMirror(data_dir)
    mirror.sync([card], _enabled(vault))
    managed = vault / MANAGED_FOLDER
    watched = [
        _note_path(vault, card),
        managed / INDEX_FILENAME,
        managed / MANIFEST_FILENAME,
        managed / MARKER_FILENAME,
    ]
    timestamps = {path: path.stat().st_mtime_ns for path in watched}

    result = mirror.sync([card], _enabled(vault))

    assert result.created_notes == 0
    assert result.updated_notes == 0
    assert result.unchanged_notes == 1
    assert not result.index_updated
    assert not result.changed
    assert result.conflicts == ()
    assert {path: path.stat().st_mtime_ns for path in watched} == timestamps


def test_user_block_is_preserved_when_managed_card_changes(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    data_dir = _data_dir(tmp_path)
    card = _card()
    mirror = ObsidianMirror(data_dir)
    mirror.sync([card], _enabled(vault))
    path = _note_path(vault, card)
    original = path.read_text(encoding="utf-8")
    user_text = "\n我在 Obsidian 中追加的联想。\n- [ ] 稍后验证\n"
    path.write_text(
        original.replace(USER_START + "\n\n" + USER_END, USER_START + user_text + USER_END),
        encoding="utf-8",
    )
    changed = card.model_copy(update={"note": "数据库中的新备注"})

    result = mirror.sync([changed], _enabled(vault))
    updated = path.read_text(encoding="utf-8")

    assert result.updated_notes == 1
    assert result.conflicts == ()
    assert user_text in updated
    assert "数据库中的新备注" in updated


def test_managed_region_tampering_is_reported_and_never_overwritten(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    data_dir = _data_dir(tmp_path)
    card = _card()
    mirror = ObsidianMirror(data_dir)
    mirror.sync([card], _enabled(vault))
    path = _note_path(vault, card)
    tampered = path.read_text(encoding="utf-8").replace("## 提取原文", "## 被篡改的原文")
    path.write_text(tampered, encoding="utf-8")

    result = mirror.sync([card.model_copy(update={"note": "不会写入"})], _enabled(vault))

    assert any("受管区域被修改" in conflict for conflict in result.conflicts)
    assert result.updated_notes == 0
    assert path.read_text(encoding="utf-8") == tampered
    assert "不会写入" not in path.read_text(encoding="utf-8")


def test_foreign_note_at_uuid_path_is_a_conflict_and_not_overwritten(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    data_dir = _data_dir(tmp_path)
    mirror = ObsidianMirror(data_dir)
    mirror.sync([], _enabled(vault))
    card = _card()
    path = _note_path(vault, card)
    path.write_text("这是用户自己的同名笔记", encoding="utf-8")

    result = mirror.sync([card], _enabled(vault))

    assert len(result.conflicts) == 1
    assert path.read_text(encoding="utf-8") == "这是用户自己的同名笔记"


def test_exact_managed_notes_are_recovered_after_manifest_was_not_committed(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    data_dir = _data_dir(tmp_path)
    card = _card()
    mirror = ObsidianMirror(data_dir)
    mirror.sync([card], _enabled(vault))
    note = _note_path(vault, card)
    original_note = note.read_bytes()
    manifest = vault / MANAGED_FOLDER / MANIFEST_FILENAME
    manifest.unlink()

    recovered = mirror.sync([card], _enabled(vault))

    assert recovered.conflicts == ()
    assert recovered.unchanged_notes == 1
    assert note.read_bytes() == original_note
    assert manifest.is_file()


def test_exact_updated_note_recovers_when_manifest_still_has_previous_hash(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    data_dir = _data_dir(tmp_path)
    card = _card()
    changed = card.model_copy(update={"note": "更新后的本地备注"})
    mirror = ObsidianMirror(data_dir)
    mirror.sync([card], _enabled(vault))
    manifest = vault / MANAGED_FOLDER / MANIFEST_FILENAME
    previous_manifest = manifest.read_bytes()
    mirror.sync([changed], _enabled(vault))
    updated_note = _note_path(vault, card).read_bytes()
    manifest.write_bytes(previous_manifest)

    recovered = mirror.sync([changed], _enabled(vault))

    assert recovered.conflicts == ()
    assert recovered.unchanged_notes == 1
    assert _note_path(vault, card).read_bytes() == updated_note


def test_index_tampering_is_a_conflict_and_not_overwritten(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    data_dir = _data_dir(tmp_path)
    mirror = ObsidianMirror(data_dir)
    card = _card()
    mirror.sync([card], _enabled(vault))
    index = vault / MANAGED_FOLDER / INDEX_FILENAME
    tampered = index.read_text(encoding="utf-8").replace("# Capture Assistant", "# 用户改写")
    index.write_text(tampered, encoding="utf-8")

    result = mirror.sync([card.model_copy(update={"stance": "agree"})], _enabled(vault))

    assert any("索引未覆盖" in conflict for conflict in result.conflicts)
    assert index.read_text(encoding="utf-8") == tampered


def test_full_reconcile_does_not_delete_orphan_note_or_manifest_entry(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    data_dir = _data_dir(tmp_path)
    mirror = ObsidianMirror(data_dir)
    card = _card()
    mirror.sync([card], _enabled(vault))
    note = _note_path(vault, card)

    result = mirror.sync([], _enabled(vault))
    manifest = json.loads(
        (vault / MANAGED_FOLDER / MANIFEST_FILENAME).read_text(encoding="utf-8")
    )
    index = (vault / MANAGED_FOLDER / INDEX_FILENAME).read_text(encoding="utf-8")

    assert note.is_file()
    assert card.id in manifest["cards"]
    assert card.id not in index
    assert result.card_count == 0
    assert result.index_updated


def test_corrupt_marker_or_manifest_stops_sync_without_overwrite(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    data_dir = _data_dir(tmp_path)
    mirror = ObsidianMirror(data_dir)
    card = _card()
    mirror.sync([card], _enabled(vault))
    note = _note_path(vault, card)
    original = note.read_bytes()
    manifest = vault / MANAGED_FOLDER / MANIFEST_FILENAME
    manifest.write_text("{broken", encoding="utf-8")

    with pytest.raises(ObsidianVaultError, match="清单已损坏"):
        mirror.sync([card.model_copy(update={"note": "不得落盘"})], _enabled(vault))
    assert note.read_bytes() == original

    manifest.unlink()
    marker = vault / MANAGED_FOLDER / MARKER_FILENAME
    marker.write_text('{"schema":99,"owner":"other"}', encoding="utf-8")
    with pytest.raises(ObsidianVaultError, match="管理标记"):
        mirror.sync([card], _enabled(vault))
    assert note.read_bytes() == original


def test_attachments_are_opt_in_and_only_selection_png_is_copied(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    data_dir = _data_dir(tmp_path)
    (data_dir / "screenshots" / "selection.png").write_bytes(PNG)
    (data_dir / "screenshots" / "full.png").write_bytes(PNG_OTHER)
    card = _card()
    mirror = ObsidianMirror(data_dir)

    disabled_result = mirror.sync([card], _enabled(vault, copy_attachments=False))
    assert disabled_result.copied_attachments == 0
    assert not (vault / MANAGED_FOLDER / ATTACHMENTS_FOLDER).exists()
    assert "![[附件/" not in _note_path(vault, card).read_text(encoding="utf-8")

    enabled_result = mirror.sync([card], _enabled(vault, copy_attachments=True))
    attachment = vault / MANAGED_FOLDER / ATTACHMENTS_FOLDER / f"{card.id}.png"

    assert enabled_result.copied_attachments == 1
    assert enabled_result.updated_notes == 1
    assert attachment.read_bytes() == PNG
    assert len(list((vault / MANAGED_FOLDER / ATTACHMENTS_FOLDER).iterdir())) == 1
    assert f"![[附件/{card.id}.png]]" in _note_path(vault, card).read_text(encoding="utf-8")

    idempotent = mirror.sync([card], _enabled(vault, copy_attachments=True))
    assert idempotent.unchanged_attachments == 1
    assert idempotent.copied_attachments == 0


@pytest.mark.parametrize(
    ("relative_path", "content", "message"),
    [
        ("outside.png", PNG, "screenshots"),
        ("screenshots/not-png.jpg", PNG, "PNG"),
        ("screenshots/fake.png", b"not-a-png", "PNG"),
        ("screenshots/missing.png", None, "不存在"),
    ],
)
def test_invalid_selection_attachments_are_conflicts_but_text_note_still_syncs(
    tmp_path: Path,
    relative_path: str,
    content: bytes | None,
    message: str,
) -> None:
    vault = _vault(tmp_path)
    data_dir = _data_dir(tmp_path)
    target = data_dir / relative_path
    if content is not None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    card = _card(screenshot_path=relative_path)

    result = ObsidianMirror(data_dir).sync(
        [card], _enabled(vault, copy_attachments=True)
    )

    assert result.created_notes == 1
    assert result.copied_attachments == 0
    assert any(message in conflict for conflict in result.conflicts)
    assert "![[附件/" not in _note_path(vault, card).read_text(encoding="utf-8")


def test_different_existing_attachment_is_a_conflict_and_is_not_overwritten(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    data_dir = _data_dir(tmp_path)
    (data_dir / "screenshots" / "selection.png").write_bytes(PNG)
    mirror = ObsidianMirror(data_dir)
    mirror.sync([], _enabled(vault, copy_attachments=True))
    card = _card()
    attachment = vault / MANAGED_FOLDER / ATTACHMENTS_FOLDER / f"{card.id}.png"
    attachment.write_bytes(PNG_OTHER)

    result = mirror.sync([card], _enabled(vault, copy_attachments=True))

    assert any("已有不同内容" in conflict for conflict in result.conflicts)
    assert attachment.read_bytes() == PNG_OTHER
    assert "![[附件/" not in _note_path(vault, card).read_text(encoding="utf-8")


def test_selection_png_symlink_is_rejected_when_supported(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    data_dir = _data_dir(tmp_path)
    real = tmp_path / "real.png"
    real.write_bytes(PNG)
    linked = data_dir / "screenshots" / "selection.png"
    try:
        linked.symlink_to(real)
    except OSError:
        pytest.skip("当前 Windows 权限不允许创建测试符号链接")
    card = _card()

    result = ObsidianMirror(data_dir).sync(
        [card], _enabled(vault, copy_attachments=True)
    )

    assert result.copied_attachments == 0
    assert any("符号链接" in conflict for conflict in result.conflicts)


def test_sync_rejects_non_card_and_duplicate_card_ids(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    mirror = ObsidianMirror(_data_dir(tmp_path))
    card = _card()

    with pytest.raises(ObsidianError, match="只能包含"):
        mirror.sync([object()], _enabled(vault))  # type: ignore[list-item]
    with pytest.raises(ObsidianError, match="重复"):
        mirror.sync([card, card], _enabled(vault))


def test_settings_require_vault_before_enabled_sync(tmp_path: Path) -> None:
    with pytest.raises(ObsidianSettingsError, match="选择 Vault"):
        ObsidianMirror(_data_dir(tmp_path)).sync(
            [], ObsidianSettings(enabled=True, vault_path=None)
        )


def test_conflict_error_is_public_for_manager_boundary() -> None:
    assert issubclass(ObsidianConflictError, ObsidianError)
