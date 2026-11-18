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
    """引用存在但工件文件已被清理。"""


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
        if not isinstance(artifact_id, str) or not artifact_id:
            raise ArtifactMissing("工件引用缺少 artifact_id")
        path = self._path(artifact_id)
        if not path.is_file():
            raise ArtifactMissing(
                f"工件 {artifact_id[:12]} 不存在（可能已被清理）；不能在不丢失协作历史的情况下恢复"
            )
        return json.loads(path.read_bytes().decode("utf-8"))


# 默认实例按需创建，测试可以用自己的目录覆盖。
_default_store: ArtifactStore | None = None


def default_store() -> ArtifactStore:
    global _default_store
    if _default_store is None:
        _default_store = ArtifactStore()
    return _default_store
