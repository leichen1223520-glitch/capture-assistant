"""RapidOCR 结果规范化、文字合并和失败边界测试。"""

from __future__ import annotations

import unittest
from dataclasses import dataclass

import numpy as np
from PIL import Image

import app.ocr as ocr_module
from app.ocr import OCRError, merge_ocr_boxes, normalize_ocr_output, ocr_image


def _box(
    left: float,
    top: float,
    right: float,
    bottom: float,
    text: str,
    confidence: float,
) -> dict[str, object]:
    return {
        "box": [[left, top], [right, top], [right, bottom], [left, bottom]],
        "text": text,
        "confidence": confidence,
    }


class _RecordingEngine:
    def __init__(self, output: object) -> None:
        self.output = output
        self.images: list[np.ndarray] = []

    def __call__(self, image: np.ndarray) -> object:
        self.images.append(image)
        return self.output


@dataclass
class _ParallelOutput:
    boxes: object
    txts: object
    scores: object


class OCRTests(unittest.TestCase):
    """覆盖 RapidOCR 1.x/新版格式、阅读顺序和证据保留。"""

    def test_module_keeps_engine_uninitialized_until_first_real_call(self) -> None:
        original = ocr_module._ENGINE
        try:
            ocr_module._ENGINE = None
            self.assertIsNone(ocr_module._ENGINE)
        finally:
            ocr_module._ENGINE = original

    def test_normalizes_rapidocr_1x_tuple_and_string_score(self) -> None:
        output = (
            [
                [
                    [[1, 2], [11, 2], [11, 8], [1, 8]],
                    "Local",
                    "0.88",
                ]
            ],
            [0.1, 0.2, 0.3],
        )

        boxes = normalize_ocr_output(output)

        self.assertEqual(boxes[0]["text"], "Local")
        self.assertEqual(boxes[0]["box"][0], [1.0, 2.0])
        self.assertAlmostEqual(boxes[0]["confidence"], 0.88)

    def test_normalizes_parallel_array_output(self) -> None:
        output = _ParallelOutput(
            boxes=np.array([[[0, 0], [10, 0], [10, 5], [0, 5]]]),
            txts=np.array(["证据"]),
            scores=np.array([0.91]),
        )

        boxes = normalize_ocr_output(output)

        self.assertEqual(boxes[0]["text"], "证据")
        self.assertAlmostEqual(boxes[0]["confidence"], 0.91)

    def test_accepts_empty_parallel_output(self) -> None:
        self.assertEqual(normalize_ocr_output(_ParallelOutput(None, None, None)), [])

    def test_merges_rows_and_sorts_each_row_by_x(self) -> None:
        boxes = [
            _box(90, 50, 150, 70, "first", 0.7),
            _box(50, 10, 90, 30, "优先", 0.9),
            _box(10, 50, 80, 70, "Local", 0.8),
            _box(10, 11, 45, 31, "本地", 0.95),
        ]

        text, confidence, ordered = merge_ocr_boxes(boxes)  # type: ignore[arg-type]

        self.assertEqual(text, "本地优先\nLocal first")
        self.assertEqual([box["text"] for box in ordered], ["本地", "优先", "Local", "first"])
        expected = (0.95 * 2 + 0.9 * 2 + 0.8 * 5 + 0.7 * 5) / 14
        self.assertAlmostEqual(confidence, expected)

    def test_keeps_low_confidence_text_and_punctuation(self) -> None:
        boxes = [
            _box(0, 0, 20, 10, "观点", 0.99),
            _box(21, 0, 25, 10, "，", 0.05),
            _box(26, 0, 50, 10, "存疑", 0.4),
            _box(51, 0, 55, 10, "。", 0.1),
        ]

        text, _, ordered = merge_ocr_boxes(boxes)  # type: ignore[arg-type]

        self.assertEqual(text, "观点，存疑。")
        self.assertEqual(len(ordered), 4)
        self.assertEqual(ordered[1]["confidence"], 0.05)

    def test_empty_result_is_honest_empty_output(self) -> None:
        engine = _RecordingEngine((None, [0.0, 0.0, 0.0]))

        result = ocr_image(Image.new("L", (20, 10), 255), engine=engine)

        self.assertEqual(result, ("", 0.0, []))
        self.assertEqual(engine.images[0].shape, (10, 20, 3))

    def test_ocr_image_preserves_boxes_and_uses_rgb_array(self) -> None:
        raw = [[[[0, 0], [10, 0], [10, 5], [0, 5]], "Evidence", 0.8]]
        engine = _RecordingEngine((raw, [0.1, 0.1, 0.1]))

        text, confidence, boxes = ocr_image(
            Image.new("RGBA", (30, 20), (10, 20, 30, 40)),
            engine=engine,
        )

        self.assertEqual(text, "Evidence")
        self.assertAlmostEqual(confidence, 0.8)
        self.assertEqual(boxes[0]["box"], [[0.0, 0.0], [10.0, 0.0], [10.0, 5.0], [0.0, 5.0]])
        self.assertEqual(engine.images[0].dtype, np.uint8)

    def test_rejects_malformed_or_out_of_range_engine_output(self) -> None:
        invalid_outputs = (
            [[[[0, 0], [1, 0]], "broken", 0.5]],
            [[[[0, 0], [1, 0], [1, 1], [0, 1]], "broken", 1.5]],
            {"boxes": [], "txts": []},
        )
        for output in invalid_outputs:
            with self.subTest(output=output):
                with self.assertRaises(OCRError):
                    normalize_ocr_output(output)

    def test_wraps_engine_failure_without_including_image_content(self) -> None:
        def broken_engine(image: np.ndarray) -> object:
            del image
            raise RuntimeError("engine internal failure")

        with self.assertRaisesRegex(OCRError, "本地 OCR 执行失败") as context:
            ocr_image(Image.new("RGB", (10, 10), "white"), engine=broken_engine)
        self.assertNotIn("engine internal failure", str(context.exception))

    def test_rejects_non_image_input(self) -> None:
        with self.assertRaises(TypeError):
            ocr_image("not an image")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
