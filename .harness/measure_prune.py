"""Compare shard size for several KEEP_CHECKPOINTS values (copy of real shard)."""
import pathlib
import shutil
import sys
import tempfile

sys.path.insert(0, r"E:\项目\agent_study\dsh_work2\backend")
import app.checkpoint as cp  # noqa: E402

DATA = pathlib.Path(r"E:\项目\agent_study\dsh_work2\backend\data\checkpoints.pkl.d")
src = next(p for p in DATA.glob("*.pkl") if p.stat().st_size > 6_000_000)
original_mb = src.stat().st_size / 1048576
print(f"source {original_mb:.2f} MB")

for keep in (1, 2, 3, 5, 8):
    cp.PersistentSaver.KEEP_CHECKPOINTS = keep
    tmp = pathlib.Path(tempfile.mkdtemp()) / "cp.pkl"
    (tmp.parent / "cp.pkl.d").mkdir()
    shutil.copy2(src, tmp.parent / "cp.pkl.d" / src.name)
    saver = cp.PersistentSaver(tmp)
    thread = next(iter(saver.storage))
    saver.put(
        {"configurable": {"thread_id": thread, "checkpoint_ns": "", "checkpoint_id": "zzz"}},
        {"id": "zzz", "channel_values": {}, "channel_versions": {}, "versions_seen": {}},
        {"step": 99},
        {},
    )
    out = tmp.parent / "cp.pkl.d" / src.name
    kept = len(saver.storage[thread][""])
    print(f"KEEP={keep}: snapshots={kept} shard={out.stat().st_size / 1048576:.2f} MB")
