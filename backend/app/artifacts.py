"""内容寻址的工件存储。

背景：LangGraph 会把每个检查点的整份状态序列化一次，而会话里的历史消息、消息总线和
证据目录都是大对象。把这些对象按内容存到图外、状态里只留引用，可以避免同一份数据
在每份快照里重复一份；快照本身仍然全部保留（删除快照会破坏中断恢复所需的写入）。

约定：
- 键是内容的 sha256，内容只写一次；
- 引用是 ``{"artifact_id": str, "kind": str, "bytes": int}``；
- 读不到时报错，绝不静默返回空对象，否则恢复会退化成“丢失协作历史”。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .paths import DATA_DIR

DEFAULT_ARTIFACT_DIR = DATA_DIR / "artifacts"


class ArtifactMissing(RuntimeError):
    """引用存在但工件文件已被清理（或引用本身非法）。"""


_HEX = set("0123456789abcdef")


def _valid_id(artifact_id: object) -> bool:
    """工件 id 必须是 64 位 sha256 十六进制：路径由它直接拼出来，不能接受任意字符串。"""
    return (
        isinstance(artifact_id, str)
        and len(artifact_id) == 64
        and all(char in _HEX for char in artifact_id)
    )


class ArtifactStore:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else DEFAULT_ARTIFACT_DIR

    def _path(self, artifact_id: str) -> Path:
        return self.root / artifact_id[:2] / f"{artifact_id}.json"

    def put(self, payload) -> dict:
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        data = text.encode("utf-8")
        artifact_id = hashlib.sha256(data).hexdigest()
        path = self._path(artifact_id)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f"{path.name}.{artifact_id[:8]}.tmp")
            try:
                tmp.write_bytes(data)
                tmp.replace(path)
            finally:
                tmp.unlink(missing_ok=True)
        return {"artifact_id": artifact_id, "bytes": len(data)}

    def get(self, ref: dict):
        artifact_id = (ref or {}).get("artifact_id")
        if not artifact_id:
            raise ArtifactMissing("工件引用缺少 artifact_id")
        if not _valid_id(artifact_id):
            raise ArtifactMissing(f"工件引用格式非法：{str(artifact_id)[:40]}")
        path = self._path(artifact_id)
        if not path.is_file():
            raise ArtifactMissing(
                f"工件 {artifact_id[:12]} 不存在（可能已被清理）；不能在不丢失协作历史的情况下恢复"
            )
        return json.loads(path.read_bytes().decode("utf-8"))

    # --- retention ------------------------------------------------------

    def iter_files(self):
        """遍历磁盘上的工件文件，返回 (artifact_id, path)。不跟随目录外路径。"""
        if not self.root.is_dir():
            return
        for path in self.root.glob("*/*.json"):
            artifact_id = path.stem
            if len(artifact_id) == 64 and all(c in "0123456789abcdef" for c in artifact_id):
                yield artifact_id, path

    def collect_garbage(self, referenced: set[str], *, min_age_seconds: float) -> dict:
        """删除“未被引用且已超过保护期”的工件。

        - 只处理工件根目录下符合哈希命名的文件，路径由 artifact_id 重新推导；
        - 保护期内的文件一律保留，避免删掉尚未写入检查点的在途会话；
        - 返回统计，便于日志与验收。
        """
        import time

        removed = 0
        freed = 0
        kept_fresh = 0
        kept_used = 0
        cutoff = time.time() - max(0.0, min_age_seconds)
        for artifact_id, path in list(self.iter_files()):
            if artifact_id in referenced:
                kept_used += 1
                continue
            try:
                if path.stat().st_mtime > cutoff:
                    kept_fresh += 1
                    continue
                size = path.stat().st_size
                path.unlink()
            except FileNotFoundError:
                continue
            removed += 1
            freed += size
        return {
            "removed": removed,
            "freed_bytes": freed,
            "kept_referenced": kept_used,
            "kept_fresh": kept_fresh,
        }


def referenced_ids(obj, found: set[str] | None = None) -> set[str]:
    """递归收集任意会话结构中的工件引用 ID。"""
    found = set() if found is None else found
    if isinstance(obj, dict):
        artifact_id = obj.get("artifact_id")
        if isinstance(artifact_id, str) and len(artifact_id) == 64:
            found.add(artifact_id)
        for value in obj.values():
            referenced_ids(value, found)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            referenced_ids(value, found)
    return found


_HEX64 = __import__("re").compile(rb"[0-9a-f]{64}")


def scan_checkpoint_references(paths) -> set[str]:
    """从检查点文件字节里收集被引用的工件 ID。

    LangGraph 把状态序列化成 msgpack 再放进 pickle，直接反序列化需要注册全部自定义类型；
    这里改为扫描原始字节：工件 ID 会以字面量出现在序列化结果中，扫描不会漏，也不会误删。
    """
    found: set[str] = set()
    for path in paths:
        path = Path(path)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            continue
        for match in _HEX64.findall(data):
            found.add(match.decode("ascii"))
    return found


def checkpoint_files(root: Path) -> list[Path]:
    """列出检查点分片与旧的单文件检查点。"""
    root = Path(root)
    files: list[Path] = []
    legacy = root / "checkpoints.pkl"
    if legacy.is_file():
        files.append(legacy)
    shard_dir = root / "checkpoints.pkl.d"
    if shard_dir.is_dir():
        files.extend(sorted(shard_dir.glob("*.pkl")))
    return files


# 默认实例按需创建，测试可以用自己的目录覆盖。
_default_store: ArtifactStore | None = None


def default_store() -> ArtifactStore:
    global _default_store
    if _default_store is None:
        _default_store = ArtifactStore()
    return _default_store
