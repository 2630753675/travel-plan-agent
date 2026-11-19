"""工件保留策略回归：引用扫描、保护期、预览与执行。

不调用真实模型/网络。
运行：
    .venv/Scripts/python.exe scripts/verify_artifacts_gc.py
"""

from __future__ import annotations

import os
import pathlib
import pickle
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.artifacts import (  # noqa: E402
    ArtifactStore,
    checkpoint_files,
    scan_checkpoint_references,
)

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'} {name}{(': ' + detail) if detail and not cond else ''}")
    if not cond:
        failures.append(name)


def main() -> int:
    tmp = pathlib.Path(tempfile.mkdtemp())
    store = ArtifactStore(tmp / "artifacts")

    used = store.put({"histories": {"a": [{"content": "in use"}]}})
    stale = store.put({"histories": {"b": [{"content": "stale"}]}})
    fresh = store.put({"histories": {"c": [{"content": "fresh"}]}})

    # 只有 used 出现在检查点里；stale 与 fresh 都无人引用。
    shard = tmp / "checkpoints.pkl.d" / "deadbeef.pkl"
    shard.parent.mkdir(parents=True)
    shard.write_bytes(pickle.dumps({"session": {"histories": used}}))

    refs = scan_checkpoint_references(checkpoint_files(tmp))
    check("从检查点字节中扫描到引用", used["artifact_id"] in refs, str(len(refs)))
    check("未引用工件不在引用集", stale["artifact_id"] not in refs and fresh["artifact_id"] not in refs)

    # stale 设为 2 天前；fresh 保持刚刚创建。
    old = time.time() - 2 * 86400
    stale_path = next(p for aid, p in store.iter_files() if aid == stale["artifact_id"])
    os.utime(stale_path, (old, old))

    stats = store.collect_garbage(refs, min_age_seconds=86400)
    check("删除未被引用且过期的工件", stats["removed"] == 1 and stats["freed_bytes"] > 0, str(stats))
    check("被引用工件保留", used["artifact_id"] in {aid for aid, _ in store.iter_files()})
    check("保护期内工件保留", fresh["artifact_id"] in {aid for aid, _ in store.iter_files()})
    check("被删工件确实不在磁盘", not stale_path.exists())

    # 保护期设为 0 时，只保留仍被引用的那个；fresh 从未被引用，应被清掉。
    stats2 = store.collect_garbage({used["artifact_id"]}, min_age_seconds=0)
    check("过期后清理其余未被引用工件", stats2["removed"] >= 1, str(stats2))
    check("仍被引用的工件不被删除", used["artifact_id"] in {aid for aid, _ in store.iter_files()})
    try:
        store.get(used)
        check("仍在引用集中的工件可读取", True)
    except Exception as exc:  # noqa: BLE001
        check("仍在引用集中的工件可读取", False, str(exc))

    # 引用里的 id 会被直接拼进路径，必须只接受 64 位 sha256。
    secret = tmp / "artifacts" / "secret.json"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text('{"leaked": true}', encoding="utf-8")
    for bad in ("a/../../secret", "../secret", "0" * 63, "g" * 64):
        try:
            store.get({"artifact_id": bad})
            check(f"非法工件 id 被拒绝：{bad[:16]}", False, "读取成功")
        except Exception:  # noqa: BLE001
            check(f"非法工件 id 被拒绝：{bad[:16]}", True)
    check("合法 id 仍可读取", store.get(used) is not None)

    print(f"\n{'PASS' if not failures else 'FAIL'} 工件保留策略回归{'通过' if not failures else '失败'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
