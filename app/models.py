"""项目核心数据模型。

当前阶段只冻结观点卡片（Card）的最小字段集合。后续模块必须通过这些
字段保存原文、来源和用户明确设置的态度，不能用模型生成内容覆盖原始证据。
"""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

TextSource = Literal["dom", "ocr"]
Stance = Literal["unknown", "agree", "disagree", "doubt", "useful"]


def _new_card_id() -> str:
    """生成可序列化的 UUID4 字符串。"""

    return str(uuid4())


def _local_timestamp() -> str:
    """返回包含本机时区偏移的 ISO 8601 时间字符串。"""

    return datetime.now().astimezone().isoformat()


class Card(BaseModel):
    """一条可回溯到屏幕证据的观点卡片。"""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        validate_default=True,
    )

    id: str = Field(default_factory=_new_card_id)
    text: str = Field(frozen=True)
    edited_text: str | None = None
    text_source: TextSource = Field(frozen=True)
    confidence: float = Field(ge=0.0, le=1.0, frozen=True)
    screenshot_path: str
    full_screenshot_path: str
    source_url: str | None = None
    source_title: str | None = None
    video_time: float | None = Field(default=None, ge=0.0)
    app_name: str | None = None
    monitor: dict[str, Any] | None = None
    created_at: str = Field(default_factory=_local_timestamp)
    stance: Stance = "unknown"
    note: str = ""

    @field_validator("id")
    @classmethod
    def id_must_be_uuid4(cls, value: str) -> str:
        """校验并规范化 UUID4，避免把任意字符串用作文件或记录标识。"""

        try:
            parsed = UUID(value)
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("id 必须是合法的 UUID4 字符串") from exc
        if parsed.version != 4:
            raise ValueError("id 必须是 UUID4")
        return str(parsed)

    @field_validator("screenshot_path", "full_screenshot_path")
    @classmethod
    def screenshot_paths_must_be_relative(cls, value: str) -> str:
        """只接受数据目录内的相对路径，阻止路径穿越和跨盘访问。"""

        if not value.strip() or "\x00" in value:
            raise ValueError("截图路径不能为空")

        normalized = value.replace("\\", "/")
        posix_path = PurePosixPath(normalized)
        windows_path = PureWindowsPath(value)
        if (
            posix_path.is_absolute()
            or windows_path.is_absolute()
            or bool(windows_path.drive)
            or ".." in posix_path.parts
            or ".." in windows_path.parts
        ):
            raise ValueError("截图路径必须是数据目录内且不含 '..' 的相对路径")
        if posix_path.as_posix() == ".":
            raise ValueError("截图路径必须指向文件")
        return posix_path.as_posix()

    @field_validator("monitor")
    @classmethod
    def monitor_must_match_capture_contract(
        cls,
        value: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """限定显示器元数据为正数的 width、height 与 scale。"""

        if value is None:
            return None

        required_keys = {"width", "height", "scale"}
        if set(value) != required_keys:
            raise ValueError("monitor 必须且只能包含 width、height、scale")

        width = value["width"]
        height = value["height"]
        scale = value["scale"]
        if (
            isinstance(width, bool)
            or not isinstance(width, int)
            or width <= 0
            or isinstance(height, bool)
            or not isinstance(height, int)
            or height <= 0
        ):
            raise ValueError("monitor 的 width 和 height 必须是正整数")
        if (
            isinstance(scale, bool)
            or not isinstance(scale, (int, float))
            or not isfinite(float(scale))
            or scale <= 0
        ):
            raise ValueError("monitor 的 scale 必须是有限正数")

        return {"width": width, "height": height, "scale": float(scale)}

    @field_validator("created_at")
    @classmethod
    def created_at_must_include_timezone(cls, value: str) -> str:
        """拒绝没有时区信息的时间，避免跨设备排序产生歧义。"""

        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("created_at 必须是合法的 ISO 8601 时间") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("created_at 必须包含时区")
        return value
