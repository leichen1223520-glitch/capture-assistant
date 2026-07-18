"""使用内存合成中英文样图验证仓库内 RapidOCR 模型。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app.ocr import ocr_image

WINDOWS_FONTS = Path("C:/Windows/Fonts")
ARIAL = WINDOWS_FONTS / "arial.ttf"
MICROSOFT_YAHEI = WINDOWS_FONTS / "msyh.ttc"


def _text_image(text: str, font_path: Path) -> Image.Image:
    """在内存中生成高对比度固定 OCR 样图，不写入磁盘。"""

    image = Image.new("RGB", (760, 170), "white")
    font = ImageFont.truetype(str(font_path), 72)
    ImageDraw.Draw(image).text((25, 30), text, font=font, fill="black")
    return image


@unittest.skipUnless(
    sys.platform == "win32" and ARIAL.is_file() and MICROSOFT_YAHEI.is_file(),
    "真实 OCR 集成测试需要 Windows 自带 Arial 和微软雅黑字体",
)
class RealOCRIntegrationTests(unittest.TestCase):
    """确认离线模型能识别固定中英文关键字且置信度可用。"""

    def test_recognizes_fixed_english_sample(self) -> None:
        text, confidence, boxes = ocr_image(_text_image("LOCAL EVIDENCE", ARIAL))

        self.assertIn("LOCAL", text.upper())
        self.assertIn("EVIDENCE", text.upper())
        self.assertGreater(confidence, 0.5)
        self.assertTrue(boxes)

    def test_recognizes_fixed_chinese_sample(self) -> None:
        text, confidence, boxes = ocr_image(_text_image("本地优先", MICROSOFT_YAHEI))

        self.assertIn("本地优先", text)
        self.assertGreater(confidence, 0.5)
        self.assertTrue(boxes)


if __name__ == "__main__":
    unittest.main()
