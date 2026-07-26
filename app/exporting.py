"""把观点卡片导出为只读 JSON 或安全的 Markdown 文本。"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from urllib.parse import quote, urlsplit

from .models import Card

_STANCE_LABELS = {
    "unknown": "未标记",
    "agree": "认同",
    "disagree": "反对",
    "doubt": "存疑",
    "useful": "只是有用",
}
_BACKTICK_RUN = re.compile(r"`+")


def cards_to_json(cards: Iterable[Card]) -> str:
    """返回 UTF-8 友好的 JSON 数组，不解释或执行任何卡片内容。"""

    payload = [card.model_dump(mode="json") for card in cards]
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _fenced_data(value: str) -> str:
    """用比正文内任意反引号串更长的围栏包住不可信资料。"""

    longest = max((len(match.group(0)) for match in _BACKTICK_RUN.finditer(value)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{value}\n{fence}"


def _safe_http_url(value: str | None) -> str | None:
    """仅把无控制字符的 HTTP(S) 绝对 URL 转为 Markdown 目标。"""

    if not value or any(ord(character) < 32 or ord(character) == 127 for character in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
            return None
        # 尖括号目标不会被括号截断；对尖括号、空白和反斜杠等再次编码。
        return quote(
            value,
            safe=":/?#[]@!$&'()*+,;=%~.-_",
        )
    except ValueError:
        return None


def cards_to_markdown(cards: Iterable[Card]) -> str:
    """导出 Markdown。

    原文、整理文字、备注和非 HTTP(S) URL 一律进入动态长度代码围栏，避免
    恶意资料闭合围栏后注入 HTML。合法来源 URL 只生成普通外部链接。
    """

    sections = ["# 本地观点卡片导出", ""]
    for card in cards:
        sections.extend(
            [
                f"## 卡片 {card.id}",
                "",
                f"- 创建时间：`{card.created_at}`",
                f"- 文字来源：`{card.text_source}`",
                f"- 置信度：`{card.confidence:.4f}`",
                f"- 态度：`{_STANCE_LABELS[card.stance]}`（`{card.stance}`）",
                "",
                "### 提取原文",
                "",
                _fenced_data(card.text),
                "",
            ]
        )
        if card.edited_text is not None:
            sections.extend(
                [
                    "### 人工整理文字",
                    "",
                    _fenced_data(card.edited_text),
                    "",
                ]
            )
        if card.source_title:
            sections.extend(
                [
                    "### 来源标题",
                    "",
                    _fenced_data(card.source_title),
                    "",
                ]
            )
        if card.source_url:
            safe_url = _safe_http_url(card.source_url)
            sections.extend(["### 来源网址", ""])
            if safe_url is None:
                sections.extend([_fenced_data(card.source_url), ""])
            else:
                sections.extend([f"[打开来源](<{safe_url}>)", ""])
        if card.video_time is not None:
            sections.extend([f"- 视频时间：`{card.video_time:.3f}` 秒", ""])
        if card.note:
            sections.extend(["### 用户备注", "", _fenced_data(card.note), ""])
        sections.extend(
            [
                "### 本地截图路径",
                "",
                _fenced_data(card.screenshot_path),
                "",
                "### 本地完整截图路径",
                "",
                _fenced_data(card.full_screenshot_path),
                "",
                "---",
                "",
            ]
        )
    return "\n".join(sections).rstrip() + "\n"
