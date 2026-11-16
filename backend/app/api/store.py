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
CREATE TABLE IF NOT EXISTS plan_versions (
    plan_id     TEXT NOT NULL,
    version     INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    source      TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    itinerary   TEXT NOT NULL,
    critique    TEXT NOT NULL,
    negotiation TEXT,
    PRIMARY KEY (plan_id, version)
);
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    status      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    payload     TEXT NOT NULL
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
    *,
    source: str = "initial",
    note: str = "",
) -> int:
    """写入当前结果，并追加一个版本快照；返回新版本号。

    版本是只追加的：重排、降配、回滚各自留一份，允许回看和回退。
    """
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
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM plan_versions WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
        version = int(row[0])
        conn.execute(
            "INSERT INTO plan_versions "
            "(plan_id, version, created_at, source, note, itinerary, critique, negotiation) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                plan_id,
                version,
                datetime.now(timezone.utc).isoformat(),
                source,
                note,
                json.dumps(itinerary, ensure_ascii=False),
                json.dumps(critique, ensure_ascii=False),
                json.dumps(negotiation, ensure_ascii=False) if negotiation else None,
            ),
        )
        conn.commit()
    return version


def list_versions(plan_id: str) -> list[dict]:
    """版本列表只给摘要，避免把整份行程塞进列表响应。"""
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT version, created_at, source, note, itinerary, critique "
            "FROM plan_versions WHERE plan_id = ? ORDER BY version",
            (plan_id,),
        ).fetchall()
    out = []
    for version, created_at, source, note, itinerary, critique in rows:
        it = json.loads(itinerary)
        crit = json.loads(critique)
        out.append(
            {
                "version": version,
                "created_at": created_at,
                "source": source,
                "note": note,
                "total_cost": it.get("total_cost", 0),
                "day_count": len(it.get("days") or []),
                "passed": crit.get("passed", False),
                "blockers": len(
                    [i for i in (crit.get("issues") or []) if i.get("severity") == "blocker"]
                ),
            }
        )
    return out


def load_version(plan_id: str, version: int) -> dict | None:
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT version, created_at, source, note, itinerary, critique, negotiation "
            "FROM plan_versions WHERE plan_id = ? AND version = ?",
            (plan_id, version),
        ).fetchone()
    if row is None:
        return None
    return {
        "version": row[0],
        "created_at": row[1],
        "source": row[2],
        "note": row[3],
        "itinerary": json.loads(row[4]),
        "critique": json.loads(row[5]),
        "negotiation": json.loads(row[6]) if row[6] else None,
    }


def save_run(run_id: str, status: str, payload: dict) -> None:
    """保存失败/中断的运行摘要，让页面刷新后仍能看到原因或继续确认。"""
    with closing(_connect()) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO runs (run_id, status, created_at, payload) VALUES (?, ?, ?, ?)",
            (run_id, status, datetime.now(timezone.utc).isoformat(), json.dumps(payload, ensure_ascii=False)),
        )
        conn.commit()


def load_run(run_id: str) -> dict | None:
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT run_id, status, created_at, payload FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "run_id": row[0],
        "status": row[1],
        "created_at": row[2],
        "payload": json.loads(row[3]),
    }


def current_version(plan_id: str) -> int:
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) FROM plan_versions WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
    return int(row[0])


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
