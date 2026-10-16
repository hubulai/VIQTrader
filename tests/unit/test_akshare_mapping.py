"""AkShare 适配器映射逻辑离线单测（注入假取数源，不联网）。

覆盖：能力集合、Protocol 合规、列/dtype/主键、取值、空结果、未声明能力抛错、
源异常包装、重试、去重、非现金分红等。
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from viqtrader.data import contract
from viqtrader.data.adapters.akshare import AkShareAdapter, AkShareSource
from viqtrader.data.base import (
    SCHEMAS,
    Capability,
    DataAdapter,
    DataSourceError,
    UnsupportedCapabilityError,
)

# ---------------- 预置原始帧（形状对齐实测真实返回） -------------

_CODE = "00700"

_INDICATOR_RAW = pd.DataFrame(
    {
        "SECUCODE": ["00700.HK", "00700.HK"],
        "REPORT_DATE": ["2023-12-31 00:00:00", "2022-12-31 00:00:00"],
        "OPERATE_INCOME": [609015000000.0, 554552000000.0],
        "HOLDER_PROFIT": [115216000000.0, 188243000000.0],
        "ROE_AVG": [15.06, 21.13],
        "ROA": [7.30, 11.77],
        "GROSS_PROFIT_RATIO": [48.13, 43.06],
        "NET_PROFIT_RATIO": [19.38, 30.57],
        "DEBT_ASSET_RATIO": [44.61, 39.13],
        "BASIC_EPS": [12.186, 20.938],
        "BPS": [88.93, 107.07],
    }
)

# 资产负债表（长表）
_BALANCE_RAW = pd.DataFrame(
    {
        "REPORT_DATE": ["2023-12-31 00:00:00"] * 3 + ["2022-12-31 00:00:00"] * 3,
        "STD_ITEM_NAME": ["总资产", "总权益", "总负债"] * 2,
        "AMOUNT": [1577000e6, 872000e6, 705000e6, 1350000e6, 812000e6, 538000e6],
    }
)

# 利润表（长表）
_INCOME_RAW = pd.DataFrame(
    {
        "REPORT_DATE": ["2023-12-31 00:00:00"] * 4 + ["2022-12-31 00:00:00"] * 4,
        "STD_ITEM_NAME": ["营业额", "毛利", "股东应占溢利", "每股基本盈利"] * 2,
        "AMOUNT": [609015e6, 293109e6, 115216e6, 12.186, 554552e6, 238825e6, 188243e6, 20.938],
    }
)

# 现金流量表（长表）
_CASHFLOW_RAW = pd.DataFrame(
    {
        "REPORT_DATE": ["2023-12-31 00:00:00", "2022-12-31 00:00:00"],
        "STD_ITEM_NAME": ["经营业务现金净额", "经营业务现金净额"],
        "AMOUNT": [221962e6, 147408e6],
    }
)

_DIVIDEND_RAW = pd.DataFrame(
    {
        "最新公告日期": ["2024-05-14", "2023-05-17", "2022-11-16", "2022-05-18"],
        "财政年度": ["2023", "2022", "2022", "2021"],
        "分红方案": [
            "每股派港币3.4元",
            "每股派港币2.4元",
            "特殊说明:每10股分派1股美团B类普通股股份(相当于每股派18.13港元)",
            "每股派港币1.6元",
        ],
        "分配类型": ["年度分配", "年度分配", "特别分配", "年度分配"],
        "除净日": ["2024-05-17", "2023-05-19", "2023-01-05", None],  # 末行为不分红/缺除净日
        "截至过户日": ["2024/05/21-2024/05/22", "", "", ""],
        "发放日": ["2024-05-31", "2023-06-05", "2023-03-24", None],
    }
)

_STOCK_LIST_RAW = pd.DataFrame(
    {
        "序号": [1, 2],
        "代码": ["00700", "1810"],  # 第二行缺前导零，需 zfill 补齐
        "名称": ["腾讯控股", "  美团-W "],
        "最新价": [300.0, 90.0],
    }
)

_FX_RAW = pd.DataFrame(
    {
        "日期": ["2023-12-29", "2023-12-28", "2023-12-27", "2023-12-26"],
        "美元": [673.30, 673.13, 672.98, 672.49],
        "港元": [85.798, 85.768, 85.753, 85.688],
    }
)


class FakeAkSource:
    """假取数源：按 symbol 分派三大报表，其余直接返回预置帧。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def stock_financial_hk_analysis_indicator_em(self, symbol: str, indicator: str) -> pd.DataFrame:
        self.calls.append(f"indicator:{symbol}:{indicator}")
        return _INDICATOR_RAW.copy()

    def stock_financial_hk_report_em(self, stock: str, symbol: str, indicator: str) -> pd.DataFrame:
        self.calls.append(f"report:{stock}:{symbol}")
        return {
            "资产负债表": _BALANCE_RAW,
            "利润表": _INCOME_RAW,
            "现金流量表": _CASHFLOW_RAW,
        }[symbol].copy()

    def stock_hk_dividend_payout_em(self, symbol: str) -> pd.DataFrame:
        self.calls.append(f"dividend:{symbol}")
        return _DIVIDEND_RAW.copy()

    def stock_dividend_cninfo(self, symbol: str) -> pd.DataFrame:
        self.calls.append(f"dividend_a:{symbol}")
        return _DIVIDEND_A_RAW.copy()

    def stock_hk_ggt_components_em(self) -> pd.DataFrame:
        self.calls.append("ggt")
        return _STOCK_LIST_RAW.copy()

    def currency_boc_safe(self) -> pd.DataFrame:
        self.calls.append("boc")
        return _FX_RAW.copy()


@pytest.fixture
def adapter() -> AkShareAdapter:
    return AkShareAdapter(source=FakeAkSource(), retries=1, retry_wait=0.0)


# ---------------- 能力集合 / Protocol ----------------


def test_protocol_conformance(adapter: AkShareAdapter) -> None:
    assert isinstance(adapter, DataAdapter)


def test_capabilities_are_measured_set(adapter: AkShareAdapter) -> None:
    assert adapter.capabilities() == frozenset(
        {
            Capability.STOCK_LIST,
            Capability.FINANCIAL,
            Capability.DIVIDEND,
            Capability.FX_RATE,
        }
    )
    # A股行情属 QMT 分工；公告/名单变动流水无可靠源 -> 不声明
    for cap in (Capability.DAILY_BAR, Capability.DISCLOSURE, Capability.HK_CONNECT_HISTORY):
        assert cap not in adapter.capabilities()


def test_undeclared_capabilities_raise(adapter: AkShareAdapter) -> None:
    with pytest.raises(UnsupportedCapabilityError):
        adapter.fetch_daily_bar("600519", date(2024, 1, 2), date(2024, 1, 2))
    with pytest.raises(UnsupportedCapabilityError):
        adapter.fetch_disclosure("00700", date(2024, 1, 2), date(2024, 1, 2))
    with pytest.raises(UnsupportedCapabilityError):
        adapter.fetch_hk_connect_history()


# ---------------- FINANCIAL ----------------


def test_financial_structure_and_values(adapter: AkShareAdapter) -> None:
    df = adapter.fetch_financial(_CODE)
    contract.check_frame(df, Capability.FINANCIAL, adapter="akshare")
    assert list(df.columns) == list(SCHEMAS[Capability.FINANCIAL].columns)
    assert len(df) == 2
    assert pd.api.types.is_datetime64_any_dtype(df["report_period"])
    assert pd.api.types.is_datetime64_any_dtype(df["ann_date"])
    assert df["ann_date"].isna().all()  # 东财源无披露日，如实留空
    # 主键唯一
    assert not df.duplicated(subset=["code", "report_period"]).any()
    row = df.set_index("report_period").loc[pd.Timestamp("2023-12-31")]
    assert row["revenue"] == pytest.approx(609015e6)
    assert row["net_profit"] == pytest.approx(115216e6)
    assert row["roe"] == pytest.approx(0.1506)
    assert row["gross_margin"] == pytest.approx(0.4813)
    assert row["debt_ratio"] == pytest.approx(0.4461)
    assert row["total_assets"] == pytest.approx(1577000e6)
    assert row["total_equity"] == pytest.approx(872000e6)
    assert row["ocf"] == pytest.approx(221962e6)
    assert pd.isna(row["fcf"])  # 源未直接给出，不做无依据推算


def test_financial_periods_filter(adapter: AkShareAdapter) -> None:
    df = adapter.fetch_financial(_CODE, periods=[date(2023, 12, 31)])
    assert len(df) == 1
    assert df.iloc[0]["report_period"] == pd.Timestamp("2023-12-31")


def test_financial_falls_back_to_statements(adapter: AkShareAdapter) -> None:
    """指标表缺列时，用三大报表口径兜底计算比率。"""
    source = FakeAkSource()
    source.stock_financial_hk_analysis_indicator_em = (  # type: ignore[method-assign]
        lambda symbol, indicator: pd.DataFrame({"REPORT_DATE": ["2023-12-31 00:00:00"]})
    )
    ad = AkShareAdapter(source=source, retries=1, retry_wait=0.0)
    df = ad.fetch_financial(_CODE)
    row = df.set_index("report_period").loc[pd.Timestamp("2023-12-31")]
    assert row["revenue"] == pytest.approx(609015e6)
    assert row["roe"] == pytest.approx(115216e6 / 872000e6)
    assert row["debt_ratio"] == pytest.approx(705000e6 / 1577000e6)


def test_empty_result_returns_contract_empty_frame(adapter: AkShareAdapter) -> None:
    contract.check_empty_result(adapter)


# ---------------- DIVIDEND ----------------


def test_dividend_mapping(adapter: AkShareAdapter) -> None:
    df = adapter.fetch_dividend(_CODE)
    contract.check_frame(df, Capability.DIVIDEND, adapter="akshare")
    assert len(df) == 3  # 缺除净日的行被丢弃
    assert pd.api.types.is_datetime64_any_dtype(df["ex_date"])
    assert df["dividend_per_share"].dtype == "float64"
    assert not df.duplicated(subset=["code", "ex_date", "dividend_type"]).any()
    cash = df[df["dividend_type"] == "现金"]
    assert (cash["dividend_per_share"] >= 0).all()
    assert role_of(df) == pytest.approx(3.4)
    special = df[df["dividend_type"] == "特别"]
    assert len(special) == 1
    assert special.iloc[0]["dividend_per_share"] == pytest.approx(18.13)


def role_of(df: pd.DataFrame) -> float:
    return float(df[df["ex_date"] == pd.Timestamp("2024-05-17")]["dividend_per_share"].iloc[0])


# A股分红（巨潮 stock_dividend_cninfo 形状，含不含税与送转两路）

_A_CODE = "600519"

_DIVIDEND_A_RAW = pd.DataFrame(
    {
        "实施方案公告日期": ["2025-06-20", "2025-12-11", "2024-06-21", "2026-04-17"],
        "分红类型": ["年度分红", "季度分红", "年度分红", "预披露"],
        "送股比例": [None, None, 2.0, None],
        "转增比例": [None, None, 3.0, None],
        "派息比例": [276.73, 239.57, None, None],
        "股权登记日": ["2025-06-25", "2025-12-18", "2024-06-26", None],
        "除权日": ["2025-06-26", "2025-12-19", "2024-06-27", None],  # 末行未实施，除权日空
        "派息日": ["2025-06-26", "2025-12-19", None, None],
        "股份到账日": [None, None, "2024-07-01", None],
        "实施方案分红说明": ["10派276.73元(含税)", "10派239.57元(含税)", "10送2转3", "无"],
        "报告时间": ["2024年报", "2025三季报", "2023年报", "2026半年报"],
    }
)


def test_dividend_a_mapping(adapter: AkShareAdapter) -> None:
    df = adapter.fetch_dividend(_A_CODE)
    contract.check_frame(df, Capability.DIVIDEND, adapter="akshare")
    assert list(df.columns) == list(SCHEMAS[Capability.DIVIDEND].columns)
    assert len(df) == 3  # 未实施预案（除权日为空）被丢弃
    assert df["code"].unique().tolist() == [_A_CODE]
    assert pd.api.types.is_datetime64_any_dtype(df["ex_date"])
    assert not df.duplicated(subset=["code", "ex_date", "dividend_type"]).any()
    # 现金：每 10 股派息 / 10 = 每股税前（A股不扣税），派息日如实落库
    cash = df[df["dividend_type"] == "现金"]
    assert len(cash) == 2
    assert (cash["dividend_per_share"] > 0).all()
    row = df[df["ex_date"] == pd.Timestamp("2025-06-26")].iloc[0]
    assert row["dividend_per_share"] == pytest.approx(27.673)
    assert row["pay_date"] == pd.Timestamp("2025-06-26")
    # 纯送转：保留记录但无现金，div_per_share 为空
    transfer = df[df["dividend_type"] == "送转"]
    assert len(transfer) == 1
    assert pd.isna(transfer.iloc[0]["dividend_per_share"])


def test_dividend_market_routing() -> None:
    """A股 6 位走巨潮、港股 5 位走东财（代码位数即市场）。"""
    source = FakeAkSource()
    ad = AkShareAdapter(source=source, retries=1, retry_wait=0.0)
    ad.fetch_dividend(_CODE)
    ad.fetch_dividend(_A_CODE)
    assert f"dividend:{_CODE}" in source.calls
    assert f"dividend_a:{_A_CODE}" in source.calls
    assert f"dividend:{_A_CODE}" not in source.calls


def test_dividend_a_empty_source_returns_contract_empty_frame() -> None:
    source = FakeAkSource()
    source.stock_dividend_cninfo = lambda symbol: pd.DataFrame()  # type: ignore[method-assign]
    ad = AkShareAdapter(source=source, retries=1, retry_wait=0.0)
    contract.check_frame(ad.fetch_dividend(_A_CODE), Capability.DIVIDEND, adapter="akshare")
    assert ad.fetch_dividend(_A_CODE).empty


# ---------------- STOCK_LIST ----------------


def test_stock_list_mapping(adapter: AkShareAdapter) -> None:
    df = adapter.fetch_stock_list()
    contract.check_stock_list(adapter)
    assert df["code"].tolist() == ["00700", "01810"]  # 缺前导零的代码被 zfill 补齐
    assert set(df["market"]) == {"HK"}
    assert set(df["name"]) == {"腾讯控股", "美团-W"}  # 名称去首尾空白
    assert df["is_hk_connect"].all()
    assert df["is_hk_connect"].dtype == bool
    assert not df.duplicated(subset=["code", "market"]).any()


def test_stock_list_non_hk_market_empty(adapter: AkShareAdapter) -> None:
    df = adapter.fetch_stock_list(market="SH")
    contract.check_frame(df, Capability.STOCK_LIST, adapter="akshare")
    assert df.empty


# ---------------- FX_RATE ----------------


def test_fx_rate_mapping(adapter: AkShareAdapter) -> None:
    df = adapter.fetch_fx_rate("HKD/CNY", date(2023, 12, 27), date(2023, 12, 29))
    contract.check_fx_rate(adapter, "HKD/CNY", date(2023, 12, 27), date(2023, 12, 29))
    assert len(df) == 3
    assert pd.api.types.is_datetime64_any_dtype(df["trade_date"])
    assert df.iloc[0]["rate"] == pytest.approx(0.85798)
    assert (df["rate"] > 0).all()


def test_fx_rate_unknown_pair_empty(adapter: AkShareAdapter) -> None:
    df = adapter.fetch_fx_rate("USD/JPY", date(2023, 12, 27), date(2023, 12, 29))
    contract.check_frame(df, Capability.FX_RATE, adapter="akshare")
    assert df.empty


# ---------------- 空源 / 异常包装 / 重试 ----------------


def test_empty_source_returns_contract_empty_frame() -> None:
    empty = pd.DataFrame()
    source = FakeAkSource()
    source.stock_hk_ggt_components_em = lambda: empty.copy()  # type: ignore[method-assign]
    source.stock_financial_hk_analysis_indicator_em = (  # type: ignore[method-assign]
        lambda symbol, indicator: empty.copy()
    )
    source.stock_financial_hk_report_em = (  # type: ignore[method-assign]
        lambda stock, symbol, indicator: empty.copy()
    )
    source.stock_hk_dividend_payout_em = lambda symbol: empty.copy()  # type: ignore[method-assign]
    source.stock_dividend_cninfo = lambda symbol: empty.copy()  # type: ignore[method-assign]
    source.currency_boc_safe = lambda: empty.copy()  # type: ignore[method-assign]
    ad = AkShareAdapter(source=source, retries=1, retry_wait=0.0)
    for cap, frame in (
        (Capability.STOCK_LIST, ad.fetch_stock_list()),
        (Capability.FINANCIAL, ad.fetch_financial(_CODE)),
        (Capability.DIVIDEND, ad.fetch_dividend(_CODE)),
        (Capability.DIVIDEND, ad.fetch_dividend(_A_CODE)),
    ):
        contract.check_frame(frame, cap, adapter="akshare")
        assert frame.empty


def test_source_error_is_wrapped() -> None:
    source = FakeAkSource()

    def boom(symbol: str) -> pd.DataFrame:
        raise ConnectionError("Remote end closed connection")

    source.stock_hk_dividend_payout_em = boom  # type: ignore[method-assign]
    ad = AkShareAdapter(source=source, retries=2, retry_wait=0.0)
    with pytest.raises(DataSourceError):
        ad.fetch_dividend(_CODE)


def test_retry_then_success() -> None:
    source = FakeAkSource()
    state = {"n": 0}

    def flaky(symbol: str) -> pd.DataFrame:
        state["n"] += 1
        if state["n"] == 1:
            raise ConnectionError("transient")
        return _DIVIDEND_RAW.copy()

    source.stock_hk_dividend_payout_em = flaky  # type: ignore[method-assign]
    ad = AkShareAdapter(source=source, retries=3, retry_wait=0.0)
    df = ad.fetch_dividend(_CODE)
    assert state["n"] == 2
    assert not df.empty


def test_invalid_code_returns_empty_without_calling_source() -> None:
    source = FakeAkSource()
    ad = AkShareAdapter(source=source, retries=1, retry_wait=0.0)
    assert ad.fetch_financial("ZZZ_NONEXIST").empty
    assert ad.fetch_dividend("ZZZ_NONEXIST").empty
    assert source.calls == []


def test_fake_source_satisfies_protocol() -> None:
    assert isinstance(FakeAkSource(), AkShareSource)  # runtime_checkable Protocol
