"""持久化 LangGraph checkpoint。

旧版使用单个全量 pickle；当前实现兼容读取旧文件，同时把新写入按 thread_id 分片。
这样旧数据无需一次性迁移，单次 checkpoint 不再重写全部线程。每个分片仍是原子替换，
异步写入在工作线程执行，避免阻塞事件循环。

边界：仍适合单进程服务；多进程需要外部锁。旧全量文件在首次运行时仍会被读取，
但不会被新写入覆盖。分片目录需要和主文件一起备份。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import pickle
from collections import defaultdict
from pathlib import Path
from threading import RLock
from uuid import uuid4

from langgraph.checkpoint.memory import InMemorySaver

logger = logging.getLogger(__name__)


class PersistentSaver(InMemorySaver):
    """兼容旧全量文件、按 thread_id 增量写入新 checkpoint 分片。"""

    _FORMAT = "thread-shards-v1"

    def __init__(self, path: Path, **kwargs):
        super().__init__(**kwargs)
        self._path = Path(path)
        self._shard_dir = self._path.with_name(self._path.name + ".d")
        self._persist_lock = RLock()
        self._restore()

    # --- restore ---------------------------------------------------------

    @staticmethod
    def _empty_storage():
        return defaultdict(lambda: defaultdict(dict))

    def _restore(self) -> None:
        # 先恢复历史全量文件，再用分片覆盖同一 thread；未迁移的旧 thread 仍可读。
        if self._path.is_file():
            try:
                payload = pickle.loads(self._path.read_bytes())
                self._restore_payload(payload)
            except Exception:
                logger.exception("Failed to restore legacy checkpoint: path=%s", self._path)

        if not self._shard_dir.is_dir():
            return
        for shard in self._shard_dir.glob("*.pkl"):
            try:
                payload = pickle.loads(shard.read_bytes())
                self._restore_shard(payload)
            except Exception:
                logger.exception("Failed to restore checkpoint shard: path=%s", shard)

    def _restore_payload(self, payload: dict) -> None:
        storage = self._empty_storage()
        for thread_id, namespaces in (payload.get("storage") or {}).items():
            for ns, checkpoints in namespaces.items():
                storage[thread_id][ns].update(checkpoints)
        self.storage = storage
        writes = defaultdict(dict)
        writes.update(payload.get("writes") or {})
        self.writes = writes
        self.blobs = dict(payload.get("blobs") or {})

    def _remove_thread_memory(self, thread_id: str) -> None:
        self.storage.pop(thread_id, None)
        for key in list(self.writes):
            if isinstance(key, tuple) and key and key[0] == thread_id:
                del self.writes[key]
        for key in list(self.blobs):
            if isinstance(key, tuple) and key and key[0] == thread_id:
                del self.blobs[key]

    def _restore_shard(self, payload: dict) -> None:
        thread_id = payload.get("thread_id")
        if not isinstance(thread_id, str) or not thread_id:
            raise ValueError("checkpoint shard missing thread_id")
        self._remove_thread_memory(thread_id)
        if payload.get("deleted"):
            return
        for ns, checkpoints in (payload.get("storage") or {}).items():
            self.storage[thread_id][ns].update(checkpoints)
        for key, value in (payload.get("writes") or []):
            self.writes[tuple(key)] = value
        for key, value in (payload.get("blobs") or []):
            self.blobs[tuple(key)] = value

    # --- shard persistence ----------------------------------------------

    def _thread_id(self, config: dict) -> str:
        thread_id = (config.get("configurable") or {}).get("thread_id")
        if not isinstance(thread_id, str) or not thread_id:
            raise ValueError("checkpoint config requires configurable.thread_id")
        return thread_id

    def _shard_path(self, thread_id: str) -> Path:
        digest = hashlib.sha256(thread_id.encode("utf-8")).hexdigest()
        return self._shard_dir / f"{digest}.pkl"

    def _thread_payload(self, thread_id: str, *, deleted: bool = False) -> dict:
        if deleted:
            return {"format": self._FORMAT, "thread_id": thread_id, "deleted": True}
        return {
            "format": self._FORMAT,
            "thread_id": thread_id,
            "deleted": False,
            "storage": {ns: dict(checkpoints) for ns, checkpoints in self.storage.get(thread_id, {}).items()},
            "writes": [(list(key), value) for key, value in self.writes.items()
                        if isinstance(key, tuple) and key and key[0] == thread_id],
            "blobs": [(list(key), value) for key, value in self.blobs.items()
                      if isinstance(key, tuple) and key and key[0] == thread_id],
        }

    def _write_shard(self, payload: dict) -> None:
        self._shard_dir.mkdir(parents=True, exist_ok=True)
        shard = self._shard_path(payload["thread_id"])
        tmp = shard.with_name(f"{shard.name}.{uuid4().hex}.tmp")
        try:
            tmp.write_bytes(pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL))
            tmp.replace(shard)
        except Exception:
            logger.exception("Failed to persist checkpoint shard: thread_id=%s", payload["thread_id"])
            raise
        finally:
            tmp.unlink(missing_ok=True)

    def _persist_thread(self, thread_id: str, *, deleted: bool = False) -> None:
        with self._persist_lock:
            self._write_shard(self._thread_payload(thread_id, deleted=deleted))

    def _put_async_sync(self, config, checkpoint, metadata, new_versions):
        with self._persist_lock:
            result = super().put(config, checkpoint, metadata, new_versions)
            self._write_shard(self._thread_payload(self._thread_id(config)))
            return result

    def _put_writes_async_sync(self, config, writes, task_id, task_path):
        with self._persist_lock:
            super().put_writes(config, writes, task_id, task_path)
            self._write_shard(self._thread_payload(self._thread_id(config)))

    def _delete_async_sync(self, thread_id):
        with self._persist_lock:
            super().delete_thread(thread_id)
            self._write_shard(self._thread_payload(thread_id, deleted=True))

    # --- LangGraph write methods ----------------------------------------

    def put(self, config, checkpoint, metadata, new_versions):
        with self._persist_lock:
            result = super().put(config, checkpoint, metadata, new_versions)
            self._write_shard(self._thread_payload(self._thread_id(config)))
            return result

    async def aput(self, config, checkpoint, metadata, new_versions):
        return await asyncio.to_thread(self._put_async_sync, config, checkpoint, metadata, new_versions)

    def put_writes(self, config, writes, task_id, task_path=""):
        with self._persist_lock:
            super().put_writes(config, writes, task_id, task_path)
            self._write_shard(self._thread_payload(self._thread_id(config)))

    async def aput_writes(self, config, writes, task_id, task_path=""):
        await asyncio.to_thread(self._put_writes_async_sync, config, writes, task_id, task_path)

    def delete_thread(self, thread_id: str) -> None:
        self._delete_async_sync(thread_id)

    async def adelete_thread(self, thread_id: str) -> None:
        await asyncio.to_thread(self._delete_async_sync, thread_id)

    # --- maintenance ----------------------------------------------------

    def reset(self) -> None:
        """丢弃全部检查点与分片快照。"""
        with self._persist_lock:
            self.storage = self._empty_storage()
            self.writes = defaultdict(dict)
            self.blobs = {}
            if self._path.exists():
                self._path.unlink()
            if self._shard_dir.exists():
                for item in self._shard_dir.glob("*"):
                    if item.is_file():
                        item.unlink()
                self._shard_dir.rmdir()
