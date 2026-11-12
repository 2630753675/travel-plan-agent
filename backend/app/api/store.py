"""规划结果持久化（SQLite）。

只存最终结果与过程轨迹，不存任何模型凭据。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone

from ..paths import DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    id          TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    brief       TEXT NOT NULL,
    itinerary   TEXT NOT NULL,
    critique    TEXT NOT NULL,
    trace       TEXT NOT NULL,
    revision    INTEGER NOT NULL DEFAULT 0,
    negotiation TEXT,
    activity    TEXT
);
CREATE TABLE IF NOT EXISTS run_events (
    run_id      TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    event_id    TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL,
    payload     TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);
"""


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(_SCHEMA)
    # 兼容早期版本的库
    cols = {row[1] for row in conn.execute("PRAGMA table_info(plans)")}
    if "revision" not in cols:
        conn.execute("ALTER TABLE plans ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")
    if "negotiation" not in cols:
        conn.execute("ALTER TABLE plans ADD COLUMN negotiation TEXT")
    if "activity" not in cols:
        conn.execute("ALTER TABLE plans ADD COLUMN activity TEXT")
    return conn


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def next_event_seq(run_id: str) -> int:
    # 注意：sqlite3 的连接上下文只提交事务、不关闭连接；
    # 用 closing 保证释放句柄，否则 Windows 上会锁住 db 文件。
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM run_events WHERE run_id = ?", (run_id,)
        ).fetchone()
    return int(row[0])


def append_event(event: dict) -> None:
    with closing(_connect()) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO run_events (run_id, seq, event_id, occurred_at, payload) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                event["run_id"],
                event["seq"],
                event["event_id"],
                event["occurred_at"],
                json.dumps(event, ensure_ascii=False),
            ),
        )
        conn.commit()


def list_events(run_id: str, after_seq: int = 0) -> list[dict]:
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT payload FROM run_events WHERE run_id = ? AND seq > ? ORDER BY seq",
            (run_id, after_seq),
        ).fetchall()
    return [json.loads(row[0]) for row in rows]


def save(
    plan_id: str,
    brief: dict,
    itinerary: dict,
    critique: dict,
    trace: list[dict],
    revision: int = 0,
    negotiation: dict | None = None,
    activity: list[dict] | None = None,
) -> None:
    with closing(_connect()) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO plans "
            "(id, created_at, brief, itinerary, critique, trace, revision, negotiation, activity) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                plan_id,
                datetime.now(timezone.utc).isoformat(),
                json.dumps(brief, ensure_ascii=False),
                json.dumps(itinerary, ensure_ascii=False),
                json.dumps(critique, ensure_ascii=False),
                json.dumps(trace, ensure_ascii=False),
                revision,
                json.dumps(negotiation, ensure_ascii=False) if negotiation else None,
                json.dumps(activity, ensure_ascii=False) if activity else None,
            ),
        )
        conn.commit()


def load(plan_id: str) -> dict | None:
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT id, created_at, brief, itinerary, critique, trace, revision, negotiation, activity "
            "FROM plans WHERE id = ?",
            (plan_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "created_at": row[1],
        "brief": json.loads(row[2]),
        "itinerary": json.loads(row[3]),
        "critique": json.loads(row[4]),
        "trace": json.loads(row[5]),
        "revision": row[6],
        "negotiation": json.loads(row[7]) if row[7] else None,
        "activity": json.loads(row[8]) if row[8] else [],
    }
