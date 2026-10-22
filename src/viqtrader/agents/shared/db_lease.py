"""DuckDB 短租约：让 LLM 调用阶段完全不持有数据库连接（P7 工程修复）。

背景：Windows 下 DuckDB 以文件锁实现单写者语义——一个进程只要持有读写连接，
其他进程（如 Web 控制台）就连只读打开都会失败并返回 503。投委会的估值/反方
各是一次可达 600 秒的深度模型调用；若像过去那样「开库跑到底」，控制台在整段
LLM 期间都读不了库。

约定（本模块是唯一入口，禁止在 Agent 内散落 duckdb.connect）：
- Agent 拆成「读输入 → LLM 调用 → 写结果」三段，LLM 段零数据库依赖；
- 读段与写段各取一次租约，退出即 close，文件锁随连接释放；
- 阶段之间不做跨阶段事务：各段均为独立 INSERT，无一致性依赖；
- 测试用内存库无法重连，故以 `hold(conn)` 复用外部连接、由调用方管理生命周期。
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager, closing, nullcontext

import duckdb

from viqtrader.config.loader import Config

# 租约函数：每次调用返回一个「进入即取连接、退出即释放」的上下文管理器。
LeaseFn = Callable[[], AbstractContextManager[duckdb.DuckDBPyConnection]]


def file_lease(config: Config) -> AbstractContextManager[duckdb.DuckDBPyConnection]:
    """真实库租约：每次进入新开连接，退出立即关闭以释放文件锁。"""
    from viqtrader.store.db import connect  # 延迟导入：避免 agents 模块导入即触达存储层

    return closing(connect(config))


def hold(conn: duckdb.DuckDBPyConnection) -> AbstractContextManager[duckdb.DuckDBPyConnection]:
    """内存库/测试租约：沿用外部连接，不关闭（生命周期由调用方负责）。"""
    return nullcontext(conn)


__all__ = ["LeaseFn", "file_lease", "hold"]
