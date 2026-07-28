"""半自动观察模式使用的纯内存候选收件箱。

本模块不访问数据库、截图目录或网络。:class:`CandidateInbox` 在 ``offer`` 成功
调用后接管 :class:`app.pipeline.PreparedCard` 的所有权；候选只有经 ``take``
明确转交给审核流程后，才可能由现有审核窗口保存到磁盘。
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from difflib import SequenceMatcher
from math import isfinite
from threading import RLock
from time import monotonic
from typing import Final, Iterable, Literal
from unicodedata import normalize
from uuid import uuid4

from .pipeline import PreparedCard

MAX_CANDIDATES: Final = 20
MAX_MEMORY_BYTES: Final = 192 * 1024 * 1024
EXACT_DEDUPE_WINDOW_SECONDS: Final = 12.0
APPROX_DEDUPE_WINDOW_SECONDS: Final = 4.0
APPROX_MIN_TEXT_LENGTH: Final = 8
APPROX_SIMILARITY_THRESHOLD: Final = 0.92
_MAX_APPROX_TEXT_LENGTH: Final = 512

OfferStatus = Literal["added", "merged", "closed", "too_large"]


def normalize_candidate_text(text: str) -> str:
    """按 NFKC、空白折叠和大小写折叠生成内存去重文本。"""

    if not isinstance(text, str):
        raise TypeError("候选文字必须是字符串")
    return " ".join(normalize("NFKC", text).split()).casefold()


def estimate_candidate_bytes(candidate: PreparedCard) -> int:
    """保守估算候选持有的图像、预览和主要文字所占内存。

    屏幕捕获产生 RGB/RGBA 图像；按每像素至少四字节估算可避免低估这两类图像。
    该数值用于有界缓冲，不代表 Python 进程的精确驻留内存。
    """

    if not isinstance(candidate, PreparedCard):
        raise TypeError("candidate 必须是 PreparedCard")
    if candidate.is_closed:
        raise ValueError("候选图像已经释放")

    image_bytes = 0
    for image in (candidate.selected_image, candidate.full_image):
        width, height = image.size
        if width <= 0 or height <= 0:
            raise ValueError("候选图像尺寸必须大于零")
        image_bytes += width * height * max(4, len(image.getbands()))

    card = candidate.card
    text_values = (
        card.text,
        card.edited_text or "",
        card.source_url or "",
        card.source_title or "",
        card.app_name or "",
        card.note,
    )
    text_bytes = sum(len(value.encode("utf-8")) for value in text_values)
    return (
        image_bytes
        + len(candidate.selected_preview_png)
        + len(candidate.full_preview_png)
        + text_bytes
    )


@dataclass(frozen=True, slots=True)
class CandidateSummary:
    """不会暴露可变图像所有权的候选列表摘要。"""

    entry_id: str
    card_id: str
    text: str
    text_source: str
    confidence: float
    source_url: str | None
    source_title: str | None
    video_time: float | None
    created_at: str
    session_id: str
    source_key: str
    region_key: str
    first_seen: float
    last_seen: float
    occurrences: int
    estimated_bytes: int
    selected_preview_png: bytes
    full_preview_png: bytes


@dataclass(frozen=True, slots=True)
class OfferResult:
    """一次 ``offer`` 的结果以及为满足上限而淘汰的候选。"""

    status: OfferStatus
    entry_id: str | None
    occurrences: int
    evicted_entry_ids: tuple[str, ...] = ()

    @property
    def accepted(self) -> bool:
        """候选是否已新增或与现有条目合并。"""

        return self.status in {"added", "merged"}


@dataclass(slots=True)
class _Entry:
    entry_id: str
    prepared: PreparedCard
    session_id: str
    source_key: str
    region_key: str
    normalized_text: str
    first_seen: float
    last_seen: float
    occurrences: int
    estimated_bytes: int

    def summary(self) -> CandidateSummary:
        card = self.prepared.card
        return CandidateSummary(
            entry_id=self.entry_id,
            card_id=card.id,
            text=card.text,
            text_source=card.text_source,
            confidence=card.confidence,
            source_url=card.source_url,
            source_title=card.source_title,
            video_time=card.video_time,
            created_at=card.created_at,
            session_id=self.session_id,
            source_key=self.source_key,
            region_key=self.region_key,
            first_seen=self.first_seen,
            last_seen=self.last_seen,
            occurrences=self.occurrences,
            estimated_bytes=self.estimated_bytes,
            selected_preview_png=self.prepared.selected_preview_png,
            full_preview_png=self.prepared.full_preview_png,
        )


class CandidateInbox:
    """线程安全、受数量和内存双重约束的 ``PreparedCard`` 所有权容器."""

    def __init__(
        self,
        *,
        max_candidates: int = MAX_CANDIDATES,
        max_memory_bytes: int = MAX_MEMORY_BYTES,
    ) -> None:
        if (
            not isinstance(max_candidates, int)
            or isinstance(max_candidates, bool)
            or not 1 <= max_candidates <= MAX_CANDIDATES
        ):
            raise ValueError(f"max_candidates 必须是 1 到 {MAX_CANDIDATES} 的整数")
        if (
            not isinstance(max_memory_bytes, int)
            or isinstance(max_memory_bytes, bool)
            or not 1 <= max_memory_bytes <= MAX_MEMORY_BYTES
        ):
            raise ValueError(
                f"max_memory_bytes 必须是 1 到 {MAX_MEMORY_BYTES} 的整数"
            )

        self.max_candidates = max_candidates
        self.max_memory_bytes = max_memory_bytes
        self._lock = RLock()
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._owned_object_ids: set[int] = set()
        self._memory_bytes = 0
        self._closed = False

    @property
    def closed(self) -> bool:
        """收件箱是否已永久关闭。"""

        with self._lock:
            return self._closed

    @property
    def memory_bytes(self) -> int:
        """当前候选的保守内存估算总和。"""

        with self._lock:
            return self._memory_bytes

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def snapshot(self) -> tuple[CandidateSummary, ...]:
        """以最近活动优先顺序返回不可变摘要，不转移图像所有权。"""

        with self._lock:
            return tuple(entry.summary() for entry in reversed(self._entries.values()))

    def offer(
        self,
        candidate: PreparedCard,
        *,
        session_id: str,
        source_key: str,
        region_key: str,
        now: float | None = None,
    ) -> OfferResult:
        """接管一个候选，执行去重并强制数量和内存上限。

        除“同一对象已由本收件箱持有”这一调用方错误外，本方法一旦被调用便消费
        ``candidate``：若收件箱关闭、候选过大或参数无效，也会在返回或抛错前释放
        它。所有图像释放都发生在内部锁之外。
        """

        if not isinstance(candidate, PreparedCard):
            raise TypeError("candidate 必须是 PreparedCard")

        with self._lock:
            if id(candidate) in self._owned_object_ids:
                raise ValueError("同一个 PreparedCard 已由此收件箱持有")

        try:
            identity = (
                self._validate_identity("session_id", session_id),
                self._validate_identity("source_key", source_key),
                self._validate_identity("region_key", region_key),
            )
            observed_at = monotonic() if now is None else self._validate_time(now)
            normalized_text = normalize_candidate_text(candidate.card.text)
            if not normalized_text:
                raise ValueError("候选文字规范化后不能为空")
            estimated_bytes = estimate_candidate_bytes(candidate)
        except Exception:
            candidate.close()
            raise

        to_close: list[PreparedCard] = []
        evicted_ids: list[str] = []
        status: OfferStatus
        entry_id: str | None
        occurrences = 0

        with self._lock:
            # 参数校验与内存估算发生在锁外；在真正接管前必须再次检查，
            # 防止两个线程同时 offer 同一个 PreparedCard 的竞态。
            if id(candidate) in self._owned_object_ids:
                raise ValueError("同一个 PreparedCard 已由此收件箱持有")
            if self._closed:
                status = "closed"
                entry_id = None
                to_close.append(candidate)
            elif estimated_bytes > self.max_memory_bytes:
                status = "too_large"
                entry_id = None
                to_close.append(candidate)
            else:
                match = self._find_duplicate_locked(
                    session_id=identity[0],
                    source_key=identity[1],
                    region_key=identity[2],
                    normalized_text=normalized_text,
                    observed_at=observed_at,
                )
                if match is None:
                    entry_id = str(uuid4())
                    entry = _Entry(
                        entry_id=entry_id,
                        prepared=candidate,
                        session_id=identity[0],
                        source_key=identity[1],
                        region_key=identity[2],
                        normalized_text=normalized_text,
                        first_seen=observed_at,
                        last_seen=observed_at,
                        occurrences=1,
                        estimated_bytes=estimated_bytes,
                    )
                    self._entries[entry_id] = entry
                    self._owned_object_ids.add(id(candidate))
                    self._memory_bytes += estimated_bytes
                    status = "added"
                    occurrences = 1
                else:
                    entry_id = match.entry_id
                    match.last_seen = max(match.last_seen, observed_at)
                    match.occurrences += 1
                    occurrences = match.occurrences
                    if self._should_replace(
                        match,
                        candidate,
                        normalized_text=normalized_text,
                    ):
                        previous = match.prepared
                        self._owned_object_ids.remove(id(previous))
                        self._memory_bytes -= match.estimated_bytes
                        match.prepared = candidate
                        match.normalized_text = normalized_text
                        match.estimated_bytes = estimated_bytes
                        self._owned_object_ids.add(id(candidate))
                        self._memory_bytes += estimated_bytes
                        to_close.append(previous)
                    else:
                        to_close.append(candidate)
                    self._entries.move_to_end(entry_id)
                    status = "merged"

                protected_id = entry_id
                while (
                    len(self._entries) > self.max_candidates
                    or self._memory_bytes > self.max_memory_bytes
                ):
                    oldest_id = next(
                        (
                            candidate_id
                            for candidate_id in self._entries
                            if candidate_id != protected_id
                        ),
                        None,
                    )
                    if oldest_id is None:
                        break
                    evicted = self._remove_locked(oldest_id)
                    if evicted is not None:
                        evicted_ids.append(oldest_id)
                        to_close.append(evicted)

        self._close_candidates(to_close)
        return OfferResult(
            status=status,
            entry_id=entry_id,
            occurrences=occurrences,
            evicted_entry_ids=tuple(evicted_ids),
        )

    def take(self, entry_id: str) -> PreparedCard | None:
        """移除并把一个候选的所有权转交给调用方；未找到时返回 ``None``。"""

        self._validate_entry_id(entry_id)
        with self._lock:
            return self._remove_locked(entry_id)

    def discard(self, entry_id: str) -> bool:
        """从收件箱删除并在锁外释放一个候选。"""

        candidate = self.take(entry_id)
        if candidate is None:
            return False
        candidate.close()
        return True

    def discard_many(self, entry_ids: Iterable[str]) -> tuple[str, ...]:
        """原子移出多个候选并在锁外批量释放，返回实际删除的 ID。"""

        if isinstance(entry_ids, (str, bytes)):
            raise TypeError("entry_ids 必须是 ID 可迭代对象，不能是单个字符串")
        requested = tuple(dict.fromkeys(entry_ids))
        for entry_id in requested:
            self._validate_entry_id(entry_id)

        removed_ids: list[str] = []
        to_close: list[PreparedCard] = []
        with self._lock:
            for entry_id in requested:
                candidate = self._remove_locked(entry_id)
                if candidate is not None:
                    removed_ids.append(entry_id)
                    to_close.append(candidate)
        self._close_candidates(to_close)
        return tuple(removed_ids)

    def discard_all(self) -> int:
        """清空但不关闭收件箱，并在锁外释放所有候选。"""

        with self._lock:
            to_close = self._drain_locked()
        self._close_candidates(to_close)
        return len(to_close)

    def close(self) -> None:
        """幂等关闭收件箱，并在锁外释放仍由其拥有的全部候选。"""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            to_close = self._drain_locked()
        self._close_candidates(to_close)

    @staticmethod
    def _validate_identity(name: str, value: str) -> str:
        if not isinstance(value, str):
            raise TypeError(f"{name} 必须是字符串")
        normalized_value = value.strip()
        if not normalized_value or "\x00" in normalized_value:
            raise ValueError(f"{name} 不能为空或包含 NUL")
        return normalized_value

    @staticmethod
    def _validate_time(value: float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("now 必须是有限数字")
        result = float(value)
        if not isfinite(result):
            raise ValueError("now 必须是有限数字")
        return result

    @staticmethod
    def _validate_entry_id(entry_id: str) -> None:
        if not isinstance(entry_id, str):
            raise TypeError("entry_id 必须是字符串")
        if not entry_id or "\x00" in entry_id:
            raise ValueError("entry_id 不能为空或包含 NUL")

    def _find_duplicate_locked(
        self,
        *,
        session_id: str,
        source_key: str,
        region_key: str,
        normalized_text: str,
        observed_at: float,
    ) -> _Entry | None:
        for entry in reversed(self._entries.values()):
            if (
                entry.session_id != session_id
                or entry.source_key != source_key
                or entry.region_key != region_key
            ):
                continue
            elapsed = observed_at - entry.last_seen
            if elapsed < 0:
                continue
            if (
                elapsed <= EXACT_DEDUPE_WINDOW_SECONDS
                and entry.normalized_text == normalized_text
            ):
                return entry
            if elapsed > APPROX_DEDUPE_WINDOW_SECONDS:
                continue
            if (
                min(len(entry.normalized_text), len(normalized_text))
                < APPROX_MIN_TEXT_LENGTH
                or max(len(entry.normalized_text), len(normalized_text))
                > _MAX_APPROX_TEXT_LENGTH
            ):
                continue
            similarity = SequenceMatcher(
                None,
                entry.normalized_text,
                normalized_text,
                autojunk=False,
            ).ratio()
            if similarity >= APPROX_SIMILARITY_THRESHOLD:
                return entry
        return None

    @staticmethod
    def _should_replace(
        existing: _Entry,
        incoming: PreparedCard,
        *,
        normalized_text: str,
    ) -> bool:
        old_text = existing.normalized_text
        if old_text != normalized_text:
            return old_text in normalized_text and len(normalized_text) > len(old_text)
        return incoming.card.confidence > existing.prepared.card.confidence + 0.02

    def _remove_locked(self, entry_id: str) -> PreparedCard | None:
        entry = self._entries.pop(entry_id, None)
        if entry is None:
            return None
        self._memory_bytes -= entry.estimated_bytes
        self._owned_object_ids.remove(id(entry.prepared))
        return entry.prepared

    def _drain_locked(self) -> list[PreparedCard]:
        candidates = [entry.prepared for entry in self._entries.values()]
        self._entries.clear()
        self._owned_object_ids.clear()
        self._memory_bytes = 0
        return candidates

    @staticmethod
    def _close_candidates(candidates: Iterable[PreparedCard]) -> None:
        for candidate in candidates:
            candidate.close()
