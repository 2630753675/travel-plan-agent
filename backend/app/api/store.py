"""规划结果持久化（SQLite）。

只存最终结果与过程轨迹，不存任何模型凭据。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
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
    negotiation TEXT
);
"""


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(_SCHEMA)
    # 兼容早期版本的库
    cols = {row[1] for row in conn.execute("PRAGMA table_info(plans)")}
    if "revision" not in cols:
        conn.execute("ALTER TABLE plans ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")
    if "negotiation" not in cols:
        conn.execute("ALTER TABLE plans ADD COLUMN negotiation TEXT")
    return conn


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def save(
    plan_id: str,
    brief: dict,
    itinerary: dict,
    critique: dict,
    trace: list[dict],
    revision: int = 0,
    negotiation: dict | None = None,
) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO plans "
            "(id, created_at, brief, itinerary, critique, trace, revision, negotiation) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                plan_id,
                datetime.now(timezone.utc).isoformat(),
                json.dumps(brief, ensure_ascii=False),
                json.dumps(itinerary, ensure_ascii=False),
                json.dumps(critique, ensure_ascii=False),
                json.dumps(trace, ensure_ascii=False),
                revision,
                json.dumps(negotiation, ensure_ascii=False) if negotiation else None,
            ),
        )


def load(plan_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, created_at, brief, itinerary, critique, trace, revision, negotiation "
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
    }
