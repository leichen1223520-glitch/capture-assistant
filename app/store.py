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
from .models import Card, Stance, TextSource

FTS_TOKENIZERS: Final = ("trigram", "unicode61")
RECORD_STATES: Final = ("draft", "saved")
MAX_QUERY_CHARACTERS: Final = 500
UPDATABLE_FIELDS: Final = frozenset(
    {
        "edited_text",
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
        self.screenshot_dir = self.data_dir / "screenshots"

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
                        edited_text TEXT,
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
                        note TEXT NOT NULL DEFAULT '',
                        record_state TEXT NOT NULL DEFAULT 'saved' CHECK (
                            record_state IN ('draft', 'saved')
                        )
                    );

                    CREATE TABLE IF NOT EXISTS app_meta (
                        key TEXT PRIMARY KEY NOT NULL,
                        value TEXT NOT NULL
                    );
                    """
                )
                self._ensure_edited_text_column(connection)
                self._ensure_record_state_column(connection)
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS cards_state_created_idx
                    ON cards(record_state, created_at)
                    """
                )
                connection.executescript(
                    """
                    DROP TRIGGER IF EXISTS cards_ai;
                    DROP TRIGGER IF EXISTS cards_ad;
                    DROP TRIGGER IF EXISTS cards_au;
                    """
                )
                tokenizer = self._ensure_fts_table(connection)
                connection.executescript(
                    """
                    DROP TRIGGER IF EXISTS cards_ai;
                    DROP TRIGGER IF EXISTS cards_ad;
                    DROP TRIGGER IF EXISTS cards_au;

                    CREATE TRIGGER cards_ai AFTER INSERT ON cards
                    WHEN new.record_state = 'saved' BEGIN
                        INSERT INTO cards_fts(
                            card_id, text, edited_text, source_title, note
                        )
                        VALUES (
                            new.id,
                            new.text,
                            COALESCE(new.edited_text, ''),
                            COALESCE(new.source_title, ''),
                            new.note
                        );
                    END;

                    CREATE TRIGGER cards_ad AFTER DELETE ON cards BEGIN
                        DELETE FROM cards_fts WHERE card_id = old.id;
                    END;

                    CREATE TRIGGER cards_au
                    AFTER UPDATE OF text, edited_text, source_title, note, record_state
                    ON cards BEGIN
                        DELETE FROM cards_fts WHERE card_id = old.id;
                        INSERT INTO cards_fts(
                            card_id, text, edited_text, source_title, note
                        )
                        SELECT
                            new.id,
                            new.text,
                            COALESCE(new.edited_text, ''),
                            COALESCE(new.source_title, ''),
                            new.note
                        WHERE new.record_state = 'saved';
                    END;
                    """
                )
                # 首次升级旧库时重建一次索引；重复初始化也不会产生重复记录。
                connection.execute("DELETE FROM cards_fts")
                connection.execute(
                    """
                    INSERT INTO cards_fts(
                        card_id, text, edited_text, source_title, note
                    )
                    SELECT
                        id,
                        text,
                        COALESCE(edited_text, ''),
                        COALESCE(source_title, ''),
                        note
                    FROM cards
                    WHERE record_state = 'saved'
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
    def _ensure_edited_text_column(connection: sqlite3.Connection) -> None:
        """为旧库补充可选校对文字；既有正文继续作为未经覆盖的原始证据。"""

        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(cards)").fetchall()
        }
        if "edited_text" not in columns:
            connection.execute(
                "ALTER TABLE cards ADD COLUMN edited_text TEXT"
            )

    @staticmethod
    def _ensure_record_state_column(connection: sqlite3.Connection) -> None:
        """把没有草稿状态的旧库无损迁移为“既有记录均已保存”。"""

        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(cards)").fetchall()
        }
        if "record_state" not in columns:
            connection.execute(
                """
                ALTER TABLE cards ADD COLUMN record_state TEXT NOT NULL
                DEFAULT 'saved' CHECK (record_state IN ('draft', 'saved'))
                """
            )

    @staticmethod
    def _ensure_fts_table(connection: sqlite3.Connection) -> str:
        existing = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'cards_fts'"
        ).fetchone()
        preferred: tuple[str, ...] = FTS_TOKENIZERS
        if existing is not None:
            schema_sql = str(existing["sql"] or "").casefold()
            current = "trigram" if "trigram" in schema_sql else "unicode61"
            if "edited_text" in schema_sql:
                return current
            connection.execute("DROP TABLE cards_fts")
            preferred = (current,) if current == "unicode61" else FTS_TOKENIZERS

        last_error: sqlite3.OperationalError | None = None
        for tokenizer in preferred:
            try:
                connection.execute(
                    f"""
                    CREATE VIRTUAL TABLE cards_fts USING fts5(
                        card_id UNINDEXED,
                        text,
                        edited_text,
                        source_title,
                        note,
                        tokenize = '{tokenizer}'
                    )
                    """
                )
                return tokenizer
            except sqlite3.OperationalError as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

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
    def _serialize_card(card: Card, record_state: str) -> tuple[Any, ...]:
        monitor_json = (
            json.dumps(card.monitor, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            if card.monitor is not None
            else None
        )
        return (
            card.id,
            card.text,
            card.edited_text,
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
            record_state,
        )

    @staticmethod
    def _row_to_card(row: sqlite3.Row) -> Card:
        try:
            monitor = json.loads(row["monitor_json"]) if row["monitor_json"] else None
            return Card(
                id=row["id"],
                text=row["text"],
                edited_text=row["edited_text"],
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

    def _add_card(self, card: Card, *, record_state: str) -> Card:
        if record_state not in RECORD_STATES:
            raise StoreError("卡片内部状态无效。")

        try:
            validated = Card.model_validate(card.model_dump())
        except (AttributeError, ValidationError) as exc:
            raise StoreError("待保存卡片不符合数据契约。") from exc
        try:
            with closing(self._connect()) as connection, connection:
                connection.execute(
                    """
                    INSERT INTO cards(
                        id, text, edited_text, text_source, confidence,
                        screenshot_path, full_screenshot_path,
                        source_url, source_title, video_time, app_name,
                        monitor_json, created_at, stance, note, record_state
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    self._serialize_card(validated, record_state),
                )
        except sqlite3.IntegrityError as exc:
            raise StoreError("卡片 ID 已存在或字段违反数据库约束。") from exc
        except sqlite3.Error as exc:
            raise StoreError("无法保存本地卡片。") from exc
        return validated

    def add_card(self, card: Card) -> Card:
        """验证并新增一张正式卡片；保持既有公开行为不变。"""

        return self._add_card(card, record_state="saved")

    def add_draft(self, card: Card) -> Card:
        """新增一张等待人工审核的草稿，普通读取与检索不会暴露它。"""

        return self._add_card(card, record_state="draft")

    def _get_card_any_state(self, card_id: str) -> tuple[Card, str] | None:
        try:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT * FROM cards WHERE id = ?",
                    (card_id,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise StoreError("无法读取本地卡片。") from exc
        if row is None:
            return None
        state = str(row["record_state"])
        if state not in RECORD_STATES:
            raise StoreError("数据库中的卡片内部状态无效。")
        return self._row_to_card(row), state

    def get_card(self, card_id: str) -> Card | None:
        """按 ID 返回已审核保存的卡片；草稿与不存在均返回 ``None``。"""

        try:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT * FROM cards WHERE id = ? AND record_state = 'saved'",
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
                        edited_text = ?, source_url = ?, source_title = ?, video_time = ?,
                        app_name = ?, monitor_json = ?, stance = ?, note = ?
                    WHERE id = ?
                    """,
                    (
                        validated.edited_text,
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

    def finalize_draft(self, card_id: str, **changes: Any) -> Card:
        """校验修改并把一张草稿原子转为正式卡片，从此进入 FTS 与普通读取。"""

        unknown_fields = set(changes) - UPDATABLE_FIELDS
        if unknown_fields:
            raise StoreError("完成草稿时包含不允许修改的卡片字段。")
        loaded = self._get_card_any_state(card_id)
        if loaded is None:
            raise StoreError("待完成的草稿不存在。")
        current, record_state = loaded
        if record_state != "draft":
            raise StoreError("待完成的记录不是草稿。")

        payload = current.model_dump()
        payload.update(changes)
        try:
            validated = Card.model_validate(payload)
        except ValidationError as exc:
            raise StoreError("完成后的卡片不符合数据契约。") from exc

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
                        edited_text = ?, source_url = ?, source_title = ?, video_time = ?,
                        app_name = ?, monitor_json = ?, stance = ?, note = ?,
                        record_state = 'saved'
                    WHERE id = ? AND record_state = 'draft'
                    """,
                    (
                        validated.edited_text,
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
                    raise StoreError("草稿在完成写入前已不存在或状态已经改变。")
        except sqlite3.Error as exc:
            raise StoreError("无法完成本地草稿。") from exc
        return validated

    def list_recent(self, limit: int = 50, offset: int = 0) -> list[Card]:
        """按创建时间倒序返回最近卡片。"""

        self._validate_page(limit, offset)
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    """
                    SELECT * FROM cards
                    WHERE record_state = 'saved'
                    ORDER BY datetime(created_at) DESC, rowid DESC
                    LIMIT ? OFFSET ?
                    """,
                    (limit, offset),
                ).fetchall()
        except sqlite3.Error as exc:
            raise StoreError("无法列出本地卡片。") from exc
        return [self._row_to_card(row) for row in rows]

    def list_saved_snapshot(self, limit: int = 2_001) -> list[Card]:
        """用单个 SQLite 读语句返回一致快照，供有上限的只读导出。"""

        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 10_000
        ):
            raise StoreError("快照上限需为 1–10000。")
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    """
                    SELECT * FROM cards
                    WHERE record_state = 'saved'
                    ORDER BY datetime(created_at) DESC, rowid DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise StoreError("无法创建本地卡片导出快照。") from exc
        return [self._row_to_card(row) for row in rows]

    def query_cards(
        self,
        query: str = "",
        *,
        stance: Stance | None = None,
        text_source: TextSource | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[Card]:
        """按文字、态度和来源在数据库内筛选，避免先分页再筛选漏项。"""

        self._validate_page(limit, offset)
        if not isinstance(query, str):
            raise StoreError("搜索词必须是字符串。")
        if stance is not None and stance not in (
            "unknown",
            "agree",
            "disagree",
            "doubt",
            "useful",
        ):
            raise StoreError("态度筛选值无效。")
        if text_source is not None and text_source not in ("dom", "ocr"):
            raise StoreError("文字来源筛选值无效。")

        normalized = " ".join(query.split())
        if "\x00" in normalized:
            raise StoreError("搜索词包含无效字符。")
        if len(normalized) > MAX_QUERY_CHARACTERS:
            raise StoreError("搜索词不能超过 500 个字符。")
        clauses = ["record_state = 'saved'"]
        parameters: list[object] = []
        if stance is not None:
            clauses.append("stance = ?")
            parameters.append(stance)
        if text_source is not None:
            clauses.append("text_source = ?")
            parameters.append(text_source)
        if normalized:
            clauses.append(
                "("
                "instr(lower(text), lower(?)) > 0 OR "
                "instr(lower(COALESCE(edited_text, '')), lower(?)) > 0 OR "
                "instr(lower(COALESCE(source_title, '')), lower(?)) > 0 OR "
                "instr(lower(note), lower(?)) > 0"
                ")"
            )
            parameters.extend([normalized] * 4)
        parameters.extend([limit, offset])

        statement = f"""
            SELECT * FROM cards
            WHERE {' AND '.join(clauses)}
            ORDER BY datetime(created_at) DESC, rowid DESC
            LIMIT ? OFFSET ?
        """
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(statement, parameters).fetchall()
        except sqlite3.Error as exc:
            raise StoreError("无法筛选本地卡片。") from exc
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
        if len(normalized) > MAX_QUERY_CHARACTERS:
            raise StoreError("搜索词不能超过 500 个字符。")

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
                              AND cards.record_state = 'saved'
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
            WHERE record_state = 'saved'
              AND (
                   instr(text, ?) > 0
                OR instr(COALESCE(edited_text, ''), ?) > 0
                OR instr(COALESCE(source_title, ''), ?) > 0
                OR instr(note, ?) > 0
              )
            ORDER BY datetime(created_at) DESC, rowid DESC
            LIMIT ?
            """,
            (query, query, query, query, limit),
        ).fetchall()

    def _safe_screenshot_path(self, relative_path: str) -> Path:
        try:
            screenshot_root = self.screenshot_dir.resolve()
            candidate = (self.data_dir / relative_path).resolve()
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise StoreError("无法安全解析卡片截图路径，已拒绝删除。") from exc
        if (
            screenshot_root == self.data_dir
            or self.data_dir not in screenshot_root.parents
        ):
            raise StoreError("截图目录超出本地数据目录，已拒绝文件操作。")
        if candidate == screenshot_root or screenshot_root not in candidate.parents:
            raise StoreError("卡片截图路径超出受控截图目录，已拒绝删除。")
        if candidate == self.db_path:
            raise StoreError("卡片截图路径指向本地数据库，已拒绝删除。")
        return candidate

    def _assert_no_cross_references(
        self,
        connection: sqlite3.Connection,
        card_id: str,
        paths: set[Path],
    ) -> None:
        rows = connection.execute(
            """
            SELECT screenshot_path, full_screenshot_path
            FROM cards WHERE id <> ?
            """,
            (card_id,),
        ).fetchall()
        for row in rows:
            for field in ("screenshot_path", "full_screenshot_path"):
                try:
                    referenced = self._safe_screenshot_path(str(row[field]))
                except StoreError:
                    # 其他记录的越界路径不是当前受控目标；它会在自身删除时被拒绝。
                    continue
                if referenced in paths:
                    raise StoreError("卡片截图仍被其他记录引用，已拒绝删除。")

    def _delete_card(
        self,
        card_id: str,
        *,
        required_state: str | None = None,
    ) -> bool:
        """在一个写事务内按可选状态删除记录及两张截图。由于先取得
        ``BEGIN IMMEDIATE``，状态检查与删除之间不会被另一写入者改成正式卡片。

        为优先保护隐私，先清理经路径边界验证的截图，再提交数据库删除。若极少见
        的数据库写入失败，可能留下指向已删除截图的卡片，但不会留下未受控副本。
        """

        if required_state is not None and required_state not in RECORD_STATES:
            raise StoreError("待删除卡片状态不合法。")

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if required_state is None:
                row = connection.execute(
                    "SELECT * FROM cards WHERE id = ?",
                    (card_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM cards WHERE id = ? AND record_state = ?",
                    (card_id, required_state),
                ).fetchone()
            if row is None:
                connection.rollback()
                return False
            card = self._row_to_card(row)
            paths = {
                self._safe_screenshot_path(card.screenshot_path),
                self._safe_screenshot_path(card.full_screenshot_path),
            }
            self._assert_no_cross_references(connection, card_id, paths)
            for path in paths:
                if path.is_dir():
                    raise StoreError("卡片截图路径指向目录，已拒绝删除。")
            for path in paths:
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    raise StoreError(
                        "无法删除本地卡片截图；数据库记录仍被保留。"
                    ) from exc

            if required_state is None:
                cursor = connection.execute(
                    "DELETE FROM cards WHERE id = ?",
                    (card_id,),
                )
            else:
                cursor = connection.execute(
                    "DELETE FROM cards WHERE id = ? AND record_state = ?",
                    (card_id, required_state),
                )
            if cursor.rowcount != 1:
                raise StoreError("待删除卡片在写入前已不存在。")
            connection.commit()
            return True
        except StoreError:
            connection.rollback()
            raise
        except sqlite3.Error as exc:
            connection.rollback()
            raise StoreError("截图已清理，但无法删除本地数据库记录。") from exc
        finally:
            connection.close()

    def delete_card(self, card_id: str) -> bool:
        """删除正式卡片或草稿、FTS 记录和两张截图；不存在时返回 ``False``。"""

        return self._delete_card(card_id)

    def delete_draft(self, card_id: str) -> bool:
        """只删除仍处于草稿状态的记录和截图，绝不删除正式卡片。"""

        return self._delete_card(card_id, required_state="draft")

    def cleanup_drafts(self) -> int:
        """清理上次异常退出遗留的全部草稿及截图，并返回删除数量。

        主程序应在接受新的抓取请求之前调用。任何一张草稿清理失败都会抛出异常，
        让启动流程能够明确提示，而不是把遗留候选悄悄当成正式资料。
        """

        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    """
                    SELECT * FROM cards
                    WHERE record_state = 'draft' ORDER BY rowid
                    """
                ).fetchall()
                for row in rows:
                    card = self._row_to_card(row)
                    card_id = card.id
                    expected_selected = f"screenshots/{card_id}.png"
                    expected_full = f"screenshots/full_{card_id}.png"
                    if (
                        card.screenshot_path != expected_selected
                        or card.full_screenshot_path != expected_full
                    ):
                        raise StoreError(
                            "候选草稿截图文件名不符合安全清理规范，已停止启动清理。"
                        )
                    paths = {
                        self._safe_screenshot_path(expected_selected),
                        self._safe_screenshot_path(expected_full),
                    }
                    self._assert_no_cross_references(connection, card_id, paths)
        except sqlite3.Error as exc:
            raise StoreError("无法读取待清理的候选草稿。") from exc

        deleted = 0
        for row in rows:
            if self.delete_draft(str(row["id"])):
                deleted += 1
        return deleted
