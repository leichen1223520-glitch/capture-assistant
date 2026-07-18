"""SQLite 卡片存储、FTS5 全文检索和本地截图删除。

每个公开操作都使用独立连接并显式关闭。SQL 全部参数化；日志和异常不会包含
卡片正文、URL 或用户查询。默认优先使用适合中文子串的 FTS5 trigram 分词器，
不可用时降级为 unicode61，并对短中文查询使用普通子串匹配兜底。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Final

from pydantic import ValidationError

from .config import DATA_DIR, DB_PATH
from .models import Card

FTS_TOKENIZERS: Final = ("trigram", "unicode61")
UPDATABLE_FIELDS: Final = frozenset(
    {
        "text",
        "text_source",
        "confidence",
        "source_url",
        "source_title",
        "video_time",
        "app_name",
        "monitor",
        "stance",
        "note",
    }
)


class StoreError(RuntimeError):
    """表示数据库、数据契约或受控文件清理失败。"""


class Store:
    """观点卡片的本地 SQLite 仓库。"""

    def __init__(
        self,
        db_path: str | Path = DB_PATH,
        data_dir: str | Path = DATA_DIR,
    ) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        self.data_dir = Path(data_dir).expanduser().resolve()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def init_db(self) -> None:
        """幂等创建卡片表、FTS5 索引及同步触发器。"""

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        try:
            with closing(self._connect()) as connection, connection:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA synchronous = NORMAL")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS cards (
                        id TEXT PRIMARY KEY NOT NULL,
                        text TEXT NOT NULL,
                        text_source TEXT NOT NULL CHECK (text_source IN ('dom', 'ocr')),
                        confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
                        screenshot_path TEXT NOT NULL,
                        full_screenshot_path TEXT NOT NULL,
                        source_url TEXT,
                        source_title TEXT,
                        video_time REAL CHECK (video_time IS NULL OR video_time >= 0),
                        app_name TEXT,
                        monitor_json TEXT,
                        created_at TEXT NOT NULL,
                        stance TEXT NOT NULL CHECK (
                            stance IN ('unknown', 'agree', 'disagree', 'doubt', 'useful')
                        ),
                        note TEXT NOT NULL DEFAULT ''
                    );

                    CREATE TABLE IF NOT EXISTS app_meta (
                        key TEXT PRIMARY KEY NOT NULL,
                        value TEXT NOT NULL
                    );
                    """
                )
                tokenizer = self._ensure_fts_table(connection)
                connection.executescript(
                    """
                    CREATE TRIGGER IF NOT EXISTS cards_ai AFTER INSERT ON cards BEGIN
                        INSERT INTO cards_fts(card_id, text, source_title, note)
                        VALUES (
                            new.id,
                            new.text,
                            COALESCE(new.source_title, ''),
                            new.note
                        );
                    END;

                    CREATE TRIGGER IF NOT EXISTS cards_ad AFTER DELETE ON cards BEGIN
                        DELETE FROM cards_fts WHERE card_id = old.id;
                    END;

                    CREATE TRIGGER IF NOT EXISTS cards_au
                    AFTER UPDATE OF text, source_title, note ON cards BEGIN
                        DELETE FROM cards_fts WHERE card_id = old.id;
                        INSERT INTO cards_fts(card_id, text, source_title, note)
                        VALUES (
                            new.id,
                            new.text,
                            COALESCE(new.source_title, ''),
                            new.note
                        );
                    END;
                    """
                )
                # 首次升级旧库时重建一次索引；重复初始化也不会产生重复记录。
                connection.execute("DELETE FROM cards_fts")
                connection.execute(
                    """
                    INSERT INTO cards_fts(card_id, text, source_title, note)
                    SELECT id, text, COALESCE(source_title, ''), note FROM cards
                    """
                )
                connection.execute(
                    """
                    INSERT INTO app_meta(key, value) VALUES ('fts_tokenizer', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (tokenizer,),
                )
        except sqlite3.Error as exc:
            raise StoreError("无法初始化本地 SQLite/FTS5 数据库。") from exc

    @staticmethod
    def _ensure_fts_table(connection: sqlite3.Connection) -> str:
        existing = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'cards_fts'"
        ).fetchone()
        if existing is not None:
            schema_sql = str(existing["sql"] or "").casefold()
            return "trigram" if "trigram" in schema_sql else "unicode61"

        try:
            connection.execute(
                """
                CREATE VIRTUAL TABLE cards_fts USING fts5(
                    card_id UNINDEXED,
                    text,
                    source_title,
                    note,
                    tokenize = 'trigram'
                )
                """
            )
            return "trigram"
        except sqlite3.OperationalError:
            connection.execute(
                """
                CREATE VIRTUAL TABLE cards_fts USING fts5(
                    card_id UNINDEXED,
                    text,
                    source_title,
                    note,
                    tokenize = 'unicode61'
                )
                """
            )
            return "unicode61"

    def fts_tokenizer(self) -> str:
        """返回当前数据库实际使用的 FTS5 分词器。"""

        try:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT value FROM app_meta WHERE key = 'fts_tokenizer'"
                ).fetchone()
        except sqlite3.Error as exc:
            raise StoreError("无法读取本地全文检索配置。") from exc
        if row is None or row["value"] not in FTS_TOKENIZERS:
            raise StoreError("数据库尚未初始化或全文检索配置无效。")
        return str(row["value"])

    @staticmethod
    def _serialize_card(card: Card) -> tuple[Any, ...]:
        monitor_json = (
            json.dumps(card.monitor, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            if card.monitor is not None
            else None
        )
        return (
            card.id,
            card.text,
            card.text_source,
            card.confidence,
            card.screenshot_path,
            card.full_screenshot_path,
            card.source_url,
            card.source_title,
            card.video_time,
            card.app_name,
            monitor_json,
            card.created_at,
            card.stance,
            card.note,
        )

    @staticmethod
    def _row_to_card(row: sqlite3.Row) -> Card:
        try:
            monitor = json.loads(row["monitor_json"]) if row["monitor_json"] else None
            return Card(
                id=row["id"],
                text=row["text"],
                text_source=row["text_source"],
                confidence=row["confidence"],
                screenshot_path=row["screenshot_path"],
                full_screenshot_path=row["full_screenshot_path"],
                source_url=row["source_url"],
                source_title=row["source_title"],
                video_time=row["video_time"],
                app_name=row["app_name"],
                monitor=monitor,
                created_at=row["created_at"],
                stance=row["stance"],
                note=row["note"],
            )
        except (json.JSONDecodeError, TypeError, ValidationError) as exc:
            raise StoreError("数据库中的卡片记录不符合当前数据契约。") from exc

    def add_card(self, card: Card) -> Card:
        """验证并新增一张卡片；重复 ID 会明确失败。"""

        try:
            validated = Card.model_validate(card.model_dump())
        except (AttributeError, ValidationError) as exc:
            raise StoreError("待保存卡片不符合数据契约。") from exc
        try:
            with closing(self._connect()) as connection, connection:
                connection.execute(
                    """
                    INSERT INTO cards(
                        id, text, text_source, confidence,
                        screenshot_path, full_screenshot_path,
                        source_url, source_title, video_time, app_name,
                        monitor_json, created_at, stance, note
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    self._serialize_card(validated),
                )
        except sqlite3.IntegrityError as exc:
            raise StoreError("卡片 ID 已存在或字段违反数据库约束。") from exc
        except sqlite3.Error as exc:
            raise StoreError("无法保存本地卡片。") from exc
        return validated

    def get_card(self, card_id: str) -> Card | None:
        """按 ID 返回卡片，不存在时返回 ``None``。"""

        try:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT * FROM cards WHERE id = ?",
                    (card_id,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise StoreError("无法读取本地卡片。") from exc
        return None if row is None else self._row_to_card(row)

    def update_card(self, card_id: str, **changes: Any) -> Card:
        """经字段白名单和完整 Card 校验后更新卡片。"""

        unknown_fields = set(changes) - UPDATABLE_FIELDS
        if unknown_fields:
            raise StoreError("更新包含不允许修改的卡片字段。")
        current = self.get_card(card_id)
        if current is None:
            raise StoreError("待更新卡片不存在。")
        if not changes:
            return current

        payload = current.model_dump()
        payload.update(changes)
        try:
            validated = Card.model_validate(payload)
        except ValidationError as exc:
            raise StoreError("更新后的卡片不符合数据契约。") from exc

        monitor_json = (
            json.dumps(
                validated.monitor,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            if validated.monitor is not None
            else None
        )
        try:
            with closing(self._connect()) as connection, connection:
                cursor = connection.execute(
                    """
                    UPDATE cards SET
                        text = ?, text_source = ?, confidence = ?,
                        source_url = ?, source_title = ?, video_time = ?,
                        app_name = ?, monitor_json = ?, stance = ?, note = ?
                    WHERE id = ?
                    """,
                    (
                        validated.text,
                        validated.text_source,
                        validated.confidence,
                        validated.source_url,
                        validated.source_title,
                        validated.video_time,
                        validated.app_name,
                        monitor_json,
                        validated.stance,
                        validated.note,
                        card_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise StoreError("待更新卡片在写入前已不存在。")
        except sqlite3.Error as exc:
            raise StoreError("无法更新本地卡片。") from exc
        return validated

    def list_recent(self, limit: int = 50, offset: int = 0) -> list[Card]:
        """按创建时间倒序返回最近卡片。"""

        self._validate_page(limit, offset)
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    """
                    SELECT * FROM cards
                    ORDER BY datetime(created_at) DESC, rowid DESC
                    LIMIT ? OFFSET ?
                    """,
                    (limit, offset),
                ).fetchall()
        except sqlite3.Error as exc:
            raise StoreError("无法列出本地卡片。") from exc
        return [self._row_to_card(row) for row in rows]

    @staticmethod
    def _validate_page(limit: int, offset: int) -> None:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 500
            or isinstance(offset, bool)
            or not isinstance(offset, int)
            or offset < 0
        ):
            raise StoreError("分页参数无效：limit 需为 1–500，offset 不能为负数。")

    def search(self, query: str, limit: int = 50) -> list[Card]:
        """在正文、来源标题和备注中检索，不解释或执行资料内容。"""

        self._validate_page(limit, 0)
        normalized = " ".join(query.split())
        if not normalized:
            return []
        if "\x00" in normalized:
            raise StoreError("搜索词包含无效字符。")

        try:
            with closing(self._connect()) as connection:
                tokenizer_row = connection.execute(
                    "SELECT value FROM app_meta WHERE key = 'fts_tokenizer'"
                ).fetchone()
                tokenizer = str(tokenizer_row["value"]) if tokenizer_row else "unicode61"
                has_cjk = any(
                    "\u3400" <= character <= "\u9fff" for character in normalized
                )
                if (
                    (tokenizer == "trigram" and len(normalized) < 3)
                    or (tokenizer == "unicode61" and has_cjk)
                ):
                    rows = self._substring_search(connection, normalized, limit)
                else:
                    match_query = " AND ".join(
                        f'"{part.replace(chr(34), chr(34) * 2)}"'
                        for part in normalized.split()
                    )
                    try:
                        rows = connection.execute(
                            """
                            SELECT cards.*
                            FROM cards_fts
                            JOIN cards ON cards.id = cards_fts.card_id
                            WHERE cards_fts MATCH ?
                            ORDER BY bm25(cards_fts), datetime(cards.created_at) DESC
                            LIMIT ?
                            """,
                            (match_query, limit),
                        ).fetchall()
                        if not rows:
                            rows = self._substring_search(connection, normalized, limit)
                    except sqlite3.OperationalError:
                        rows = self._substring_search(connection, normalized, limit)
        except sqlite3.Error as exc:
            raise StoreError("无法搜索本地卡片。") from exc
        return [self._row_to_card(row) for row in rows]

    @staticmethod
    def _substring_search(
        connection: sqlite3.Connection,
        query: str,
        limit: int,
    ) -> list[sqlite3.Row]:
        return connection.execute(
            """
            SELECT * FROM cards
            WHERE instr(text, ?) > 0
               OR instr(COALESCE(source_title, ''), ?) > 0
               OR instr(note, ?) > 0
            ORDER BY datetime(created_at) DESC, rowid DESC
            LIMIT ?
            """,
            (query, query, query, limit),
        ).fetchall()

    def _safe_screenshot_path(self, relative_path: str) -> Path:
        candidate = (self.data_dir / relative_path).resolve()
        if candidate == self.data_dir or self.data_dir not in candidate.parents:
            raise StoreError("卡片截图路径超出本地数据目录，已拒绝删除。")
        return candidate

    def delete_card(self, card_id: str) -> bool:
        """删除卡片、FTS 记录和两张本地截图；不存在时返回 ``False``。

        为优先保护隐私，先清理经路径边界验证的截图，再提交数据库删除。若极少见
        的数据库写入失败，可能留下指向已删除截图的卡片，但不会留下未受控副本。
        """

        card = self.get_card(card_id)
        if card is None:
            return False

        paths = {
            self._safe_screenshot_path(card.screenshot_path),
            self._safe_screenshot_path(card.full_screenshot_path),
        }
        for path in paths:
            try:
                if path.is_dir():
                    raise StoreError("卡片截图路径指向目录，已拒绝删除。")
                path.unlink(missing_ok=True)
            except OSError as exc:
                raise StoreError("无法删除本地卡片截图；数据库记录仍被保留。") from exc

        try:
            with closing(self._connect()) as connection, connection:
                cursor = connection.execute("DELETE FROM cards WHERE id = ?", (card_id,))
                if cursor.rowcount != 1:
                    raise StoreError("待删除卡片在写入前已不存在。")
        except sqlite3.Error as exc:
            raise StoreError("截图已清理，但无法删除本地数据库记录。") from exc
        return True
