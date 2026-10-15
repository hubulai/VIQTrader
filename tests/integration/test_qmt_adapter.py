"""P1.3 QMT 适配器联网集成测试。

运行前提：miniQMT 终端已启动登录 + xtquant SDK 已安装。
默认跳过（pytestmark=integration），手动执行：
    .venv\\Scripts\\python.exe -m pytest tests/integration/test_qmt_adapter.py -m integration -v
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from viqtrader.data.adapters.qmt import QMTAdapter
from viqtrader.data.base import DataSourceError

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def adapter() -> QMTAdapter:
    a = QMTAdapter()
    try:
        # 轻量探测：拉一只股票的短区间行情，验证终端连通
        a.fetch_daily_bar("600519", date(2024, 12, 30), date(2024, 12, 31))
    except DataSourceError as exc:
        pytest.skip(f"QMT 终端不可用：{exc}")
    return a


def test_daily_bar_a_share(adapter: QMTAdapter) -> None:
    df = adapter.fetch_daily_bar("600519", date(2024, 1, 1), date(2024, 1, 31))
    assert not df.empty
    assert df["close"].gt(0).all()
    assert df["trade_date"].min() >= pd.Timestamp("2024-01-01")


def test_daily_bar_hk_connect(adapter: QMTAdapter) -> None:
    df = adapter.fetch_daily_bar("00700", date(2024, 1, 1), date(2024, 1, 31))
    assert not df.empty
    assert df["close"].gt(0).all()


def test_financial_a_share(adapter: QMTAdapter) -> None:
    df = adapter.fetch_financial("600519")
    assert not df.empty
    # 实测纪律：report_time 下 ann_date 必须有值（point-in-time 关键）
    assert df["ann_date"].notna().any()
    assert df["ann_date"].ge(df["report_period"]).all()
    # PershareIndex 核心指标非空
    assert df["roe"].notna().any()
    assert df["eps"].notna().any()


def test_stock_list(adapter: QMTAdapter) -> None:
    df = adapter.fetch_stock_list()
    assert not df.empty
    # '沪深京A股' 覆盖 SH/SZ/BJ；港股通名单归 AkShare，QMT market='HK' 返回空
    assert {"SH", "SZ"}.issubset(set(df["market"]))
    assert adapter.fetch_stock_list("HK").empty
