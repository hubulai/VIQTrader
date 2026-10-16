"""P1.3 QMT 适配器离线单测：fake QMTSource 注入，验证映射与契约行为。

不依赖 xtquant / miniQMT 终端；真实终端联调在 tests/integration/test_qmt_adapter.py。
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from viqtrader.data.adapters.qmt import (
    QMTAdapter,
    _map_daily_bar,
    _map_financial,
    _map_stock_list,
    from_qmt_code,
    infer_market,
    to_qmt_code,
)
from viqtrader.data.base import (
    SCHEMAS,
    Capability,
    DataSourceError,
    UnsupportedCapabilityError,
)

# ---------------------------------------------------------------------------
# 代码/市场转换
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "market"),
    [
        ("600519", "SH"),
        ("601088", "SH"),
        ("688981", "SH"),
        ("000858", "SZ"),
        ("300750", "SZ"),
        ("002594", "SZ"),
        ("430047", "BJ"),
        ("830799", "BJ"),
        ("920001", "BJ"),
        ("00700", "HK"),
        ("03690", "HK"),
    ],
)
def test_infer_market(code: str, market: str) -> None:
    assert infer_market(code) == market


def test_infer_market_invalid() -> None:
    with pytest.raises(DataSourceError):
        infer_market("ABC123")


def test_qmt_code_roundtrip() -> None:
    assert to_qmt_code("600519") == "600519.SH"
    assert to_qmt_code("00700") == "00700.HK"
    assert to_qmt_code("300750", "SZ") == "300750.SZ"
    assert from_qmt_code("600519.SH") == ("600519", "SH")
    assert from_qmt_code("00700.HK") == ("00700", "HK")


# ---------------------------------------------------------------------------
# 日线映射
# ---------------------------------------------------------------------------


def _raw_bar_frame() -> pd.DataFrame:
    idx = pd.Index([20240102, 20240103], name="time")
    return pd.DataFrame(
        {
            "open": [1700.0, 1690.0],
            "high": [1710.0, 1700.0],
            "low": [1690.0, 1680.0],
            "close": [1705.0, 1695.0],
            "volume": [30000.0, 28000.0],
            "amount": [5.1e7, 4.7e7],
        },
        index=idx,
    )


def test_map_daily_bar_without_ratio() -> None:
    df = _map_daily_bar(_raw_bar_frame(), None, "600519")
    assert list(df.columns) == list(SCHEMAS[Capability.DAILY_BAR].columns)
    assert len(df) == 2
    assert df["trade_date"].iloc[0] == pd.Timestamp("2024-01-02")
    assert df["close"].iloc[1] == 1695.0
    assert df["adj_factor"].isna().all()
    assert df["turnover_rate"].isna().all()


def test_map_daily_bar_with_ratio() -> None:
    ratio = pd.DataFrame({"close": [1705.0, 847.5]}, index=pd.Index([20240102, 20240103]))
    df = _map_daily_bar(_raw_bar_frame(), ratio, "600519")
    assert df["adj_factor"].iloc[0] == pytest.approx(1.0)
    assert df["adj_factor"].iloc[1] == pytest.approx(0.5)


def test_map_daily_bar_empty() -> None:
    df = _map_daily_bar(pd.DataFrame(), None, "600519")
    assert df.empty
    assert list(df.columns) == list(SCHEMAS[Capability.DAILY_BAR].columns)


def test_map_daily_bar_epoch_index() -> None:
    idx = pd.Index([1704153600000, 1704240000000])  # 2024-01-02 / 2024-01-03 UTC
    raw = _raw_bar_frame()
    raw.index = idx
    df = _map_daily_bar(raw, None, "600519")
    assert df["trade_date"].dt.year.eq(2024).all()
    assert df["trade_date"].dt.month.eq(1).all()


# ---------------------------------------------------------------------------
# 财务映射
# ---------------------------------------------------------------------------


def _fake_financial_tables() -> dict[str, pd.DataFrame]:
    """仿真实终端结构：RangeIndex + m_timetag/m_anntime（YYYYMMDD）+ 英文字段名。"""
    tags = ["20240331", "20240630"]
    anns = ["20240420", "20240815"]
    return {
        "Income": pd.DataFrame(
            {
                "m_timetag": tags,
                "m_anntime": anns,
                "revenue": [4.0e10, 8.5e10],
                "net_profit_excl_min_int_inc": [2.0e10, 4.2e10],
            },
        ),
        "Balance": pd.DataFrame(
            {
                "m_timetag": tags,
                "m_anntime": anns,
                "tot_assets": [2.5e11, 2.6e11],
                "total_equity": [2.0e11, 2.1e11],
            },
        ),
        "CashFlow": pd.DataFrame(
            {
                "m_timetag": tags,
                "m_anntime": anns,
                "net_cash_flows_oper_act": [2.2e10, 4.5e10],
                "cash_pay_acq_const_fiolta": [1.0e9, 2.0e9],
            },
        ),
        "PershareIndex": pd.DataFrame(
            {
                "m_timetag": tags,
                "m_anntime": anns,
                "s_fa_eps_basic": [15.0, 32.0],
                "s_fa_bps": [160.0, 170.0],
                "equity_roe": [30.0, 31.0],
                "sales_gross_profit": [91.0, 91.5],
                "inc_net_profit_rate": [50.0, 49.5],
                "gear_ratio": [20.0, 19.0],
            },
        ),
        "Capital": pd.DataFrame({"m_timetag": tags, "m_anntime": anns}),
    }


def test_map_financial_full() -> None:
    df = _map_financial(_fake_financial_tables(), "600519")
    assert list(df.columns) == list(SCHEMAS[Capability.FINANCIAL].columns)
    assert len(df) == 2
    row = df.iloc[0]
    assert row["report_period"] == pd.Timestamp("2024-03-31")
    assert row["ann_date"] == pd.Timestamp("2024-04-20")  # m_anntime=公告日
    assert row["revenue"] == 4.0e10
    assert row["roe"] == pytest.approx(0.30)  # 百分数 / 100
    assert row["gross_margin"] == pytest.approx(0.91)
    assert row["debt_ratio"] == pytest.approx(0.20)
    assert row["fcf"] == pytest.approx(2.2e10 - 1.0e9)  # ocf - capex


def test_map_financial_missing_table() -> None:
    tables = _fake_financial_tables()
    del tables["CashFlow"]
    df = _map_financial(tables, "600519")
    assert len(df) == 2
    assert df["ocf"].isna().all()
    assert df["fcf"].isna().all()
    assert df["revenue"].notna().all()  # 其余表不受影响


def test_map_financial_missing_field() -> None:
    tables = _fake_financial_tables()
    tables["PershareIndex"] = tables["PershareIndex"].drop(columns=["equity_roe"])
    df = _map_financial(tables, "600519")
    assert df["roe"].isna().all()
    assert df["eps"].notna().all()


def test_map_financial_empty() -> None:
    df = _map_financial({}, "600519")
    assert df.empty
    assert list(df.columns) == list(SCHEMAS[Capability.FINANCIAL].columns)


def test_map_financial_restated_duplicate_kept_latest() -> None:
    """同表同报告期多版本（原披露+调整后）：保留公告日最新一行，主键唯一。"""
    tables = {
        "Income": pd.DataFrame(
            {
                "m_timetag": ["20231231", "20231231", "20240331"],
                "m_anntime": ["20240420", "20240615", "20240425"],
                "revenue": [1.0e10, 1.1e10, 4.0e10],
                "net_profit_excl_min_int_inc": [5.0e9, 5.5e9, 2.0e10],
            }
        ),
    }
    df = _map_financial(tables, "600519")
    assert df["report_period"].is_unique  # 2026-10-16 QMT 实测：多版本致主键冲突
    row = df[df["report_period"] == pd.Timestamp("2023-12-31")].iloc[0]
    assert row["ann_date"] == pd.Timestamp("2024-06-15")  # 调整后版本胜出
    assert row["revenue"] == 1.1e10


def test_map_financial_cross_table_ann_mismatch_takes_latest() -> None:
    """跨表公告日不一致：合并为一行，公告日取各表最晚值（保守无前视）。"""
    tables = {
        "Income": pd.DataFrame(
            {
                "m_timetag": ["20231231"],
                "m_anntime": ["20240420"],
                "revenue": [1.0e10],
                "net_profit_excl_min_int_inc": [5.0e9],
            }
        ),
        "PershareIndex": pd.DataFrame(
            {
                "m_timetag": ["20231231"],
                "m_anntime": ["20240422"],
                "s_fa_eps_basic": [15.0],
            }
        ),
    }
    df = _map_financial(tables, "600519")
    assert len(df) == 1
    assert df.iloc[0]["ann_date"] == pd.Timestamp("2024-04-22")
    assert df.iloc[0]["revenue"] == 1.0e10
    assert df.iloc[0]["eps"] == 15.0


def test_map_financial_cumulative_to_quarterly() -> None:
    """累计口径折算单季：Q1 原样，H1/年报差分；跨年 Q1 不差分；fcf 用单季值。"""
    tables = {
        "Income": pd.DataFrame(
            {
                "m_timetag": ["20230331", "20230630", "20231231", "20240331"],
                "m_anntime": ["20230425", "20230810", "20240417", "20240425"],
                "revenue": [100.0, 300.0, 700.0, 90.0],
                "net_profit_excl_min_int_inc": [50.0, 140.0, 330.0, 45.0],
            }
        ),
        "CashFlow": pd.DataFrame(
            {
                "m_timetag": ["20230331", "20230630", "20231231", "20240331"],
                "m_anntime": ["20230425", "20230810", "20240417", "20240425"],
                "net_cash_flows_oper_act": [60.0, 150.0, 320.0, 55.0],
                "cash_pay_acq_const_fiolta": [10.0, 30.0, 60.0, 8.0],
            }
        ),
        "PershareIndex": pd.DataFrame(
            {
                "m_timetag": ["20230331", "20230630", "20231231", "20240331"],
                "m_anntime": ["20230425", "20230810", "20240417", "20240425"],
                "s_fa_eps_basic": [2.0, 6.0, 15.0, 1.8],
            }
        ),
    }
    df = _map_financial(tables, "600519")
    by_period = df.set_index("report_period")
    # Q1 原样（含跨年 2024Q1 不差分）
    assert by_period.loc[pd.Timestamp("2023-03-31"), "revenue"] == 100.0
    assert by_period.loc[pd.Timestamp("2024-03-31"), "revenue"] == 90.0
    # 年内相邻差分：H1 单季 = 300-100；Q4 单季 = 700-300（缺 Q3，报告期连续判定只查上一行，
    # 20231231 的上一行是 20230630 相差 6 个月 → 不连续 → 保守保留累计 700）
    assert by_period.loc[pd.Timestamp("2023-06-30"), "revenue"] == 200.0
    assert by_period.loc[pd.Timestamp("2023-12-31"), "revenue"] == 700.0
    assert by_period.loc[pd.Timestamp("2023-06-30"), "net_profit"] == 90.0
    assert by_period.loc[pd.Timestamp("2023-06-30"), "eps"] == 4.0
    # fcf 用单季值：2023H1 = (150-60) - (30-10) = 70
    assert by_period.loc[pd.Timestamp("2023-06-30"), "fcf"] == 70.0


def test_map_financial_gap_keeps_cumulative() -> None:
    """报告期不连续（缺上期）时保守保留累计原值，不做错误差分。"""
    tables = {
        "Income": pd.DataFrame(
            {
                "m_timetag": ["20230331", "20230930"],  # 缺 H1
                "m_anntime": ["20230425", "20231030"],
                "revenue": [100.0, 500.0],
                "net_profit_excl_min_int_inc": [50.0, 240.0],
            }
        ),
    }
    df = _map_financial(tables, "600519")
    row = df[df["report_period"] == pd.Timestamp("2023-09-30")].iloc[0]
    assert row["revenue"] == 500.0  # 不差分，宁缺勿错


# ---------------------------------------------------------------------------
# 标的主数据映射
# ---------------------------------------------------------------------------


def test_map_stock_list() -> None:
    df = _map_stock_list(["600519.SH", "000858.SZ"], is_hk_connect=False)
    assert len(df) == 2
    assert set(df["market"]) == {"SH", "SZ"}
    assert not df["is_hk_connect"].any()

    hk = _map_stock_list(["00700.HK"], is_hk_connect=True)
    assert hk["is_hk_connect"].iloc[0] is True or hk["is_hk_connect"].iloc[0]


def test_map_stock_list_empty() -> None:
    df = _map_stock_list([], is_hk_connect=False)
    assert df.empty
    assert list(df.columns) == list(SCHEMAS[Capability.STOCK_LIST].columns)


# ---------------------------------------------------------------------------
# 适配器行为（fake source 全链路）
# ---------------------------------------------------------------------------


class _FakeSource:
    """离线假 QMT 源：预置返回，记录调用。"""

    def __init__(self) -> None:
        self.downloaded: list[tuple[str, str, str, str]] = []

    def download_history_data(self, stock_code, period, start_time, end_time) -> None:
        self.downloaded.append((stock_code, period, start_time, end_time))

    def get_market_data_ex(
        self, field_list, stock_list, period, start_time, end_time, count, dividend_type, fill_data
    ):
        if dividend_type == "none":
            return {stock_list[0]: _raw_bar_frame()}
        if dividend_type == "front_ratio":
            return {
                stock_list[0]: pd.DataFrame(
                    {"close": [1705.0, 1695.0]},
                    index=pd.Index([20240102, 20240103]),
                )
            }
        return {}

    def download_financial_data2(
        self, stock_list, table_list, start_time, end_time, callback
    ) -> None:
        assert "PershareIndex" in table_list  # 缺则无 ROE/EPS
        # 仿真实终端：逐表回调，载荷 {'total': N, 'finished': N}
        callback({"total": len(table_list), "finished": len(table_list)})

    def get_financial_data(self, stock_list, table_list, start_time, end_time, report_type):
        return {stock_list[0]: _fake_financial_tables()}

    def get_stock_list_in_sector(self, sector_name):
        if sector_name == "沪深京A股":
            return ["600519.SH", "000858.SZ", "830799.BJ"]
        return []


def test_adapter_capabilities() -> None:
    adapter = QMTAdapter(source=_FakeSource())
    assert adapter.capabilities() == frozenset(
        {Capability.DAILY_BAR, Capability.FINANCIAL, Capability.STOCK_LIST}
    )
    assert adapter.name == "qmt"


def test_adapter_fetch_daily_bar() -> None:
    src = _FakeSource()
    adapter = QMTAdapter(source=src)
    df = adapter.fetch_daily_bar("600519", date(2024, 1, 1), date(2024, 1, 31))
    assert len(df) == 2
    assert src.downloaded == [("600519.SH", "1d", "20240101", "20240131")]
    assert df["adj_factor"].notna().all()


def test_adapter_fetch_financial() -> None:
    adapter = QMTAdapter(source=_FakeSource())
    df = adapter.fetch_financial("600519")
    assert len(df) == 2
    assert df["roe"].notna().all()


def test_adapter_fetch_financial_hk_rejected() -> None:
    adapter = QMTAdapter(source=_FakeSource())
    with pytest.raises(UnsupportedCapabilityError):
        adapter.fetch_financial("00700")


def test_adapter_fetch_stock_list_all() -> None:
    adapter = QMTAdapter(source=_FakeSource())
    df = adapter.fetch_stock_list()
    assert len(df) == 3
    assert set(df["market"]) == {"SH", "SZ", "BJ"}
    assert not df["is_hk_connect"].any()


def test_adapter_fetch_stock_list_market_filter() -> None:
    adapter = QMTAdapter(source=_FakeSource())
    df = adapter.fetch_stock_list("SH")
    assert set(df["market"]) == {"SH"}
    assert not df["is_hk_connect"].any()


def test_adapter_fetch_stock_list_hk_empty() -> None:
    """港股通名单归 AkShare，QMT market='HK' 返回空帧。"""
    adapter = QMTAdapter(source=_FakeSource())
    df = adapter.fetch_stock_list("HK")
    assert df.empty
    assert list(df.columns) == list(SCHEMAS[Capability.STOCK_LIST].columns)


@pytest.mark.parametrize(
    ("method", "kwargs"),
    [
        ("fetch_dividend", {"code": "600519"}),
        (
            "fetch_disclosure",
            {"code": "600519", "start": date(2024, 1, 1), "end": date(2024, 1, 31)},
        ),
        ("fetch_fx_rate", {"pair": "HKD/CNY", "start": date(2024, 1, 1), "end": date(2024, 1, 31)}),
        ("fetch_hk_connect_history", {}),
    ],
)
def test_adapter_unsupported(method: str, kwargs: dict) -> None:
    adapter = QMTAdapter(source=_FakeSource())
    with pytest.raises(UnsupportedCapabilityError):
        getattr(adapter, method)(**kwargs)


def test_adapter_retry_then_fail() -> None:
    class _BrokenSource(_FakeSource):
        def get_market_data_ex(self, *a, **k):
            raise ConnectionError("terminal lost")

    adapter = QMTAdapter(source=_BrokenSource(), max_retries=2, retry_backoff=0.01)
    with pytest.raises(DataSourceError, match="重试 2 次"):
        adapter.fetch_daily_bar("600519", date(2024, 1, 1), date(2024, 1, 31))


def test_adapter_lazy_import_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """无 xtquant 环境下默认源报 DataSourceError 而非 ImportError。"""
    import sys

    monkeypatch.setitem(sys.modules, "xtquant", None)
    monkeypatch.setitem(sys.modules, "xtquant.xtdata", None)
    adapter = QMTAdapter(max_retries=1)
    with pytest.raises(DataSourceError, match="xtquant"):
        adapter.fetch_daily_bar("600519", date(2024, 1, 1), date(2024, 1, 31))
