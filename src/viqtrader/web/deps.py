"""Web 层依赖注入与 DB 访问纪律（P6）。

DuckDB 单写者约束下的访问纪律：
- 查询：`read_only=True` 短连接，每请求开关。只读连接要求库文件未被
  写者锁定——schtasks 每日 DAG（读写连接）运行的几分钟内查询会撞锁，
  统一转 LockConflictError，页面提示「每日任务运行中，稍后刷新」；
- 写操作：读写连接短事务，connect 撞锁重试 1 次后转 LockConflictError，
  路由层捕获返回 503 友好提示。

分层红线：本包只允许 import store/config/alerting（import-linter
forbidden 契约），引擎/Agent 产物一律经数据库表与 outputs/ 状态文件消费。
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import duckdb

from viqtrader.config.loader import Config
from viqtrader.store.db import connect

REPO_ROOT = Path(__file__).resolve().parents[3]


class LockConflictError(Exception):
    """DuckDB 锁冲突（另一进程持有连接，如每日 DAG 运行中）。"""


@contextmanager
def query_conn(config: Config) -> Iterator[duckdb.DuckDBPyConnection]:
    """只读短连接（每请求开关）。撞锁转 LockConflictError。"""
    try:
        conn = connect(config, read_only=True)
    except duckdb.IOException as exc:
        raise LockConflictError(f"数据库被占用（只读）: {exc}") from exc
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def write_conn(
    config: Config, *, retries: int = 1, retry_sleep: float = 0.5
) -> Iterator[duckdb.DuckDBPyConnection]:
    """读写短连接（立即提交关闭）。connect 撞锁重试后转 LockConflictError。"""
    conn: duckdb.DuckDBPyConnection | None = None
    last_exc: duckdb.IOException | None = None
    for attempt in range(retries + 1):
        try:
            conn = connect(config)
            break
        except duckdb.IOException as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(retry_sleep)
    if conn is None:
        raise LockConflictError(f"数据库被占用（写入）: {last_exc}") from last_exc
    try:
        yield conn
    except duckdb.IOException as exc:
        raise LockConflictError(f"数据库写入冲突: {exc}") from exc
    finally:
        conn.close()


def outputs_dir(config: Config) -> Path:
    """状态文件目录（P5.4 契约）：<data_dir>/outputs。"""
    return Path(str(config.require("platform.data_dir"))) / "outputs"


def alerts_dir(config: Config) -> Path:
    """告警文件日志目录（P5.1 告警路由文件通道）：<data_dir>/logs/alerts。"""
    return Path(str(config.require("platform.data_dir"))) / "logs" / "alerts"
