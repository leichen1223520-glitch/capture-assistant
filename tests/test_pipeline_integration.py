"""使用真实离线 OCR 验证一次框选到本地卡片的完整数据闭环。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from PySide6.QtCore import QRect

from app.capture import CaptureMeta
from app.pipeline import build_card_from_selection
from app.store import Store
from tests.test_ocr_integration import ARIAL, _text_image


@pytest.mark.skipif(
    sys.platform != "win32" or not ARIAL.is_file(),
    reason="真实流水线集成测试需要 Windows 自带 Arial 字体",
)
def test_real_ocr_screenshot_database_and_delete_round_trip(tmp_path: Path) -> None:
    data_dir = tmp_path / "pipeline-data"
    screenshot_dir = data_dir / "screenshots"
    store = Store(db_path=data_dir / "cards.sqlite3", data_dir=data_dir)
    store.init_db()

    image = _text_image("LOCAL EVIDENCE", ARIAL)
    meta = CaptureMeta(
        monitor_index=1,
        left=0,
        top=0,
        width=image.width,
        height=image.height,
        scale=1.0,
        device_name=r"\\.\DISPLAY1",
        app_name="notepad.exe",
        captured_at="2026-07-22T15:30:00+08:00",
    )

    card = build_card_from_selection(
        image,
        meta,
        QRect(0, 0, image.width, image.height),
        store=store,
        context_provider=lambda: None,
        data_dir=data_dir,
        screenshot_dir=screenshot_dir,
    )

    assert "LOCAL" in card.text.upper()
    assert "EVIDENCE" in card.text.upper()
    assert card.text_source == "ocr"
    assert card.confidence > 0.5
    assert card.stance == "unknown"
    assert store.get_card(card.id) == card

    selected_path = data_dir / card.screenshot_path
    full_path = data_dir / card.full_screenshot_path
    assert selected_path.is_file()
    assert full_path.is_file()
    assert store.delete_card(card.id)
    assert store.get_card(card.id) is None
    assert not selected_path.exists()
    assert not full_path.exists()
