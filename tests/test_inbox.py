"""验证半自动观察候选只在有界、线程安全的内存收件箱中存在。"""

from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Thread
from unittest.mock import patch
from uuid import uuid4

from PIL import Image

from app.inbox import (
    MAX_CANDIDATES,
    MAX_MEMORY_BYTES,
    CandidateInbox,
    estimate_candidate_bytes,
    normalize_candidate_text,
)
from app.models import Card
from app.pipeline import PreparedCard


class CandidateInboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.inboxes: list[CandidateInbox] = []
        self.candidates: list[PreparedCard] = []

    def tearDown(self) -> None:
        for inbox in self.inboxes:
            inbox.close()
        for candidate in self.candidates:
            candidate.close()

    def _inbox(self, **kwargs: int) -> CandidateInbox:
        inbox = CandidateInbox(**kwargs)
        self.inboxes.append(inbox)
        return inbox

    def _candidate(
        self,
        text: str,
        *,
        confidence: float = 0.8,
        size: tuple[int, int] = (12, 8),
    ) -> PreparedCard:
        card_id = str(uuid4())
        candidate = PreparedCard(
            card=Card(
                id=card_id,
                text=text,
                text_source="ocr",
                confidence=confidence,
                screenshot_path=f"screenshots/{card_id}.png",
                full_screenshot_path=f"screenshots/full_{card_id}.png",
                source_url="https://example.test/watch?v=one",
                source_title="纯内存候选",
                video_time=3.5,
            ),
            selected_image=Image.new("RGB", (5, 3), "white"),
            full_image=Image.new("RGB", size, "black"),
            selected_preview_png=b"\x89PNG\r\n\x1a\nselected",
            full_preview_png=b"\x89PNG\r\n\x1a\nfull",
        )
        self.candidates.append(candidate)
        return candidate

    @staticmethod
    def _offer(
        inbox: CandidateInbox,
        candidate: PreparedCard,
        *,
        now: float,
        session_id: str = "session-one",
        source_key: str = "video-one",
        region_key: str = "subtitle-region",
    ):
        return inbox.offer(
            candidate,
            session_id=session_id,
            source_key=source_key,
            region_key=region_key,
            now=now,
        )

    def test_defaults_and_constructor_never_allow_larger_bounds(self) -> None:
        inbox = self._inbox()

        self.assertEqual(inbox.max_candidates, 20)
        self.assertEqual(inbox.max_memory_bytes, 192 * 1024 * 1024)
        self.assertEqual(MAX_CANDIDATES, 20)
        self.assertEqual(MAX_MEMORY_BYTES, 192 * 1024 * 1024)
        with self.assertRaises(ValueError):
            CandidateInbox(max_candidates=21)
        with self.assertRaises(ValueError):
            CandidateInbox(max_memory_bytes=MAX_MEMORY_BYTES + 1)

    def test_normalization_uses_nfkc_whitespace_and_casefold(self) -> None:
        self.assertEqual(normalize_candidate_text("  Ａ\tB\n C  "), "a b c")
        self.assertEqual(normalize_candidate_text("Straße"), "strasse")

    def test_offer_snapshot_and_take_transfer_prepared_card_ownership(self) -> None:
        inbox = self._inbox()
        candidate = self._candidate("等待人工审核")

        result = self._offer(inbox, candidate, now=10.0)

        self.assertEqual(result.status, "added")
        self.assertTrue(result.accepted)
        self.assertEqual(len(inbox), 1)
        summary = inbox.snapshot()[0]
        self.assertEqual(summary.entry_id, result.entry_id)
        self.assertEqual(summary.card_id, candidate.card.id)
        self.assertEqual(summary.text, "等待人工审核")
        self.assertEqual(summary.occurrences, 1)
        self.assertFalse(hasattr(summary, "prepared"))
        self.assertFalse(candidate.is_closed)

        assert result.entry_id is not None
        transferred = inbox.take(result.entry_id)
        self.assertIs(transferred, candidate)
        self.assertEqual(len(inbox), 0)
        self.assertEqual(inbox.memory_bytes, 0)
        self.assertFalse(candidate.is_closed)
        self.assertIsNone(inbox.take(result.entry_id))

        candidate.close()
        self.assertTrue(candidate.is_closed)

    def test_exact_duplicate_uses_nfkc_normalization_within_twelve_seconds(self) -> None:
        inbox = self._inbox()
        first = self._candidate("  ＡＢＣ\n观点 ")
        duplicate = self._candidate("abc 观点")

        initial = self._offer(inbox, first, now=1.0)
        merged = self._offer(inbox, duplicate, now=13.0)

        self.assertEqual(merged.status, "merged")
        self.assertEqual(merged.entry_id, initial.entry_id)
        self.assertEqual(merged.occurrences, 2)
        self.assertEqual(len(inbox), 1)
        self.assertTrue(duplicate.is_closed)
        self.assertFalse(first.is_closed)
        self.assertEqual(inbox.snapshot()[0].last_seen, 13.0)

    def test_exact_duplicate_after_window_becomes_new_entry(self) -> None:
        inbox = self._inbox()
        first = self._candidate("重复观点")
        later = self._candidate("重复观点")

        self._offer(inbox, first, now=1.0)
        result = self._offer(inbox, later, now=13.01)

        self.assertEqual(result.status, "added")
        self.assertEqual(len(inbox), 2)
        self.assertFalse(first.is_closed)
        self.assertFalse(later.is_closed)

    def test_conservative_approximate_duplicate_requires_length_time_and_ratio(self) -> None:
        inbox = self._inbox()
        first = self._candidate("abcdefghijklmno")
        close_match = self._candidate("abcdefghijklmnp")
        short_first = self._candidate("abcdefg")
        short_change = self._candidate("abcdefh")
        old_match = self._candidate("abcdefghijklmna")

        initial = self._offer(inbox, first, now=10.0)
        merged = self._offer(inbox, close_match, now=14.0)
        self.assertEqual(merged.status, "merged")
        self.assertEqual(merged.entry_id, initial.entry_id)
        self.assertTrue(close_match.is_closed)

        self._offer(inbox, short_first, now=20.0)
        short_result = self._offer(inbox, short_change, now=20.5)
        self.assertEqual(short_result.status, "added")

        old_result = self._offer(inbox, old_match, now=18.01)
        self.assertEqual(old_result.status, "added")
        self.assertEqual(len(inbox), 4)

    def test_deduplication_never_crosses_session_source_or_region(self) -> None:
        inbox = self._inbox()
        candidates = [self._candidate("相同文字") for _ in range(4)]

        self._offer(inbox, candidates[0], now=1.0)
        self._offer(inbox, candidates[1], now=2.0, session_id="session-two")
        self._offer(inbox, candidates[2], now=3.0, source_key="video-two")
        self._offer(inbox, candidates[3], now=4.0, region_key="region-two")

        self.assertEqual(len(inbox), 4)
        self.assertTrue(all(not candidate.is_closed for candidate in candidates))

    def test_progressive_near_duplicate_replaces_incomplete_owned_evidence(self) -> None:
        inbox = self._inbox()
        incomplete = self._candidate("abcdefghijklmno", confidence=0.7)
        complete = self._candidate("abcdefghijklmnop", confidence=0.75)

        first_result = self._offer(inbox, incomplete, now=1.0)
        merged = self._offer(inbox, complete, now=2.0)

        self.assertEqual(merged.status, "merged")
        self.assertEqual(merged.entry_id, first_result.entry_id)
        self.assertTrue(incomplete.is_closed)
        self.assertFalse(complete.is_closed)
        self.assertEqual(inbox.snapshot()[0].card_id, complete.card.id)
        assert merged.entry_id is not None
        self.assertIs(inbox.take(merged.entry_id), complete)

    def test_count_limit_evicts_oldest_and_closes_it(self) -> None:
        inbox = self._inbox(max_candidates=2)
        first = self._candidate("候选甲")
        second = self._candidate("候选乙")
        third = self._candidate("候选丙")

        first_result = self._offer(inbox, first, now=1.0)
        self._offer(inbox, second, now=2.0)
        third_result = self._offer(inbox, third, now=3.0)

        self.assertEqual(len(inbox), 2)
        self.assertEqual(third_result.evicted_entry_ids, (first_result.entry_id,))
        self.assertTrue(first.is_closed)
        self.assertFalse(second.is_closed)
        self.assertFalse(third.is_closed)

    def test_memory_limit_evicts_oldest_and_rejects_single_oversized_candidate(self) -> None:
        probe = self._candidate("候选甲")
        one_size = estimate_candidate_bytes(probe)
        probe.close()

        inbox = self._inbox(max_memory_bytes=one_size * 2 - 1)
        first = self._candidate("候选甲")
        second = self._candidate("候选乙")
        first_result = self._offer(inbox, first, now=1.0)
        second_result = self._offer(inbox, second, now=2.0)

        self.assertEqual(second_result.evicted_entry_ids, (first_result.entry_id,))
        self.assertTrue(first.is_closed)
        self.assertFalse(second.is_closed)
        self.assertLessEqual(inbox.memory_bytes, inbox.max_memory_bytes)

        small_inbox = self._inbox(max_memory_bytes=one_size - 1)
        oversized = self._candidate("候选甲")
        rejected = self._offer(small_inbox, oversized, now=1.0)
        self.assertEqual(rejected.status, "too_large")
        self.assertFalse(rejected.accepted)
        self.assertTrue(oversized.is_closed)
        self.assertEqual(len(small_inbox), 0)

    def test_discard_many_and_discard_all_release_candidates_without_closing_inbox(
        self,
    ) -> None:
        inbox = self._inbox()
        candidates = [self._candidate(f"不同候选 {index}") for index in range(4)]
        results = [
            self._offer(
                inbox,
                candidate,
                now=float(index),
                source_key=f"source-{index}",
            )
            for index, candidate in enumerate(candidates)
        ]
        ids = [result.entry_id for result in results]
        self.assertTrue(all(entry_id is not None for entry_id in ids))

        removed = inbox.discard_many([ids[0], ids[2], ids[0]])  # type: ignore[list-item]
        self.assertEqual(removed, (ids[0], ids[2]))
        self.assertTrue(candidates[0].is_closed)
        self.assertFalse(candidates[1].is_closed)
        self.assertTrue(candidates[2].is_closed)
        self.assertFalse(candidates[3].is_closed)
        self.assertEqual(inbox.discard_all(), 2)
        self.assertTrue(candidates[1].is_closed)
        self.assertTrue(candidates[3].is_closed)
        self.assertEqual(len(inbox), 0)
        self.assertFalse(inbox.closed)

        replacement = self._candidate("清空后仍可接收")
        self.assertTrue(self._offer(inbox, replacement, now=10.0).accepted)

    def test_close_is_idempotent_and_consumes_later_offers(self) -> None:
        inbox = self._inbox()
        existing = self._candidate("关闭前候选")
        self._offer(inbox, existing, now=1.0)

        inbox.close()
        inbox.close()

        self.assertTrue(inbox.closed)
        self.assertTrue(existing.is_closed)
        self.assertEqual(len(inbox), 0)
        later = self._candidate("关闭后候选")
        result = self._offer(inbox, later, now=2.0)
        self.assertEqual(result.status, "closed")
        self.assertTrue(later.is_closed)

    def test_invalid_offer_consumes_candidate_but_reoffering_owned_object_does_not(
        self,
    ) -> None:
        inbox = self._inbox()
        invalid = self._candidate("参数错误")
        with self.assertRaises(ValueError):
            self._offer(inbox, invalid, now=1.0, session_id=" ")
        self.assertTrue(invalid.is_closed)

        owned = self._candidate("已被接管")
        self._offer(inbox, owned, now=2.0)
        with self.assertRaisesRegex(ValueError, "已由此收件箱持有"):
            self._offer(inbox, owned, now=3.0)
        self.assertFalse(owned.is_closed)
        self.assertEqual(len(inbox), 1)

    def test_release_happens_after_internal_lock_is_available_to_other_thread(self) -> None:
        inbox = self._inbox()
        candidate = self._candidate("锁外释放")
        result = self._offer(inbox, candidate, now=1.0)
        assert result.entry_id is not None
        original_close = PreparedCard.close
        lock_was_available: list[bool] = []

        def observed_close(target: PreparedCard) -> None:
            completed = Event()

            def read_inbox() -> None:
                inbox.snapshot()
                completed.set()

            reader = Thread(target=read_inbox)
            reader.start()
            lock_was_available.append(completed.wait(1.0))
            reader.join(timeout=1.0)
            original_close(target)

        with patch.object(PreparedCard, "close", observed_close):
            self.assertTrue(inbox.discard(result.entry_id))

        self.assertEqual(lock_was_available, [True])
        self.assertTrue(candidate.is_closed)

    def test_concurrent_offers_and_discards_preserve_bounds_and_release_ownership(
        self,
    ) -> None:
        inbox = self._inbox()
        candidates = [self._candidate(f"并发候选 {index:03d}") for index in range(80)]

        def offer_one(index: int) -> None:
            self._offer(
                inbox,
                candidates[index],
                now=float(index),
                source_key=f"source-{index}",
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(offer_one, range(len(candidates))))

        self.assertLessEqual(len(inbox), MAX_CANDIDATES)
        self.assertLessEqual(inbox.memory_bytes, MAX_MEMORY_BYTES)
        retained_card_ids = {summary.card_id for summary in inbox.snapshot()}
        for candidate in candidates:
            self.assertEqual(
                candidate.is_closed,
                candidate.card.id not in retained_card_ids,
            )

        retained_entry_ids = [summary.entry_id for summary in inbox.snapshot()]
        with ThreadPoolExecutor(max_workers=8) as executor:
            removed = list(executor.map(inbox.discard, retained_entry_ids))

        self.assertTrue(all(removed))
        self.assertEqual(len(inbox), 0)
        self.assertEqual(inbox.memory_bytes, 0)
        self.assertTrue(all(candidate.is_closed for candidate in candidates))

    def test_concurrent_offer_of_same_object_has_exactly_one_owner(self) -> None:
        inbox = self._inbox()
        candidate = self._candidate("同一个对象只能有一个所有者")
        barrier = Barrier(2)
        from app.inbox import estimate_candidate_bytes as original_estimate

        def synchronized_estimate(value: PreparedCard) -> int:
            result = original_estimate(value)
            barrier.wait(timeout=2.0)
            return result

        def offer_same() -> object:
            return self._offer(inbox, candidate, now=1.0)

        with (
            patch("app.inbox.estimate_candidate_bytes", synchronized_estimate),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            futures = [executor.submit(offer_same) for _ in range(2)]
            outcomes: list[object] = []
            errors: list[BaseException] = []
            for future in futures:
                try:
                    outcomes.append(future.result())
                except BaseException as exc:
                    errors.append(exc)

        self.assertEqual(len(outcomes), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ValueError)
        self.assertEqual(len(inbox), 1)
        self.assertFalse(candidate.is_closed)
        self.assertEqual(inbox.snapshot()[0].card_id, candidate.card.id)

    def test_long_repetitive_text_skips_expensive_approximate_matching(self) -> None:
        inbox = self._inbox()
        first = self._candidate("甲" * 4_096)
        second = self._candidate(("甲" * 4_095) + "乙")
        self._offer(inbox, first, now=1.0)

        with patch(
            "app.inbox.SequenceMatcher",
            side_effect=AssertionError("长文本不应进入近似比较"),
        ):
            result = self._offer(inbox, second, now=2.0)

        self.assertEqual(result.status, "added")
        self.assertEqual(len(inbox), 2)

if __name__ == "__main__":
    unittest.main()
