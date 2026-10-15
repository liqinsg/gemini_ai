import uuid
import json
import sqlite3
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# 项目根目录：utils/ → 上一级
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_QUEUE_PATH = PROJECT_ROOT / "event_queue.db"

BOT_ID = "gemini_ai"
STRATEGY_VERSION = "v3.2.0"
SOURCE_HOST = socket.gethostname()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_event_id() -> str:
    return str(uuid.uuid4())


class EventQueue:
    """本地持久队列 — 网络/DB 故障不丢事件，不阻塞主交易"""

    def __init__(self, db_path: Path = DB_QUEUE_PATH):
        self.conn = sqlite3.connect(str(db_path), timeout=30)
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS event_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT UNIQUE NOT NULL,
                event_type TEXT NOT NULL,
                account_id TEXT,
                bot_id TEXT,
                instrument TEXT,
                status TEXT,
                source_host TEXT,
                tag TEXT,
                comment TEXT,
                trade_id TEXT,
                event_time TEXT NOT NULL,
                strategy_version TEXT,
                payload_json TEXT NOT NULL,
                uploaded BOOLEAN DEFAULT FALSE,
                upload_attempts INTEGER DEFAULT 0,
                last_error TEXT
            )
        """
        )
        self.conn.commit()

    def enqueue(self, event: dict[str, Any]) -> str:
        """入队即返回，不等待上传"""
        event_id = event["event_id"]
        try:
            self.conn.execute(
                """
                INSERT INTO event_queue (
                    event_id, event_type, account_id, bot_id, instrument,
                    status, source_host, tag, comment, trade_id,
                    event_time, strategy_version, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    event_id,
                    event["event_type"],
                    event.get("account_id"),
                    event.get("bot_id", BOT_ID),
                    event.get("instrument"),
                    event.get("status"),
                    event.get("source_host", SOURCE_HOST),
                    event.get("tag"),
                    event.get("comment"),
                    event.get("trade_id"),
                    event["event_time"],
                    event.get("strategy_version", STRATEGY_VERSION),
                    json.dumps(event.get("payload", {}), ensure_ascii=False),
                ),
            )
            self.conn.commit()
        except sqlite3.IntegrityError:
            pass  # 幂等：重复直接跳过
        return event_id

    def get_pending(self, limit: int = 50) -> list[tuple[Any, ...]]:
        """取出待上传批次"""
        return self.conn.execute(
            """
            SELECT id, event_id, event_type, account_id, bot_id, instrument,
                   status, source_host, tag, comment, trade_id,
                   event_time, strategy_version, payload_json, upload_attempts
            FROM event_queue
            WHERE uploaded = FALSE AND upload_attempts < 5
            ORDER BY id ASC LIMIT ?
        """,
            (limit,),
        ).fetchall()

    def mark_uploaded(self, event_ids: list[str]) -> None:
        """标记成功"""
        if not event_ids:
            return
        placeholders = ",".join("?" for _ in event_ids)
        self.conn.execute(
            f"UPDATE event_queue SET uploaded = TRUE WHERE event_id IN ({placeholders})",
            event_ids,
        )
        self.conn.commit()

    def mark_failed(self, queue_ids: list[int], error: str) -> None:
        """标记失败+重试计数"""
        if not queue_ids:
            return
        placeholders = ",".join("?" for _ in queue_ids)
        self.conn.execute(
            f"""
            UPDATE event_queue
            SET upload_attempts = upload_attempts + 1, last_error = ?
            WHERE id IN ({placeholders})
            """,
            [error[:500]] + queue_ids,
        )
        self.conn.commit()


_queue: Optional[EventQueue] = None


def get_queue() -> EventQueue:
    global _queue
    if _queue is None:
        _queue = EventQueue()
    return _queue


def emit(event_type: str, **kwargs: Any) -> str:
    """统一事件入口 — 主流程唯一调用点"""
    event = {
        "event_id": new_event_id(),
        "event_type": event_type,
        # 不传也绝不会为 NULL
        "account_id": kwargs.pop("account_id", "UNKNOWN_ACCOUNT"),
        "bot_id": kwargs.pop("bot_id", BOT_ID),
        "event_time": kwargs.pop("event_time", utc_now_iso()),
        **kwargs,
    }
    return get_queue().enqueue(event)
