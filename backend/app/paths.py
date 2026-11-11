"""共享路径。store 与 settings 共用同一个 SQLite 文件。"""

from __future__ import annotations

from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
DB_PATH = DATA_DIR / "plans.db"
