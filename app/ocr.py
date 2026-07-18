"""RapidOCR 离线识别、结果规范化与阅读顺序合并。

RapidOCR 只在第一次实际识别时导入和初始化。模块导入不会加载模型、访问网络
或处理任何图像。OCR 原始文字不会被词典或生成模型静默改写。
"""

from __future__ import annotations

import math
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, TypedDict, cast

import numpy as np
from PIL import Image


class OCRError(RuntimeError):
    """表示 OCR 引擎不可用、执行失败或返回格式不合法。"""


class OCRBox(TypedDict):
    """一块可回溯的 OCR 原始文字、四边形坐标与置信度。"""

    box: list[list[float]]
    text: str
    confidence: float


class OCREngine(Protocol):
    """RapidOCR 及测试替身共同遵循的最小调用协议。"""

    def __call__(self, image: np.ndarray) -> object: ...


_ENGINE: OCREngine | None = None
_ENGINE_LOCK = threading.Lock()
_INFERENCE_LOCK = threading.Lock()


def _get_engine() -> OCREngine:
    """返回进程内 RapidOCR 单例；初始化失败时提供可理解错误。"""

    global _ENGINE
    if _ENGINE is not None:
        return _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is not None:
            return _ENGINE
        try:
            from rapidocr_onnxruntime import RapidOCR

            _ENGINE = cast(OCREngine, RapidOCR())
        except (ImportError, OSError, RuntimeError, ValueError) as exc:
            raise OCRError("无法初始化本地 RapidOCR 引擎。") from exc
    return _ENGINE


def _as_list(value: object) -> list[Any]:
    """将 NumPy 或普通序列转换为列表，同时拒绝字符串伪序列。"""

    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        value = tolist()
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    raise OCRError("OCR 返回了无法解析的序列。")


def _normalize_points(value: object) -> list[list[float]]:
    points = _as_list(value)
    if len(points) < 4:
        raise OCRError("OCR 文字框至少需要四个坐标点。")

    normalized: list[list[float]] = []
    for point in points:
        coordinates = _as_list(point)
        if len(coordinates) != 2:
            raise OCRError("OCR 文字框坐标点必须包含 x 和 y。")
        x = float(coordinates[0])
        y = float(coordinates[1])
        if not math.isfinite(x) or not math.isfinite(y):
            raise OCRError("OCR 文字框包含非有限坐标。")
        normalized.append([x, y])
    return normalized


def _normalize_confidence(value: object) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError) as exc:
        raise OCRError("OCR 置信度不是有效数字。") from exc
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        raise OCRError("OCR 置信度必须位于 0 到 1。")
    return confidence


def _normalize_entry(entry: object) -> OCRBox:
    if isinstance(entry, Mapping):
        box_value = entry.get("box", entry.get("boxes"))
        text_value = entry.get("text", entry.get("txt"))
        score_value = entry.get("confidence", entry.get("score"))
    else:
        values = _as_list(entry)
        if len(values) < 3:
            raise OCRError("OCR 结果项必须包含文字框、文字和置信度。")
        box_value, text_value, score_value = values[:3]

    if box_value is None or text_value is None or score_value is None:
        raise OCRError("OCR 结果项缺少文字框、文字或置信度。")
    text = str(text_value)
    return OCRBox(
        box=_normalize_points(box_value),
        text=text,
        confidence=_normalize_confidence(score_value),
    )


def _entries_from_parallel_output(output: object) -> list[OCRBox] | None:
    """兼容新版 RapidOCR 的 boxes/txts/scores 平行数组输出。"""

    if isinstance(output, Mapping):
        has_parallel_fields = any(
            key in output for key in ("boxes", "txts", "texts", "scores")
        )
        boxes = output.get("boxes")
        texts = output.get("txts", output.get("texts"))
        scores = output.get("scores")
    else:
        has_parallel_fields = any(
            hasattr(output, name) for name in ("boxes", "txts", "texts", "scores")
        )
        boxes = getattr(output, "boxes", None)
        texts = getattr(output, "txts", getattr(output, "texts", None))
        scores = getattr(output, "scores", None)
    if not has_parallel_fields:
        return None
    if boxes is None and texts is None and scores is None:
        return []
    if boxes is None or texts is None or scores is None:
        raise OCRError("OCR 平行数组输出不完整。")

    box_values = _as_list(boxes)
    text_values = _as_list(texts)
    score_values = _as_list(scores)
    if not len(box_values) == len(text_values) == len(score_values):
        raise OCRError("OCR 平行数组长度不一致。")
    return [
        _normalize_entry((box, text, score))
        for box, text, score in zip(box_values, text_values, score_values, strict=True)
    ]


def normalize_ocr_output(output: object) -> list[OCRBox]:
    """把不同 RapidOCR 版本的输出转换为统一、可序列化的文字框。"""

    if output is None:
        return []

    parallel = _entries_from_parallel_output(output)
    if parallel is not None:
        return parallel

    # rapidocr-onnxruntime 1.x 返回 ``(results, elapsed)``。
    if isinstance(output, tuple) and len(output) == 2:
        output = output[0]
        if output is None:
            return []

    values = _as_list(output)
    if not values:
        return []
    return [_normalize_entry(entry) for entry in values]


def _bounds(box: OCRBox) -> tuple[float, float, float, float]:
    xs = [point[0] for point in box["box"]]
    ys = [point[1] for point in box["box"]]
    return min(xs), min(ys), max(xs), max(ys)


@dataclass(slots=True)
class _TextLine:
    boxes: list[OCRBox] = field(default_factory=list)
    top: float = 0.0
    bottom: float = 0.0

    @classmethod
    def from_box(cls, box: OCRBox) -> _TextLine:
        _, top, _, bottom = _bounds(box)
        return cls(boxes=[box], top=top, bottom=bottom)

    @property
    def center(self) -> float:
        return (self.top + self.bottom) / 2.0

    @property
    def height(self) -> float:
        return max(1.0, self.bottom - self.top)

    def accepts(self, box: OCRBox) -> bool:
        _, top, _, bottom = _bounds(box)
        height = max(1.0, bottom - top)
        overlap = max(0.0, min(self.bottom, bottom) - max(self.top, top))
        overlap_ratio = overlap / min(self.height, height)
        center_distance = abs(self.center - (top + bottom) / 2.0)
        return overlap_ratio >= 0.3 or center_distance <= max(4.0, min(self.height, height) * 0.6)

    def add(self, box: OCRBox) -> None:
        _, top, _, bottom = _bounds(box)
        self.boxes.append(box)
        self.top = min(self.top, top)
        self.bottom = max(self.bottom, bottom)


_NO_SPACE_BEFORE = frozenset("，。！？；：、,.!?;:)]}》】〉％%")
_NO_SPACE_AFTER = frozenset("([{《【〈，。！？；：、")


def _is_cjk(character: str) -> bool:
    codepoint = ord(character)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
    )


def _join_tokens(tokens: Sequence[str]) -> str:
    merged = ""
    for token in tokens:
        if not token:
            continue
        if not merged:
            merged = token
            continue
        previous = merged[-1]
        current = token[0]
        if (
            current in _NO_SPACE_BEFORE
            or previous in _NO_SPACE_AFTER
            or (_is_cjk(previous) and _is_cjk(current))
        ):
            merged += token
        else:
            merged += " " + token
    return merged


def merge_ocr_boxes(boxes: Sequence[OCRBox]) -> tuple[str, float, list[OCRBox]]:
    """按阅读顺序合并文字框，并返回长度加权总体置信度。"""

    visible_boxes = [box for box in boxes if box["text"].strip()]
    if not visible_boxes:
        return "", 0.0, []

    lines: list[_TextLine] = []
    for box in sorted(visible_boxes, key=lambda item: (_bounds(item)[1], _bounds(item)[0])):
        candidates = [line for line in lines if line.accepts(box)]
        if candidates:
            _, top, _, bottom = _bounds(box)
            target_center = (top + bottom) / 2.0
            min(
                candidates,
                key=lambda line: abs(line.center - target_center),
            ).add(box)
        else:
            lines.append(_TextLine.from_box(box))

    lines.sort(key=lambda line: (line.top, line.center))
    ordered: list[OCRBox] = []
    text_lines: list[str] = []
    for line in lines:
        line.boxes.sort(key=lambda item: (_bounds(item)[0], _bounds(item)[1]))
        ordered.extend(line.boxes)
        text_lines.append(_join_tokens([box["text"].strip() for box in line.boxes]))

    weighted_score = 0.0
    total_weight = 0
    for box in ordered:
        weight = max(1, sum(not character.isspace() for character in box["text"]))
        weighted_score += box["confidence"] * weight
        total_weight += weight
    confidence = weighted_score / total_weight if total_weight else 0.0
    return "\n".join(text_lines), confidence, ordered


def ocr_image(
    image: Image.Image,
    *,
    engine: OCREngine | Callable[[np.ndarray], object] | None = None,
) -> tuple[str, float, list[OCRBox]]:
    """离线识别 PIL 图像并返回文字、总体置信度和原始文字框。

    ``engine`` 仅用于自动测试或显式替换本地引擎。任何执行异常都会转换为
    不包含图像正文的 :class:`OCRError`。
    """

    if not isinstance(image, Image.Image):
        raise TypeError("ocr_image 只接受 PIL.Image.Image。")
    if image.width <= 0 or image.height <= 0:
        raise OCRError("OCR 图像尺寸必须大于零。")

    pixels = np.asarray(image.convert("RGB"))
    try:
        if engine is None:
            # RapidOCR 单例的内部预处理对象可能保存临时状态，串行调用更稳妥。
            with _INFERENCE_LOCK:
                raw_output = _get_engine()(pixels)
        else:
            raw_output = engine(pixels)
        boxes = normalize_ocr_output(raw_output)
    except OCRError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise OCRError("本地 OCR 执行失败。") from exc
    return merge_ocr_boxes(boxes)
