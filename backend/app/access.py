"""访问控制：本机免口令，外部访问需要口令；分享链接用签名只读放行。

设计取舍（演示场景）：
- 回环地址视为"我自己在用"，不打扰；一旦出现 `X-Forwarded-For`
  （隧道 / 反向代理会加这个头）就不再当成本机。
- 分享链接不共用总口令，而是 HMAC 签名：只读、可针对单个 plan 撤销，
  泄漏一条链接不等于泄漏整个服务。
- 口令与签名密钥在首次启动时随机生成并落到 `backend/data/`，
  不读环境变量、不写进代码，也不需要用户手填。

局限（写清楚）：这些是演示级门禁，不是账号体系——没有用户、没有会话、没有按人撤销。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from pathlib import Path

from .paths import DATA_DIR

ACCESS_TOKEN_PATH = DATA_DIR / "access_token.txt"
SHARE_SECRET_PATH = DATA_DIR / "share_secret.txt"

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _load_or_create(path: Path, length: int = 32) -> str:
    """读不到就生成一个并落盘。文件权限交给操作系统默认值。"""
    if path.is_file():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    path.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_urlsafe(length)
    path.write_text(value, encoding="utf-8")
    return value


def load_token() -> str:
    return _load_or_create(ACCESS_TOKEN_PATH)


def load_share_secret() -> str:
    return _load_or_create(SHARE_SECRET_PATH, length=48)


def is_local_client(client_host: str | None, forwarded_for: str | None) -> bool:
    """只有"直连回环且没有被代理转发过"才算本机。"""
    if forwarded_for:
        return False
    return (client_host or "") in LOOPBACK_HOSTS


def share_signature(plan_id: str, secret: str) -> str:
    return hmac.new(secret.encode(), plan_id.encode(), hashlib.sha256).hexdigest()[:16]


def verify_share(plan_id: str, signature: str | None, secret: str) -> bool:
    if not signature:
        return False
    return hmac.compare_digest(share_signature(plan_id, secret), signature)


def token_matches(supplied: str | None, token: str) -> bool:
    if not supplied:
        return False
    return hmac.compare_digest(supplied, token)
