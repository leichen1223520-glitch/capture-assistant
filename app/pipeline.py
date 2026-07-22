"""把一次主动框选转换为可追溯的本地观点卡片。

流水线只处理用户已经确认的冻结画面和选区：优先采用浏览器 DOM 选中文字，
否则仅对选区执行离线 OCR。截图与数据库记录作为一个逻辑提交单元处理；任何持久化
步骤失败时都会清理本次新建截图，数据库原子性则由 ``Store.add_card`` 的事务保证。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path, PureWindowsPath
from threading import Lock
from typing import TypeAlias
from uuid import uuid4

from PIL import Image
from PySide6.QtCore import QRect

from app.bridge import BrowserContext, get_browser_context
from app.capture import CaptureMeta, save_image
from app.config import DATA_DIR, DB_PATH, SCREENSHOT_DIR
from app.models import Card
from app.ocr import OCRBox, ocr_image
from app.overlay import OverlayError, crop_selection
from app.store import Store, StoreError

ContextProvider: TypeAlias = Callable[[], BrowserContext | None]
OCRProvider: TypeAlias = Callable[[Image.Image], tuple[str, float, list[OCRBox]]]


class PipelineError(RuntimeError):
    """表示单次卡片生成已安全中止，调用方不应把它当作成功保存。"""


_SUPPORTED_CHROMIUM_APPS = frozenset(
    {
        "brave",
        "brave-browser",
        "chrome",
        "chromium",
        "google-chrome",
        "msedge",
        "opera",
        "opera_gx",
        "vivaldi",
    }
)
_DEFAULT_STORES: dict[Path, Store] = {}
_DEFAULT_STORES_LOCK = Lock()


def _is_supported_chromium_app(app_name: str | None) -> bool:
    """判断捕获时的前台进程是否属于当前支持的 Chromium 浏览器。"""

    if not app_name:
        return False
    normalized = PureWindowsPath(app_name.strip()).name.casefold()
    if normalized.endswith(".exe"):
        normalized = normalized[:-4]
    return normalized in _SUPPORTED_CHROMIUM_APPS


def _resolve_storage_paths(
    data_dir: str | Path,
    screenshot_dir: str | Path,
) -> tuple[Path, Path, Path]:
    """规范化数据目录，并拒绝数据目录之外或等同于数据目录的截图目录。"""

    data_root = Path(data_dir).expanduser().resolve()
    screenshot_root = Path(screenshot_dir).expanduser().resolve()
    try:
        screenshot_relative = screenshot_root.relative_to(data_root)
    except ValueError as exc:
        raise PipelineError("截图目录必须位于本地数据目录之内。") from exc
    if screenshot_relative == Path("."):
        raise PipelineError("截图目录不能直接等同于本地数据目录。")
    return data_root, screenshot_root, screenshot_relative


def _browser_context_or_none(context_provider: ContextProvider) -> BrowserContext | None:
    """读取可选浏览器上下文；桥接故障不得阻断后续离线 OCR。"""

    try:
        return context_provider()
    except (OSError, RuntimeError):
        # 浏览器桥只是可选元数据来源。该边界刻意隔离提供方故障，同时不记录
        # URL、选中文字或其他可能敏感的上下文内容。
        return None


def _remove_new_files(paths: set[Path]) -> list[OSError]:
    """尽力删除本次流水线新建的文件，并返回无法清理的错误。"""

    errors: list[OSError] = []
    for path in paths:
        try:
            if path.exists() and path.is_dir():
                errors.append(OSError(f"回滚目标意外成为目录：{path}"))
                continue
            path.unlink(missing_ok=True)
        except OSError as exc:
            errors.append(exc)
    return errors


def _default_store(data_root: Path) -> Store:
    """惰性创建默认仓库，并在数据库被清空时安全地重新初始化。"""

    with _DEFAULT_STORES_LOCK:
        existing = _DEFAULT_STORES.get(data_root)
        if existing is not None:
            try:
                if existing.db_path.is_file() and existing.db_path.stat().st_size > 0:
                    return existing
                existing.init_db()
                return existing
            except (OSError, StoreError):
                _DEFAULT_STORES.pop(data_root, None)
                raise
        db_name = Path(DB_PATH).name
        created = Store(db_path=data_root / db_name, data_dir=data_root)
        try:
            created.init_db()
        except (OSError, StoreError):
            _DEFAULT_STORES.pop(data_root, None)
            raise
        else:
            _DEFAULT_STORES[data_root] = created
            return created


def _invalidate_default_store(data_root: Path, store: Store) -> None:
    """仅在对象仍匹配时让失败的默认仓库缓存失效。"""

    with _DEFAULT_STORES_LOCK:
        if _DEFAULT_STORES.get(data_root) is store:
            _DEFAULT_STORES.pop(data_root, None)


def _validate_store_paths(store: Store, data_root: Path) -> None:
    """确保仓库数据库和截图共享同一个受控数据根目录。"""

    if store.data_dir != data_root:
        raise PipelineError("注入仓库的数据目录与流水线数据目录不一致。")
    try:
        database_path = store.db_path.resolve()
        database_path.relative_to(data_root)
    except (OSError, ValueError) as exc:
        raise PipelineError("卡片数据库必须位于本地数据目录之内。") from exc
    if database_path == data_root or database_path.is_dir():
        raise PipelineError("卡片数据库路径必须指向数据目录内的文件。")


def build_card_from_selection(
    frozen_img: Image.Image,
    meta: CaptureMeta,
    rect: QRect | None,
    *,
    store: Store | None = None,
    context_provider: ContextProvider = get_browser_context,
    ocr_provider: OCRProvider = ocr_image,
    data_dir: str | Path = DATA_DIR,
    screenshot_dir: str | Path = SCREENSHOT_DIR,
) -> Card:
    """由冻结画面和用户选区生成、保存并返回一张观点卡片。

    ``rect`` 为 ``None``、空选区、空 OCR 结果或任何持久化失败都会中止本次操作。
    浏览器桥不可用时会自动降级到选区 OCR。注入的 ``store`` 应已经初始化，且其
    ``data_dir`` 必须与本函数的 ``data_dir`` 一致；未注入时会线程安全地惰性初始化
    该数据目录的默认仓库。模块导入本身不会创建目录或数据库。

    自定义 ``Store`` 必须遵守 ``add_card`` 的事务契约：抛出异常即表示记录未提交。
    “先提交再抛异常”的自定义实现违反该契约；流水线不会按 UUID 猜测记录归属并删除
    数据库内容，只会清理本次新建的截图文件。
    """

    if not isinstance(frozen_img, Image.Image):
        raise PipelineError("冻结画面必须是 PIL 图像。")
    if frozen_img.width <= 0 or frozen_img.height <= 0:
        raise PipelineError("冻结画面尺寸必须大于零。")
    if not isinstance(meta, CaptureMeta):
        raise PipelineError("捕获元数据必须是 CaptureMeta。")
    if frozen_img.size != (meta.width, meta.height):
        raise PipelineError("冻结画面尺寸与捕获元数据不一致。")
    if rect is None:
        raise PipelineError("用户已取消框选，未生成卡片。")
    if not isinstance(rect, QRect):
        raise PipelineError("选区必须是图像像素坐标 QRect。")

    data_root, screenshot_root, screenshot_relative = _resolve_storage_paths(
        data_dir,
        screenshot_dir,
    )
    if store is not None:
        _validate_store_paths(store, data_root)

    try:
        cropped = crop_selection(frozen_img, rect)
    except OverlayError as exc:
        raise PipelineError("选区为空或超出冻结画面，未生成卡片。") from exc

    context = (
        _browser_context_or_none(context_provider)
        if _is_supported_chromium_app(meta.app_name)
        else None
    )
    selected_text = ""
    if context is not None and isinstance(context.selection, str):
        selected_text = context.selection.strip()

    if selected_text:
        text = selected_text
        text_source = "dom"
        confidence = 0.99
    else:
        text, confidence, _boxes = ocr_provider(cropped)
        text = text.strip()
        if not text:
            raise PipelineError("选区内没有可保存的文字，未生成卡片。")
        text_source = "ocr"

    card_id = str(uuid4())
    selected_name = f"{card_id}.png"
    full_name = f"full_{card_id}.png"
    selected_path = screenshot_root / selected_name
    full_path = screenshot_root / full_name
    if selected_path.exists() or full_path.exists():
        raise PipelineError("新卡片截图路径已存在，已拒绝覆盖本地文件。")

    relative_prefix = screenshot_relative.as_posix()
    card = Card(
        id=card_id,
        text=text,
        text_source=text_source,
        confidence=confidence,
        screenshot_path=f"{relative_prefix}/{selected_name}",
        full_screenshot_path=f"{relative_prefix}/{full_name}",
        source_url=context.url if context is not None else None,
        source_title=context.title if context is not None else None,
        video_time=context.video_time if context is not None else None,
        app_name=meta.app_name,
        monitor=meta.card_monitor(),
        created_at=meta.captured_at,
        stance="unknown",
    )

    target_store = store
    uses_default_store = target_store is None
    if target_store is None:
        try:
            target_store = _default_store(data_root)
        except (OSError, StoreError) as exc:
            raise PipelineError("无法初始化本地卡片数据库。") from exc
    _validate_store_paths(target_store, data_root)

    try:
        existing_card = target_store.get_card(card.id)
    except StoreError as exc:
        if uses_default_store:
            _invalidate_default_store(data_root, target_store)
        raise PipelineError("无法检查本地卡片 ID 是否已存在。") from exc
    if existing_card is not None:
        raise PipelineError("新卡片 ID 已存在，已拒绝覆盖本地记录。")

    rollback_paths = {selected_path, full_path}
    try:
        save_image(cropped, selected_path)
        save_image(frozen_img, full_path)
        return target_store.add_card(card)
    except Exception as exc:
        if uses_default_store and isinstance(exc, StoreError):
            _invalidate_default_store(data_root, target_store)
        file_errors = _remove_new_files(rollback_paths)
        if file_errors:
            raise PipelineError(
                "卡片保存失败，并且本地回滚未能完整完成；请检查数据目录。"
            ) from exc
        raise PipelineError("卡片保存失败，本次新建数据已回滚。") from exc
