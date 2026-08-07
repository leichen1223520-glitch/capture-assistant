"""把已保存观点卡片安全地单向镜像到本地 Obsidian Vault。

本模块只处理文件系统与 Markdown，不依赖 Qt，也不会启动 Obsidian。SQLite 仍是事实来源；
同步只新增或更新本模块拥有的文件，首版绝不删除 Vault 中的孤立笔记或附件。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit
from uuid import UUID

from .models import Card

__all__ = [
    "ATTACHMENTS_FOLDER",
    "CARDS_FOLDER",
    "INDEX_FILENAME",
    "MANAGED_FOLDER",
    "MANIFEST_FILENAME",
    "MARKER_FILENAME",
    "ObsidianConflictError",
    "ObsidianError",
    "ObsidianMirror",
    "ObsidianSettings",
    "ObsidianSettingsError",
    "ObsidianSettingsStore",
    "ObsidianVaultError",
    "SyncResult",
]

SETTINGS_SCHEMA = 1
MIRROR_SCHEMA = 1
MANAGED_FOLDER = "Capture Assistant"
CARDS_FOLDER = "卡片"
ATTACHMENTS_FOLDER = "附件"
INDEX_FILENAME = "索引.md"
MARKER_FILENAME = ".capture-assistant-managed.json"
MANIFEST_FILENAME = ".capture-assistant-manifest.json"

_OWNER = "capture-assistant"
_SETTINGS_KEYS = {"schema", "enabled", "vault_path", "copy_attachments"}
_USER_START = "<!-- capture-assistant:user:start -->"
_USER_END = "<!-- capture-assistant:user:end -->"
_EMPTY_USER_CONTENT = "\n\n"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_MAX_SETTINGS_BYTES = 64 * 1024
_MAX_METADATA_BYTES = 4 * 1024 * 1024
_MAX_NOTE_BYTES = 8 * 1024 * 1024
_MAX_SCREENSHOT_BYTES = 32 * 1024 * 1024
_BACKTICK_RUN = re.compile(r"`+")
_STANCE_LABELS = {
    "unknown": "立场未知",
    "agree": "认同",
    "disagree": "反对",
    "doubt": "存疑",
    "useful": "只是有用",
}


class ObsidianError(RuntimeError):
    """Obsidian 配置、路径或镜像操作失败的基类。"""


class ObsidianSettingsError(ObsidianError):
    """本地 Obsidian 配置缺失、损坏或版本不兼容。"""


class ObsidianVaultError(ObsidianError):
    """目标不是可安全使用的本地 Obsidian Vault。"""


class ObsidianConflictError(ObsidianError):
    """调用方要求把同步冲突作为异常处理时使用的异常类型。"""


@dataclass(frozen=True, slots=True)
class ObsidianSettings:
    """持久化的 Obsidian 集成配置；默认完全关闭。"""

    schema: int = SETTINGS_SCHEMA
    enabled: bool = False
    vault_path: str | None = None
    copy_attachments: bool = False


@dataclass(frozen=True, slots=True)
class SyncResult:
    """一次全量 reconcile 的统计结果，不包含卡片正文。"""

    card_count: int = 0
    created_notes: int = 0
    updated_notes: int = 0
    unchanged_notes: int = 0
    copied_attachments: int = 0
    unchanged_attachments: int = 0
    conflicts: tuple[str, ...] = ()
    managed_root: Path | None = None
    index_updated: bool = False

    @property
    def changed(self) -> bool:
        """是否有笔记、索引或附件实际写入。"""

        return bool(
            self.created_notes
            or self.updated_notes
            or self.copied_attachments
            or self.index_updated
        )


class ObsidianSettingsStore:
    """在数据目录内原子读写 Obsidian 配置 JSON。"""

    def __init__(self, data_dir: str | os.PathLike[str]) -> None:
        self._data_dir = Path(data_dir)
        self.path = self._data_dir / "integrations" / "obsidian.json"

    def load(self) -> ObsidianSettings:
        """读取配置；文件不存在时返回关闭状态，损坏时不做任何写入并报错。"""

        if self.path.is_symlink():
            raise ObsidianSettingsError("Obsidian 配置文件不能是符号链接")
        if not self.path.exists():
            return ObsidianSettings()
        if not self.path.is_file():
            raise ObsidianSettingsError("Obsidian 配置路径不是普通文件")
        try:
            if self.path.stat().st_size > _MAX_SETTINGS_BYTES:
                raise ObsidianSettingsError("Obsidian 配置文件异常过大")
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except ObsidianSettingsError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ObsidianSettingsError("Obsidian 配置文件已损坏，已保持禁用且未改写文件") from exc
        return _settings_from_payload(payload)

    def save(self, settings: ObsidianSettings) -> None:
        """校验后原子保存配置；不会验证 Vault 当前是否在线。"""

        _validate_settings(settings)
        parent = self.path.parent
        _ensure_private_directory(parent, self._data_dir, "Obsidian 配置目录")
        payload = {
            "schema": settings.schema,
            "enabled": settings.enabled,
            "vault_path": settings.vault_path,
            "copy_attachments": settings.copy_attachments,
        }
        encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        _atomic_write(self.path, encoded)


class ObsidianMirror:
    """把完整的已保存 ``Card`` 集合写入一个受管 Vault 子目录。"""

    def __init__(self, data_dir: str | os.PathLike[str]) -> None:
        self._data_dir = Path(data_dir)

    def validate_vault(self, vault_path: str | os.PathLike[str]) -> Path:
        """返回规范化 Vault 路径，仅接受直接含 ``.obsidian`` 的本地绝对目录。"""

        raw = os.fspath(vault_path)
        if not isinstance(raw, str) or not raw.strip():
            raise ObsidianVaultError("请选择一个 Obsidian Vault 文件夹")
        if "\x00" in raw:
            raise ObsidianVaultError("Vault 路径包含无效字符")
        if _looks_like_unc(raw):
            raise ObsidianVaultError("为保护本地隐私，不支持网络或 UNC Vault")

        candidate = Path(raw)
        if not candidate.is_absolute():
            raise ObsidianVaultError("Vault 必须使用本机绝对路径")
        if _is_windows_remote_drive(candidate):
            raise ObsidianVaultError("为保护本地隐私，不支持映射到网络位置的 Vault")
        if ".." in candidate.parts:
            raise ObsidianVaultError("Vault 路径不能包含上级目录跳转")
        if _path_has_symlink(candidate):
            raise ObsidianVaultError("Vault 路径不能经过符号链接")
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ObsidianVaultError("Vault 路径不存在或无法访问") from exc
        if _looks_like_unc(str(resolved)) or _is_windows_remote_drive(resolved):
            raise ObsidianVaultError("为保护本地隐私，不支持指向网络位置的 Vault")
        if not resolved.is_dir():
            raise ObsidianVaultError("Vault 路径必须是文件夹")

        metadata_dir = resolved / ".obsidian"
        if metadata_dir.is_symlink():
            raise ObsidianVaultError("Vault 的 .obsidian 不能是符号链接")
        if not metadata_dir.is_dir():
            raise ObsidianVaultError("所选目录不是 Obsidian Vault：未直接找到 .obsidian 文件夹")
        return resolved

    def managed_root(self, vault_path: str | os.PathLike[str]) -> Path:
        """返回受管目录位置，但不创建或修改任何文件。"""

        return self.validate_vault(vault_path) / MANAGED_FOLDER

    def sync(self, cards: Iterable[Card], settings: ObsidianSettings) -> SyncResult:
        """全量校准已保存卡片。

        禁用时立即返回且不枚举输入。启用时只写受管目录；冲突会汇总在结果中，既不覆盖
        冲突文件，也不删除不再出现在 ``cards`` 中的旧笔记或附件。
        """

        _validate_settings(settings)
        if not settings.enabled:
            return SyncResult()
        if settings.vault_path is None:
            raise ObsidianSettingsError("启用 Obsidian 自动归档前必须先选择 Vault")

        card_list = _normalize_cards(cards)
        vault = self.validate_vault(settings.vault_path)
        root = self._prepare_managed_root(vault)
        cards_dir = _ensure_managed_subdirectory(root, CARDS_FOLDER)
        attachments_dir = (
            _ensure_managed_subdirectory(root, ATTACHMENTS_FOLDER)
            if settings.copy_attachments
            else None
        )

        manifest_path = root / MANIFEST_FILENAME
        manifest, manifest_existed = _load_manifest(manifest_path)
        entries: dict[str, dict[str, str]] = manifest["cards"]
        manifest_dirty = not manifest_existed
        conflicts: list[str] = []
        created = updated = unchanged = copied = unchanged_attachments = 0

        for card in card_list:
            attachment_name: str | None = None
            if settings.copy_attachments and attachments_dir is not None:
                try:
                    attachment_state = self._sync_attachment(card, attachments_dir)
                except ObsidianConflictError as exc:
                    conflicts.append(f"卡片 {card.id} 的选区截图未复制：{exc}")
                else:
                    attachment_name = f"{card.id}.png"
                    if attachment_state == "copied":
                        copied += 1
                    else:
                        unchanged_attachments += 1

            target = cards_dir / f"{card.id}.md"
            rendered = _render_card_note(card, attachment_name, _EMPTY_USER_CONTENT)
            source_hash = _sha256(_managed_projection(rendered).encode("utf-8"))
            existing_entry = entries.get(card.id)
            try:
                state, generated_hash = _upsert_managed_text(
                    target,
                    rendered,
                    source_hash,
                    existing_entry,
                    expected_owner=f"card:{card.id}",
                )
            except ObsidianConflictError as exc:
                conflicts.append(f"卡片 {card.id} 未覆盖：{exc}")
                continue

            if state == "created":
                created += 1
            elif state == "updated":
                updated += 1
            else:
                unchanged += 1
            new_entry = {"source_hash": source_hash, "generated_hash": generated_hash}
            if existing_entry != new_entry:
                entries[card.id] = new_entry
                manifest_dirty = True

        index_rendered = _render_index(card_list, _EMPTY_USER_CONTENT)
        index_source_hash = _sha256(_managed_projection(index_rendered).encode("utf-8"))
        index_updated = False
        try:
            index_state, index_generated_hash = _upsert_managed_text(
                root / INDEX_FILENAME,
                index_rendered,
                index_source_hash,
                manifest.get("index"),
                expected_owner="index",
            )
        except ObsidianConflictError as exc:
            conflicts.append(f"索引未覆盖：{exc}")
        else:
            index_updated = index_state in {"created", "updated"}
            new_index = {
                "source_hash": index_source_hash,
                "generated_hash": index_generated_hash,
            }
            if manifest.get("index") != new_index:
                manifest["index"] = new_index
                manifest_dirty = True

        if manifest_dirty:
            _write_json_atomic(manifest_path, manifest)

        return SyncResult(
            card_count=len(card_list),
            created_notes=created,
            updated_notes=updated,
            unchanged_notes=unchanged,
            copied_attachments=copied,
            unchanged_attachments=unchanged_attachments,
            conflicts=tuple(conflicts),
            managed_root=root,
            index_updated=index_updated,
        )

    def _prepare_managed_root(self, vault: Path) -> Path:
        root = vault / MANAGED_FOLDER
        if root.is_symlink():
            raise ObsidianVaultError("Obsidian 受管目录不能是符号链接")
        if root.exists() and not root.is_dir():
            raise ObsidianVaultError("Obsidian 受管路径已被同名文件占用")
        if not root.exists():
            try:
                root.mkdir()
            except OSError as exc:
                raise ObsidianVaultError("无法创建 Obsidian 受管目录") from exc
        _assert_within(root.resolve(strict=True), vault, "Obsidian 受管目录越过了 Vault 边界")

        marker = root / MARKER_FILENAME
        if not marker.exists():
            try:
                nonempty = next(root.iterdir(), None) is not None
            except OSError as exc:
                raise ObsidianVaultError("无法检查 Obsidian 受管目录") from exc
            if nonempty:
                raise ObsidianVaultError(
                    f"{MANAGED_FOLDER} 已有内容但缺少管理标记，为避免覆盖已停止同步"
                )
            _write_json_atomic(marker, {"schema": MIRROR_SCHEMA, "owner": _OWNER})
        else:
            _validate_marker(marker)
        return root

    def _sync_attachment(self, card: Card, attachments_dir: Path) -> str:
        data = _read_selection_png(self._data_dir, card.screenshot_path)
        target = attachments_dir / f"{card.id}.png"
        if target.is_symlink():
            raise ObsidianConflictError("目标附件是符号链接")
        if target.exists():
            if not target.is_file():
                raise ObsidianConflictError("目标附件不是普通文件")
            try:
                if target.stat().st_size > _MAX_SCREENSHOT_BYTES:
                    raise ObsidianConflictError("目标附件异常过大")
                current = target.read_bytes()
            except OSError as exc:
                raise ObsidianConflictError("无法读取已有附件") from exc
            if current != data:
                raise ObsidianConflictError("目标附件已有不同内容")
            return "unchanged"
        _atomic_write(target, data)
        return "copied"


def _settings_from_payload(payload: Any) -> ObsidianSettings:
    if not isinstance(payload, dict):
        raise ObsidianSettingsError("Obsidian 配置必须是 JSON 对象")
    if payload.get("schema") != SETTINGS_SCHEMA:
        raise ObsidianSettingsError("Obsidian 配置版本不受支持，已保持禁用且未改写文件")
    if set(payload) != _SETTINGS_KEYS:
        raise ObsidianSettingsError("Obsidian 配置字段不完整或包含未知字段")
    settings = ObsidianSettings(
        schema=payload["schema"],
        enabled=payload["enabled"],
        vault_path=payload["vault_path"],
        copy_attachments=payload["copy_attachments"],
    )
    _validate_settings(settings)
    return settings


def _validate_settings(settings: ObsidianSettings) -> None:
    if not isinstance(settings, ObsidianSettings):
        raise ObsidianSettingsError("Obsidian 配置类型无效")
    if type(settings.schema) is not int or settings.schema != SETTINGS_SCHEMA:
        raise ObsidianSettingsError("Obsidian 配置版本不受支持")
    if type(settings.enabled) is not bool or type(settings.copy_attachments) is not bool:
        raise ObsidianSettingsError("Obsidian 开关必须是布尔值")
    if settings.vault_path is not None:
        if not isinstance(settings.vault_path, str) or not settings.vault_path.strip():
            raise ObsidianSettingsError("Vault 路径必须是非空字符串或 null")
        if "\x00" in settings.vault_path:
            raise ObsidianSettingsError("Vault 路径包含无效字符")


def _normalize_cards(cards: Iterable[Card]) -> list[Card]:
    normalized: list[Card] = []
    seen: set[str] = set()
    try:
        iterator = iter(cards)
    except TypeError as exc:
        raise ObsidianError("同步输入必须是已保存 Card 的可迭代集合") from exc
    for card in iterator:
        if not isinstance(card, Card):
            raise ObsidianError("同步输入只能包含已保存的 Card")
        if card.id in seen:
            raise ObsidianError("同步输入包含重复的 Card ID")
        seen.add(card.id)
        normalized.append(card)
    normalized.sort(key=lambda item: (item.created_at, item.id))
    return normalized


def _looks_like_unc(value: str) -> bool:
    normalized = value.replace("/", "\\")
    windows = PureWindowsPath(value)
    return normalized.startswith("\\\\") or str(windows.drive).startswith("\\\\")


def _is_windows_remote_drive(path: Path) -> bool:
    """在 Windows 上拒绝映射网络盘；其他平台的绝对本地路径返回 ``False``。"""

    if os.name != "nt":
        return False
    try:
        import ctypes

        drive_type = ctypes.windll.kernel32.GetDriveTypeW(str(path.anchor))
    except (AttributeError, OSError, ValueError):
        return True
    # 仅明确接受本机可访问的盘类型；DRIVE_UNKNOWN、DRIVE_NO_ROOT_DIR 与
    # DRIVE_REMOTE 都按不安全处理，避免网络映射探测失败时静默放行。
    return drive_type not in {2, 3, 5, 6}


def _path_has_symlink(path: Path) -> bool:
    current = Path(path.anchor)
    parts = path.parts[1:] if path.anchor else path.parts
    for part in parts:
        current /= part
        try:
            if current.is_symlink():
                return True
        except OSError:
            return True
    return False


def _reject_symlink(path: Path, message: str) -> None:
    try:
        if path.is_symlink():
            raise ObsidianError(message)
    except OSError as exc:
        raise ObsidianError(message) from exc


def _assert_within(child: Path, parent: Path, message: str) -> None:
    try:
        child.relative_to(parent)
    except ValueError as exc:
        raise ObsidianVaultError(message) from exc


def _ensure_private_directory(path: Path, boundary: Path, label: str) -> None:
    try:
        boundary.mkdir(parents=True, exist_ok=True)
        boundary_resolved = boundary.resolve(strict=True)
        if boundary.is_symlink():
            raise ObsidianSettingsError(f"{label}的数据目录不能是符号链接")
        path.mkdir(parents=True, exist_ok=True)
        if path.is_symlink() or not path.is_dir():
            raise ObsidianSettingsError(f"{label}不是安全的本地目录")
        _assert_within(path.resolve(strict=True), boundary_resolved, f"{label}越过了数据目录边界")
    except ObsidianError:
        raise
    except OSError as exc:
        raise ObsidianSettingsError(f"无法创建{label}") from exc


def _ensure_managed_subdirectory(root: Path, name: str) -> Path:
    target = root / name
    if target.is_symlink():
        raise ObsidianVaultError(f"受管子目录 {name} 不能是符号链接")
    try:
        target.mkdir(exist_ok=True)
        if not target.is_dir():
            raise ObsidianVaultError(f"受管子目录 {name} 被同名文件占用")
        _assert_within(target.resolve(strict=True), root.resolve(strict=True), "受管子目录越界")
    except ObsidianError:
        raise
    except OSError as exc:
        raise ObsidianVaultError(f"无法创建受管子目录 {name}") from exc
    return target


def _validate_marker(path: Path) -> None:
    _reject_symlink(path, "Obsidian 管理标记不能是符号链接")
    if not path.is_file():
        raise ObsidianVaultError("Obsidian 管理标记不是普通文件")
    try:
        if path.stat().st_size > _MAX_METADATA_BYTES:
            raise ObsidianVaultError("Obsidian 管理标记异常过大")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except ObsidianVaultError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ObsidianVaultError("Obsidian 管理标记已损坏") from exc
    if payload != {"schema": MIRROR_SCHEMA, "owner": _OWNER}:
        raise ObsidianVaultError("Obsidian 管理标记不属于当前版本，已停止同步")


def _empty_manifest() -> dict[str, Any]:
    return {"schema": MIRROR_SCHEMA, "owner": _OWNER, "cards": {}, "index": None}


def _load_manifest(path: Path) -> tuple[dict[str, Any], bool]:
    if path.is_symlink():
        raise ObsidianVaultError("Obsidian 同步清单不能是符号链接")
    if not path.exists():
        return _empty_manifest(), False
    if not path.is_file():
        raise ObsidianVaultError("Obsidian 同步清单不是普通文件")
    try:
        if path.stat().st_size > _MAX_METADATA_BYTES:
            raise ObsidianVaultError("Obsidian 同步清单异常过大")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except ObsidianVaultError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ObsidianVaultError("Obsidian 同步清单已损坏，已停止覆盖文件") from exc
    if not isinstance(payload, dict) or set(payload) != {"schema", "owner", "cards", "index"}:
        raise ObsidianVaultError("Obsidian 同步清单结构无效")
    if payload["schema"] != MIRROR_SCHEMA or payload["owner"] != _OWNER:
        raise ObsidianVaultError("Obsidian 同步清单版本或归属无效")
    if not isinstance(payload["cards"], dict):
        raise ObsidianVaultError("Obsidian 同步清单的卡片记录无效")
    for card_id, entry in payload["cards"].items():
        if not _is_uuid4(card_id) or not _valid_manifest_entry(entry):
            raise ObsidianVaultError("Obsidian 同步清单包含无效卡片记录")
    if payload["index"] is not None and not _valid_manifest_entry(payload["index"]):
        raise ObsidianVaultError("Obsidian 同步清单包含无效索引记录")
    return payload, True


def _valid_manifest_entry(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {"source_hash", "generated_hash"}:
        return False
    return all(
        isinstance(value[key], str)
        and len(value[key]) == 64
        and all(character in "0123456789abcdef" for character in value[key])
        for key in ("source_hash", "generated_hash")
    )


def _is_uuid4(value: str) -> bool:
    try:
        parsed = UUID(value)
    except (AttributeError, TypeError, ValueError):
        return False
    return parsed.version == 4 and str(parsed) == value


def _upsert_managed_text(
    path: Path,
    rendered_with_empty_user: str,
    source_hash: str,
    manifest_entry: Mapping[str, str] | None,
    *,
    expected_owner: str,
) -> tuple[str, str]:
    owner_marker = f"<!-- capture-assistant:owner:{expected_owner} -->"
    if owner_marker not in rendered_with_empty_user:
        raise ObsidianError("内部错误：生成内容缺少归属标记")
    rendered_bytes = rendered_with_empty_user.encode("utf-8")
    if len(rendered_bytes) > _MAX_NOTE_BYTES:
        raise ObsidianConflictError("生成的笔记异常过大")

    if path.is_symlink():
        raise ObsidianConflictError("目标是符号链接")
    if not path.exists():
        generated_hash = _sha256(_managed_projection(rendered_with_empty_user).encode("utf-8"))
        _atomic_write(path, rendered_bytes)
        return "created", generated_hash
    if not path.is_file():
        raise ObsidianConflictError("目标不是普通文件")
    try:
        if path.stat().st_size > _MAX_NOTE_BYTES:
            raise ObsidianConflictError("目标笔记异常过大")
        current = path.read_text(encoding="utf-8")
    except ObsidianConflictError:
        raise
    except (OSError, UnicodeError) as exc:
        raise ObsidianConflictError("无法安全读取目标笔记") from exc
    if owner_marker not in current:
        raise ObsidianConflictError("目标笔记的归属标记无效")
    try:
        user_content = _extract_user_content(current)
        current_generated_hash = _sha256(_managed_projection(current).encode("utf-8"))
    except ValueError as exc:
        raise ObsidianConflictError("用户编辑区标记无效") from exc
    if manifest_entry is None:
        # 上次运行可能在原子写完笔记、尚未写入 manifest 时异常结束。只有当
        # 所有权标记和受管投影都与本轮确定性输出完全一致时才安全接管。
        if current_generated_hash != source_hash:
            raise ObsidianConflictError("目标已存在但不在同步清单中")
        return "unchanged", current_generated_hash
    if current_generated_hash != manifest_entry["generated_hash"]:
        if current_generated_hash == source_hash:
            # 笔记更新成功、manifest 原子替换前异常退出的可验证恢复路径。
            return "unchanged", current_generated_hash
        raise ObsidianConflictError("检测到受管区域被修改")
    if source_hash == manifest_entry["source_hash"]:
        return "unchanged", current_generated_hash

    updated = _replace_user_content(rendered_with_empty_user, user_content)
    updated_bytes = updated.encode("utf-8")
    if len(updated_bytes) > _MAX_NOTE_BYTES:
        raise ObsidianConflictError("保留用户编辑区后笔记异常过大")
    generated_hash = _sha256(_managed_projection(updated).encode("utf-8"))
    _atomic_write(path, updated_bytes)
    return "updated", generated_hash


def _extract_user_content(value: str) -> str:
    if value.count(_USER_START) != 1 or value.count(_USER_END) != 1:
        raise ValueError("用户区标记数量错误")
    start = value.index(_USER_START) + len(_USER_START)
    end = value.index(_USER_END)
    if end < start:
        raise ValueError("用户区标记顺序错误")
    return value[start:end]


def _replace_user_content(value: str, user_content: str) -> str:
    _extract_user_content(value)
    start = value.index(_USER_START) + len(_USER_START)
    end = value.index(_USER_END)
    return value[:start] + user_content + value[end:]


def _managed_projection(value: str) -> str:
    return _replace_user_content(value, _EMPTY_USER_CONTENT)


def _render_card_note(card: Card, attachment_name: str | None, user_content: str) -> str:
    lines = [
        "---",
        "capture_assistant_managed: true",
        f"capture_assistant_schema: {MIRROR_SCHEMA}",
        f"card_id: {_yaml_string(card.id)}",
        f"created_at: {_yaml_string(card.created_at)}",
        f"stance: {_yaml_string(card.stance)}",
        f"text_source: {_yaml_string(card.text_source)}",
        f"confidence: {card.confidence:.6f}",
        "tags:",
        "  - capture-assistant",
        f"  - capture-assistant/stance/{card.stance}",
    ]
    if card.video_time is not None:
        lines.append(f"video_time: {card.video_time:.6f}")
    lines.extend(
        [
            "---",
            f"<!-- capture-assistant:owner:card:{card.id} -->",
            "",
            f"# 观点卡片 {card.id}",
            "",
            f"- 态度：{_STANCE_LABELS[card.stance]}",
            f"- 创建时间：`{card.created_at}`",
            "",
            "## 提取原文（不可信资料）",
            "",
            _fenced_data(card.text),
            "",
        ]
    )
    if card.edited_text is not None:
        lines.extend(["## 人工整理文字（不可信资料）", "", _fenced_data(card.edited_text), ""])
    if card.source_title:
        lines.extend(["## 来源标题（不可信资料）", "", _fenced_data(card.source_title), ""])
    if card.source_url:
        lines.extend(["## 来源网址", ""])
        safe_url = _safe_source_url(card.source_url)
        if safe_url is None:
            lines.extend([_fenced_data(card.source_url), ""])
        else:
            lines.extend([f"[打开已去除查询参数的来源](<{safe_url}>)", ""])
    if card.note:
        lines.extend(["## 用户备注（不可信资料）", "", _fenced_data(card.note), ""])
    if attachment_name is not None:
        lines.extend(["## 选区截图", "", f"![[{ATTACHMENTS_FOLDER}/{attachment_name}]]", ""])
    lines.extend(["## 我的 Obsidian 补充", "", _USER_START + user_content + _USER_END, ""])
    return "\n".join(lines).rstrip() + "\n"


def _render_index(cards: list[Card], user_content: str) -> str:
    lines = [
        "---",
        "capture_assistant_managed: true",
        f"capture_assistant_schema: {MIRROR_SCHEMA}",
        "document_type: index",
        "tags:",
        "  - capture-assistant",
        "  - capture-assistant/index",
        "---",
        "<!-- capture-assistant:owner:index -->",
        "",
        "# Capture Assistant 卡片索引",
        "",
        "> 此索引由本地采集助手管理；数据库仍是事实来源。旧卡片不会由本同步器自动删除。",
        "",
    ]
    grouped = {stance: [] for stance in _STANCE_LABELS}
    for card in cards:
        grouped[card.stance].append(card)
    for stance, label in _STANCE_LABELS.items():
        lines.extend([f"## {label}", ""])
        if not grouped[stance]:
            lines.extend(["_暂无_", ""])
            continue
        for card in grouped[stance]:
            lines.append(
                f"- [[{CARDS_FOLDER}/{card.id}|{card.id}]] · `{card.created_at}`"
            )
        lines.append("")
    lines.extend(["## 我的 Obsidian 补充", "", _USER_START + user_content + _USER_END, ""])
    return "\n".join(lines).rstrip() + "\n"


def _fenced_data(value: str) -> str:
    longest = max((len(match.group(0)) for match in _BACKTICK_RUN.finditer(value)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{value}\n{fence}"


def _safe_source_url(value: str) -> str | None:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    sanitized = urlunsplit((parsed.scheme.casefold(), parsed.netloc, parsed.path, "", ""))
    return quote(sanitized, safe="/:[]@!$&'()*+,;=%~.-_")


def _yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _read_selection_png(data_dir: Path, screenshot_path: str) -> bytes:
    try:
        data_root = data_dir.resolve(strict=True)
    except OSError as exc:
        raise ObsidianConflictError("本地数据目录不存在") from exc
    screenshots_root = data_root / "screenshots"
    if screenshots_root.is_symlink() or not screenshots_root.is_dir():
        raise ObsidianConflictError("截图目录不存在或不是普通目录")
    candidate = data_root / Path(screenshot_path)
    if _path_has_symlink(candidate):
        raise ObsidianConflictError("截图路径经过符号链接")
    try:
        source = candidate.resolve(strict=True)
        _assert_within(source, screenshots_root.resolve(strict=True), "截图不在数据目录的 screenshots 内")
    except ObsidianVaultError as exc:
        raise ObsidianConflictError(str(exc)) from exc
    except (OSError, RuntimeError) as exc:
        raise ObsidianConflictError("选区截图不存在或无法访问") from exc
    if source.suffix.casefold() != ".png" or not source.is_file():
        raise ObsidianConflictError("选区截图不是普通 PNG 文件")
    try:
        size = source.stat().st_size
        if size < len(_PNG_SIGNATURE) or size > _MAX_SCREENSHOT_BYTES:
            raise ObsidianConflictError("选区截图大小无效")
        data = source.read_bytes()
    except ObsidianConflictError:
        raise
    except OSError as exc:
        raise ObsidianConflictError("无法读取选区截图") from exc
    if not data.startswith(_PNG_SIGNATURE):
        raise ObsidianConflictError("选区截图的 PNG 签名无效")
    return data


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    data = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _atomic_write(path, data)


def _atomic_write(path: Path, data: bytes) -> None:
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir():
        raise ObsidianError("目标目录不是安全的普通目录")
    if path.is_symlink():
        raise ObsidianError("拒绝覆盖符号链接")
    temp_path: Path | None = None
    try:
        file_descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=parent
        )
        temp_path = Path(temp_name)
        with os.fdopen(file_descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        temp_path = None
    except OSError as exc:
        raise ObsidianError("无法原子写入 Obsidian 本地文件") from exc
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
