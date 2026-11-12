"""持久化 checkpointer：让未确认的中断在服务重启后仍然可以恢复。

不引入新依赖：`InMemorySaver` 内部本来存的就是「字典 → 序列化后的字节串」，
把这三层存储快照到磁盘即可跨进程恢复。

已知边界（写清楚，避免误当成生产方案）：
- 只适用于单进程写入；多进程同时写同一个快照会互相覆盖。
- 快照损坏时丢弃并继续启动，不拖垮服务——已完成的行程在 SQLite 里，不受影响。
- 落盘是同步 IO，发生在每次 checkpoint 写入之后；本地演示规模可以接受。
"""

from __future__ import annotations

import pickle
from collections import defaultdict
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver

_EMPTY = {"storage": {}, "writes": {}, "blobs": {}}


class PersistentSaver(InMemorySaver):
    """把 LangGraph 的检查点落到单个快照文件。"""

    def __init__(self, path: Path, **kwargs):
        super().__init__(**kwargs)
        self._path = Path(path)
        self._restore()

    # --- 持久化 ---------------------------------------------------------

    def _restore(self) -> None:
        if not self._path.is_file():
            return
        try:
            payload = pickle.loads(self._path.read_bytes())
        except Exception:
            # 损坏的快照不能阻止服务启动
            return

        # LangGraph 的外层是 defaultdict(lambda: defaultdict(dict))，
        # lambda 无法 pickle，因此这里存成普通 dict 再重建嵌套结构。
        storage = defaultdict(lambda: defaultdict(dict))
        for thread_id, namespaces in (payload.get("storage") or {}).items():
            for ns, checkpoints in namespaces.items():
                storage[thread_id][ns].update(checkpoints)
        self.storage = storage

        writes = defaultdict(dict)
        writes.update(payload.get("writes") or {})
        self.writes = writes

        self.blobs = dict(payload.get("blobs") or {})

    def _persist(self) -> None:
        payload = {
            "storage": {
                thread_id: {ns: dict(checkpoints) for ns, checkpoints in namespaces.items()}
                for thread_id, namespaces in self.storage.items()
            },
            "writes": dict(self.writes),
            "blobs": dict(self.blobs),
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_bytes(pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL))
        tmp.replace(self._path)

    # --- 写路径：每次落定后刷新快照 --------------------------------------

    def put(self, config, checkpoint, metadata, new_versions):
        result = super().put(config, checkpoint, metadata, new_versions)
        self._persist()
        return result

    async def aput(self, config, checkpoint, metadata, new_versions):
        result = await super().aput(config, checkpoint, metadata, new_versions)
        self._persist()
        return result

    def put_writes(self, config, writes, task_id, task_path=""):
        super().put_writes(config, writes, task_id, task_path)
        self._persist()

    async def aput_writes(self, config, writes, task_id, task_path=""):
        await super().aput_writes(config, writes, task_id, task_path)
        self._persist()

    def delete_thread(self, thread_id: str) -> None:
        super().delete_thread(thread_id)
        self._persist()

    async def adelete_thread(self, thread_id: str) -> None:
        await super().adelete_thread(thread_id)
        self._persist()

    # --- 测试/维护用 -----------------------------------------------------

    def reset(self) -> None:
        """丢弃全部检查点与快照。"""
        self.storage = defaultdict(lambda: defaultdict(dict))
        self.writes = defaultdict(dict)
        self.blobs = {}
        self._persist()
