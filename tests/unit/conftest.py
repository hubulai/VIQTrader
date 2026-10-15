"""P2 引擎层测试公共 fixture：内存库 + 全迁移 + 双轨账户种子。"""

from __future__ import annotations

from pathlib import Path

import pytest

from viqtrader.config.loader import Config
from viqtrader.store.db import connect_memory
from viqtrader.store.migrate import migrate

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def config() -> Config:
    """真实 default.yaml 配置（无 user.yaml 覆盖）。"""
    return Config.load(REPO_ROOT / "config")


@pytest.fixture()
def conn():
    """内存库 + 全部迁移 + 双轨账户种子。"""
    conn = connect_memory()
    migrate(conn)
    conn.execute("INSERT INTO account VALUES ('acc_value', 'value', 'CNY', 800000, 0.8)")
    conn.execute("INSERT INTO account VALUES ('acc_swing', 'swing', 'CNY', 200000, 0.2)")
    yield conn
    conn.close()


@pytest.fixture()
def seed_bar(conn):
    """向 fact_daily_bar 插入日线数据的工厂。"""

    def _seed(
        code: str,
        trade_date: str,
        *,
        open_: float = 10.0,
        high: float = 10.5,
        low: float = 9.8,
        close: float = 10.2,
        volume: int = 1_000_000,
    ) -> None:
        conn.execute(
            """
            INSERT INTO fact_daily_bar
                (code, trade_date, open, high, low, close, volume, amount, adj_factor)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1.0)
            """,
            [code, trade_date, open_, high, low, close, volume, close * volume],
        )

    return _seed


@pytest.fixture()
def seed_stock(conn):
    """向 dim_stock 插入标的。"""

    def _seed(
        code: str,
        market: str = "SH",
        name: str = "测试标的",
        *,
        is_hk_connect: bool = False,
    ) -> None:
        conn.execute(
            """
            INSERT INTO dim_stock
                (code, market, name, industry, list_date, is_hk_connect)
            VALUES (?, ?, ?, NULL, '2000-01-01', ?)
            """,
            [code, market, name, is_hk_connect],
        )

    return _seed
