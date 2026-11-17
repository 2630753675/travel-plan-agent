"""应用配置：全部由界面维护，存 SQLite，不读环境变量。

安全约定：
- api_key 永不回传给前端，读取接口只给掩码；
- 前端提交的掩码值不会被当作新密钥覆盖已有值；
- 任何日志都不打印密钥。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from .paths import DB_PATH

MASK_CHAR = "****"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS app_config (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    payload TEXT NOT NULL
);
"""


class LLMSettings(BaseModel):
    enabled: bool = False
    provider: str = "openai-compatible"
    base_url: str = "https://api.deepseek.com/v1"
    model: str = "deepseek-chat"
    api_key: str = ""
    temperature: float = Field(default=0.3, ge=0, le=2)

    @property
    def has_credentials(self) -> bool:
        """凭据是否齐全。仅用于连通性测试：不要求已启用。"""
        return bool(self.api_key and self.base_url and self.model)

    @property
    def configured(self) -> bool:
        """是否真正参与规划：已启用且凭据齐全。"""
        return bool(self.enabled and self.has_credentials)


class OrchestrationSettings(BaseModel):
    max_revisions: int = Field(default=2, ge=1, le=4, description="快速模式最多 1 次重排；专家模式最多 1 次自动返工")
    node_timeout_s: int = Field(default=45, ge=5, le=180)
    max_runtime_s: int = Field(default=180, ge=30, le=600, description="单次运行最长秒数")
    max_model_calls: int = Field(default=40, ge=1, le=80, description="单次运行最多供应商模型请求数，含工具调用轮次")
    max_agent_turns: int = Field(default=20, ge=1, le=40, description="peer 模式最多 Agent 回合数，与模型请求数分别计数")
    max_output_tokens: int = Field(default=1400, ge=256, le=6000, description="单次模型输出上限")
    # 兼容旧配置，不再作为模型请求预算使用；实际限制由 max_model_calls/max_runtime_s 控制。
    token_budget: int = Field(default=60000, ge=1000, le=2_000_000)
    # fast：默认快速规划；expert：按需启用的有限专家协作；peer：显式深度团队。
    agent_mode: Literal["fast", "expert", "peer", "legacy"] = "fast"


class DataSourceSettings(BaseModel):
    kind: Literal["mock", "http"] = "mock"
    http_base_url: str = ""


class AppSettings(BaseModel):
    llm: LLMSettings = Field(default_factory=LLMSettings)
    orchestration: OrchestrationSettings = Field(default_factory=OrchestrationSettings)
    datasource: DataSourceSettings = Field(default_factory=DataSourceSettings)


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(_SCHEMA)
    return conn


def load() -> AppSettings:
    with _connect() as conn:
        row = conn.execute("SELECT payload FROM app_config WHERE id = 1").fetchone()
    if row is None:
        return AppSettings()
    try:
        return AppSettings(**json.loads(row[0]))
    except (json.JSONDecodeError, ValidationError):
        # 配置损坏时回到默认值，而不是让服务起不来
        return AppSettings()


def save(settings: AppSettings) -> AppSettings:
    payload = settings.model_dump_json()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO app_config (id, payload) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET payload = excluded.payload",
            (payload,),
        )
    return settings


def mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return MASK_CHAR
    return f"{key[:4]}{MASK_CHAR}{key[-4:]}"


def public_view(settings: AppSettings) -> dict:
    """给前端的视图：api_key 换成掩码，并额外告知是否已配置。"""
    data = settings.model_dump()
    data["llm"]["api_key"] = mask_key(settings.llm.api_key)
    data["llm"]["api_key_set"] = bool(settings.llm.api_key)
    return data


def merge_incoming(current: AppSettings, incoming: dict) -> AppSettings:
    """把前端提交的内容合入当前配置。

    前端若原样回传掩码（什么都没改），保留库里的真实密钥，避免被掩码覆盖。
    """
    payload = json.loads(json.dumps(incoming))  # 深拷贝
    llm = payload.get("llm") or {}
    key = llm.get("api_key")
    if key is None or MASK_CHAR in str(key):
        llm.pop("api_key", None)
    payload.setdefault("llm", llm)
    merged = current.model_dump()
    merged["llm"].update(llm)
    for section in ("orchestration", "datasource"):
        if section in payload and payload[section]:
            merged[section].update(payload[section])
    return AppSettings(**merged)
