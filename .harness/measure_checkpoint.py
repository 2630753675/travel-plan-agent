"""Measure which session parts dominate a checkpoint shard (no model calls)."""
from __future__ import annotations

import asyncio
import json
import pathlib
import pickle
import sys

ROOT = pathlib.Path(r"E:\项目\agent_study\dsh_work2\backend")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app.agents import hub  # noqa: E402
from app.tools.mock import MockSource  # noqa: E402
from verify_peer_success import ScriptedTeam, brief  # noqa: E402


def size(obj) -> int:
    return len(pickle.dumps(obj, protocol=4))


async def main():
    result = await hub.run_session(brief=brief(), source=MockSource(), client=ScriptedTeam(),
                                   emit=lambda *_a, **_k: None, max_turns=20, max_rounds=2)
    session = result.session
    total = size(session)
    print(f"session total {total/1024:.1f} KB")
    rows = []
    for key, value in session.items():
        rows.append((size(value), f"session.{key}"))
    for key, value in (session.get("context") or {}).items():
        rows.append((size(value), f"context.{key}"))
    rows.sort(reverse=True)
    for bytes_, name in rows:
        if bytes_ < 512:
            continue
        print(f"{bytes_/1024:9.1f} KB  {bytes_*100/total:5.1f}%  {name}")

    ctx = result.ctx
    if ctx.operations:
        sample = next(iter(ctx.operations.values()))
        print("\noperations sample:", json.dumps(
            {k: (v if not isinstance(v, (dict, list)) else f"<{type(v).__name__} len={len(v)}>")
             for k, v in sample.items()}, ensure_ascii=False))
    if ctx.evidence:
        eid, item = next(iter(ctx.evidence.items()))
        print("evidence sample keys:", sorted(item), "result KB",
              round(size(item.get("result")) / 1024, 1))

    hist = sum(size(h) for h in (session.get("histories") or {}).values())
    print(f"\nhistories total {hist/1024:.1f} KB (per role: "
          + ", ".join(f"{k}={size(v)/1024:.1f}" for k, v in (session.get("histories") or {}).items()) + ")")


asyncio.run(main())
