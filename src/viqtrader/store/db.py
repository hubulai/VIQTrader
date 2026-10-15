"""DuckDB 连接入口。所有层统一经此获取连接，禁止散落 duckdb.connect。"""

from __future__ import annotations

from pathlib import Path

import duckdb

from viqtrader.config.loader import Config


def db_path_from_config(config: Config) -> Path:
    data_dir = Path(str(config.require("platform.data_dir")))
    db_file = str(config.get("platform.duckdb_file", "viqtrader.duckdb"))
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / db_file


def connect(config: Config, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(db_path_from_config(config)), read_only=read_only)


def connect_memory() -> duckdb.DuckDBPyConnection:
    """测试用内存库。"""
    return duckdb.connect(":memory:")
