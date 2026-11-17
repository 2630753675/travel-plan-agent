"""运行级协作式取消。

为什么是协作式：模型请求在 urllib 线程里阻塞，Python 无法安全掐断底层 socket。
因此取消只设置标记，运行链路在「下一个可检查点」（节点边界、请求返回后）主动退出，
已产生的轨迹、用量和检查点都保留，不会把半成品当成可交付行程。

标记用 threading.Event 而不是 asyncio.Event：同一运行可能被不同事件循环观察
（线上 uvicorn 与测试里的 TestClient），线程事件跨循环读取是安全的。
"""

from __future__ import annotations

from threading import Event, Lock
from datetime import datetime, timezone


class RunCancelled(Exception):
    """用户请求取消当前运行；由 API 层转换成 run_needs_input 终态。"""


class RunCancellation:
    """一次运行的取消标记。"""

    def __init__(self, run_id: str):
        self.run_id = run_id
        self.requested_at: str | None = None
        self._event = Event()

    def cancel(self) -> None:
        self.requested_at = datetime.now(timezone.utc).isoformat()
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise RunCancelled(f"运行 {self.run_id} 已被用户取消")


_REGISTRY: dict[str, RunCancellation] = {}
_LOCK = Lock()


def begin(run_id: str) -> RunCancellation:
    """为一次新运行（或恢复）创建全新标记，清掉同名历史取消状态。"""
    token = RunCancellation(run_id)
    with _LOCK:
        _REGISTRY[run_id] = token
    return token


def get(run_id: str) -> RunCancellation | None:
    with _LOCK:
        return _REGISTRY.get(run_id)


def request_cancel(run_id: str) -> bool:
    """请求取消；没有进行中的运行时返回 False。"""
    with _LOCK:
        token = _REGISTRY.get(run_id)
    if token is None:
        return False
    token.cancel()
    return True


def finish(run_id: str, token: RunCancellation | None = None) -> None:
    """运行结束（正常/失败/取消）时移除登记，避免内存长期滞留。"""
    with _LOCK:
        current = _REGISTRY.get(run_id)
        if current is None:
            return
        if token is not None and current is not token:
            return
        _REGISTRY.pop(run_id, None)
