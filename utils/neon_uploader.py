import os
import time
import json
import logging
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path
from typing import Any, List, Tuple
from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import execute_values, Json

# 统一用绝对导入
from utils.event_collector import EventQueue

# 加载项目根目录 .env
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("neon_uploader")

NEON_DSN = os.getenv("NEON_DATABASE_URL")
TRANSPORT = os.getenv("NEON_TRANSPORT", "pg").lower()  # "pg" (默认) | "http"

BATCH_SIZE = 50
UPLOAD_INTERVAL = 15  # 秒
MAX_RETRIES = 5

def _http_sql(query: str, params: List[Any]) -> dict:
    host = urllib.parse.urlparse(NEON_DSN).hostname
    req = urllib.request.Request(
        f"https://{host}/sql",
        data=json.dumps({"query": query, "params": params}, default=str).encode(),
        headers={
            "Neon-Connection-String": NEON_DSN,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Neon HTTP {e.code}: {e.read().decode()[:300]}") from None

def _insert_http(values: List[Tuple[Any, ...]]) -> None:
    n = 13
    rows_sql: List[str] = []
    params: List[Any] = []
    for i, v in enumerate(values):
        b = i * n
        ph = ",".join(f"${b + j + 1}" for j in range(n - 1))
        rows_sql.append(f"({ph}, ${b + n}::jsonb)")
        params.extend(v[:-1])
        params.append(json.dumps(v[-1]))
    _http_sql(
        "INSERT INTO trade_events ("
        "event_id, event_type, account_id, bot_id, instrument, "
        "status, source_host, tag, comment, trade_id, event_time, "
        "strategy_version, payload"
        ") VALUES " + ",".join(rows_sql) +
        " ON CONFLICT (event_id) DO NOTHING",
        params,
    )

def connect_neon() -> psycopg2.extensions.connection:
    if not NEON_DSN:
        raise RuntimeError("NEON_DATABASE_URL not set in .env")
    return psycopg2.connect(NEON_DSN, connect_timeout=10)

def upload_batch(rows: List[Tuple[Any, ...]]) -> List[str]:
    if not rows:
        return []

    # SQLite 返回列顺序：
    # 0=id,1=event_id,2=event_type,3=account_id,4=bot_id,5=instrument,
    # 6=status,7=source_host,8=tag,9=comment,10=trade_id,
    # 11=event_time,12=strategy_version,13=payload_json,14=upload_attempts
    values: List[Tuple[Any, ...]] = []
    for r in rows:
        try:
            payload = json.loads(r[13]) if r[13] else {}
        except (json.JSONDecodeError, TypeError):
            payload = {"parse_error": "invalid_json"}

        values.append((
            r[1],   # event_id
            r[2],   # event_type
            r[3],   # account_id
            r[4],   # bot_id
            r[5],   # instrument
            r[6],   # status
            r[7],   # source_host
            r[8],   # tag
            r[9],   # comment
            r[10],  # trade_id
            r[11],  # event_time
            r[12],  # strategy_version
            payload,
        ))

    if TRANSPORT == "http":
        _insert_http(values)
    else:
        conn = connect_neon()
        try:
            cur = conn.cursor()
            pg_values = [v[:-1] + (Json(v[-1]),) for v in values]
            execute_values(
                cur,
                """
                INSERT INTO trade_events (
                    event_id, event_type, account_id, bot_id, instrument,
                    status, source_host, tag, comment, trade_id,
                    event_time, strategy_version, payload
                ) VALUES %s
                ON CONFLICT (event_id) DO NOTHING
                """,
                pg_values,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    logger.info(f"Uploaded {len(values)} events via {TRANSPORT}")
    return [r[1] for r in rows]

def main() -> None:
    if not NEON_DSN:
        raise SystemExit("NEON_DATABASE_URL not set in .env")
    q = EventQueue()
    logger.info(f"Neon uploader started (transport={TRANSPORT})")
    while True:
        rows = q.get_pending(limit=BATCH_SIZE)
        if not rows:
            time.sleep(UPLOAD_INTERVAL)
            continue
        queue_ids = [r[0] for r in rows]
        try:
            uploaded_ids = upload_batch(rows)
            q.mark_uploaded(uploaded_ids)
        except Exception as exc:
            logger.error(f"Upload failed: {exc}")
            q.mark_failed(queue_ids, str(exc))
            time.sleep(30)  # 失败冷却

if __name__ == "__main__":
    main()