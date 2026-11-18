"""工件库清理：默认只预览，加 --apply 才真正删除。

规则：只删除“未被任何检查点引用、且超过保护期”的工件。工件是内容寻址、被多份
会话共享的，所以不能按时间直接删；保护期用于避开尚未落盘的在途会话。

    .venv/Scripts/python.exe scripts/collect_artifacts.py                 # 预览
    .venv/Scripts/python.exe scripts/collect_artifacts.py --apply         # 执行
"""

from __future__ import annotations

import argparse
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.artifacts import ArtifactStore, checkpoint_files, scan_checkpoint_references  # noqa: E402
from app.paths import DATA_DIR  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", default=str(pathlib.Path(DATA_DIR) / "artifacts"))
    parser.add_argument("--checkpoints", default=str(DATA_DIR))
    parser.add_argument("--min-age-hours", type=float, default=24.0)
    parser.add_argument("--apply", action="store_true", help="真正删除；默认只预览")
    args = parser.parse_args()

    store = ArtifactStore(pathlib.Path(args.artifacts))
    refs = scan_checkpoint_references(checkpoint_files(pathlib.Path(args.checkpoints)))
    files = list(store.iter_files())
    total = sum(path.stat().st_size for _, path in files)
    print(f"工件目录：{store.root}")
    print(f"检查点引用：{len(refs)} 个；磁盘工件：{len(files)} 个，{total / 1024:.0f} KB")

    if not args.apply:
        import time

        cutoff = time.time() - max(0.0, args.min_age_hours * 3600)
        stale = [(aid, p) for aid, p in files if aid not in refs and p.stat().st_mtime <= cutoff]
        print(f"预览模式：可删除 {len(stale)} 个，释放 {sum(p.stat().st_size for _, p in stale) / 1024:.0f} KB")
        print("加 --apply 才会真正删除。")
        return 0

    stats = store.collect_garbage(refs, min_age_seconds=args.min_age_hours * 3600)
    print(f"已删除 {stats['removed']} 个，释放 {stats['freed_bytes'] / 1024:.0f} KB；"
          f"保留被引用 {stats['kept_referenced']} 个、保护期内 {stats['kept_fresh']} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
