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
    # DuckDB 无锁等待参数（lock_timeout 非法配置项）；跨进程锁竞态由
    # orchestrator._run_script 的 gc+sleep 句柄释放缓冲兜底
    return duckdb.connect(str(db_path_from_config(config)), read_only=read_only)


def connect_memory() -> duckdb.DuckDBPyConnection:
    """测试用内存库。"""
    return duckdb.connect(":memory:")
