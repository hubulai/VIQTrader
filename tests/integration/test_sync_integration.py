"""P1.6 集成测试：联网跑 AkShare 适配器同步真实标的。

⚠️ 默认跳过（pytest.mark.integration），需要显式加 -m integration 才会执行。
"""

from __future__ import annotations

import duckdb
import pytest

from viqtrader.data import sync as _sync
from viqtrader.data.adapters.akshare import AkShareAdapter
from viqtrader.data.base import Capability
from viqtrader.store import migrate as _migrate

pytestmark = pytest.mark.integration


@pytest.fixture()
def conn() -> duckdb.DuckDBPyConnection:
    c = duckdb.connect(":memory:")
    _migrate.migrate(c)
    return c


@pytest.fixture()
def adapter() -> AkShareAdapter:
    return AkShareAdapter()


@pytest.fixture()
def cfg_dict() -> dict:
    return {
        "hk_connect": {"dividend_tax_h_share": 0.20},
        "data": {
            "sync": {
                "lookback_days_on_gap": 10,
                "financial_lookback_years": 3,
                "daily_lookback_days": 365,
            },
            "valuation": {
                "pe_percentile_window_days": 250,
                "pe_percentile_min_samples": 60,
            },
        },
    }


def test_sync_hk_connect_stock_list(conn, adapter, cfg_dict) -> None:
    """港股通成份股全量同步。

    网络不可达时（东财 stock_hk_ggt_components_em 端点偶发 RemoteDisconnected）
    测试以 pytest.skip 标记而非硬失败——这是集成测试设计：离线单测
    (tests/unit/test_*.py) 覆盖映射与入库逻辑，联网集成测试在网络恢复后自动绿。
    """
    from viqtrader.data.base import DataSourceError

    syncer = _sync.IncrementalSyncer(conn, cfg_dict)
    cfg = _sync.SyncConfig(
        codes=[],
        adapter=adapter,
        capabilities=[Capability.STOCK_LIST],
    )
    try:
        reports = syncer.sync(cfg)
    except DataSourceError as exc:
        pytest.skip(f"AkShare 港股通成份股端点不可达：{exc}")
    assert reports, "应返回报告"
    count = conn.execute("SELECT COUNT(*) FROM dim_stock").fetchone()[0]
    if count == 0:
        # 检查是否是适配器内部捕获了 DataSourceError 并写入 warning 但未抛
        for r in reports:
            if r.warnings:
                pytest.skip(f"同步过程有数据源告警：{r.warnings}")
    assert count > 0, "dim_stock 应有数据"


def test_sync_00700_financial(conn, adapter, cfg_dict) -> None:
    """腾讯控股港股财务同步。"""
    syncer = _sync.IncrementalSyncer(conn, cfg_dict)
    cfg = _sync.SyncConfig(
        codes=["00700"],
        adapter=adapter,
        capabilities=[Capability.FINANCIAL],
    )
    reports = syncer.sync(cfg)
    fin_report = next(r for r in reports if r.capability == "financial")
    assert fin_report.rows_inserted > 0, f"应有新行，报告: {fin_report}"


def test_sync_hkd_cny_fx_rate(conn, adapter, cfg_dict) -> None:
    """港币汇率同步。"""
    from datetime import date

    syncer = _sync.IncrementalSyncer(conn, cfg_dict)
    cfg = _sync.SyncConfig(
        codes=[],
        start_date=date(2024, 1, 1),
        end_date=date(2024, 12, 31),
        adapter=adapter,
        capabilities=[Capability.FX_RATE],
        fx_pairs=["HKD/CNY"],
    )
    reports = syncer.sync(cfg)
    fx_report = next(r for r in reports if r.capability == "fx_rate")
    assert fx_report.rows_inserted > 0, f"应有新行，报告: {fx_report}"
