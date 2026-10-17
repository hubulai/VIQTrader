"""AkShare 适配器映射逻辑离线单测（注入假取数源，不联网）。

覆盖：能力集合、Protocol 合规、列/dtype/主键、取值、空结果、未声明能力抛错、
源异常包装、重试、去重、非现金分红等。
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import pandas as pd
import pytest

from viqtrader.data import contract
from viqtrader.data.adapters.akshare import AkShareAdapter, AkShareSource, DatacenterEquityClient
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

_INDUSTRY_NAME_RAW = pd.DataFrame(
    {
        "排名": [1, 2],
        "板块名称": ["酿酒行业", "银行"],
        "板块代码": ["BK0477", "BK0475"],
    }
)

_INDUSTRY_CONS_RAW: dict[str, pd.DataFrame] = {
    "酿酒行业": pd.DataFrame(
        {"序号": [1, 2], "代码": ["600519", "000858"], "名称": ["贵州茅台", "五粮液"]}
    ),
    "银行": pd.DataFrame({"序号": [1], "代码": ["000001"], "名称": ["平安银行"]}),
}

# 乱序 + 含窗口外日期，验证排序与 [start, end] 过滤
_INDEX_BAR_RAW = pd.DataFrame(
    {
        "日期": ["2024-01-04", "2024-01-03", "2024-01-02", "2023-12-29"],
        "开盘": [3905.0, 3910.0, 3900.0, 3880.0],
        "收盘": [3910.0, 3925.0, 3915.0, 3895.0],
        "最高": [3925.0, 3930.0, 3920.0, 3900.0],
        "最低": [3895.0, 3900.0, 3890.0, 3875.0],
        "成交量": [118000000, 125000000, 120000000, 115000000],
        "成交额": [1.47e11, 1.56e11, 1.50e11, 1.44e11],
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

    def stock_board_industry_name_em(self) -> pd.DataFrame:
        self.calls.append("industry_name")
        return _INDUSTRY_NAME_RAW.copy()

    def stock_board_industry_cons_em(self, symbol: str) -> pd.DataFrame:
        self.calls.append(f"industry_cons:{symbol}")
        return _INDUSTRY_CONS_RAW[symbol].copy()

    def index_zh_a_hist(
        self, symbol: str, period: str, start_date: str, end_date: str
    ) -> pd.DataFrame:
        self.calls.append(f"index:{symbol}:{start_date}:{end_date}")
        return _INDEX_BAR_RAW.copy()


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
            Capability.INDUSTRY_MAP,
            Capability.INDEX_BAR,
            Capability.SHARE_STRUCTURE,
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


# ---------------- INDUSTRY_MAP ----------------


def test_industry_map_mapping(adapter: AkShareAdapter) -> None:
    df = adapter.fetch_industry_map()
    contract.check_industry_map(adapter)
    assert list(df.columns) == list(SCHEMAS[Capability.INDUSTRY_MAP].columns)
    assert not df.duplicated(subset=["code"]).any()
    mapping = dict(zip(df["code"], df["industry"], strict=True))
    assert mapping == {"600519": "酿酒行业", "000858": "酿酒行业", "000001": "银行"}


def test_industry_map_covers_all_boards_with_interval() -> None:
    """逐个行业取成份股（industry_interval 可配，测试置 0 免等待）。"""
    source = FakeAkSource()
    ad = AkShareAdapter(source=source, retries=1, retry_wait=0.0, industry_interval=0.0)
    ad.fetch_industry_map()
    assert "industry_name" in source.calls
    cons_calls = sorted(c for c in source.calls if c.startswith("industry_cons:"))
    assert cons_calls == ["industry_cons:酿酒行业", "industry_cons:银行"]


def test_industry_map_board_list_broken_raises() -> None:
    """行业名单返回异常形状时 fail-fast（静默缺行业比报错更危险）。"""
    source = FakeAkSource()
    source.stock_board_industry_name_em = lambda: pd.DataFrame()  # type: ignore[method-assign]
    ad = AkShareAdapter(source=source, retries=1, retry_wait=0.0, industry_interval=0.0)
    with pytest.raises(DataSourceError, match="板块名称"):
        ad.fetch_industry_map()


def test_industry_map_cons_failure_raises() -> None:
    """任一行业成份股重试后仍失败即整体报错。"""
    source = FakeAkSource()

    def boom(symbol: str) -> pd.DataFrame:
        raise ConnectionError("transient")

    source.stock_board_industry_cons_em = boom  # type: ignore[method-assign]
    ad = AkShareAdapter(source=source, retries=2, retry_wait=0.0, industry_interval=0.0)
    with pytest.raises(DataSourceError, match="stock_board_industry_cons_em"):
        ad.fetch_industry_map()


# ---------------- INDEX_BAR ----------------


def test_index_bar_mapping(adapter: AkShareAdapter) -> None:
    df = adapter.fetch_index_bar("000300", date(2024, 1, 2), date(2024, 1, 4))
    contract.check_index_bar(adapter, "000300", date(2024, 1, 2), date(2024, 1, 4))
    assert list(df.columns) == list(SCHEMAS[Capability.INDEX_BAR].columns)
    assert len(df) == 3  # 窗口外的 2023-12-29 被过滤
    assert df["trade_date"].is_monotonic_increasing  # 乱序源已按日期排序
    assert set(df["code"].unique()) == {"000300"}
    first = df.iloc[0]
    assert first["trade_date"] == pd.Timestamp("2024-01-02")
    assert first["close"] == pytest.approx(3915.0)
    assert first["volume"] == pytest.approx(120000000)


def test_index_bar_bad_code_returns_empty_without_calling_source() -> None:
    source = FakeAkSource()
    ad = AkShareAdapter(source=source, retries=1, retry_wait=0.0)
    df = ad.fetch_index_bar("ZZZ_NONEXIST", date(2024, 1, 2), date(2024, 1, 4))
    contract.check_frame(df, Capability.INDEX_BAR, adapter="akshare")
    assert df.empty
    assert source.calls == []


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


# ---------------- SHARE_STRUCTURE（东财 datacenter F10 股本变动历史） ----------------

_SHARE_PAGE = [
    {
        "SECURITY_CODE": "600519",
        "SECUCODE": "600519.SH",
        "END_DATE": "2023-12-31 00:00:00",
        "TOTAL_SHARES": 1256197800,
        "TOTAL_A_SHARES": 1256197800,
        "LISTED_A_SHARES": 1256197800,
        "CHANGE_REASON": "定期报告",
    },
    {  # 同 (code, change_date) 重复 → 保留页序最后一条
        "SECURITY_CODE": "600519",
        "END_DATE": "2023-12-31 00:00:00",
        "TOTAL_SHARES": 1256197801,
        "TOTAL_A_SHARES": None,
        "LISTED_A_SHARES": None,
        "CHANGE_REASON": "同日补充",
    },
    {
        "SECURITY_CODE": "000001",
        "END_DATE": "2024-06-30 00:00:00",
        "TOTAL_SHARES": 19405918198,
        "TOTAL_A_SHARES": 19405918198,
        "LISTED_A_SHARES": 19405918198,
        "CHANGE_REASON": "定期报告",
    },
    {  # TOTAL_SHARES 为空 → 丢弃
        "SECURITY_CODE": "910001",
        "END_DATE": "2023-12-31 00:00:00",
        "TOTAL_SHARES": None,
        "CHANGE_REASON": "非股票",
    },
    {  # END_DATE 缺失 → 丢弃
        "SECURITY_CODE": "600519",
        "END_DATE": None,
        "TOTAL_SHARES": 100,
        "CHANGE_REASON": "异常行",
    },
    {  # 非 6 位代码 → 丢弃
        "SECURITY_CODE": "ABC123",
        "END_DATE": "2023-12-31 00:00:00",
        "TOTAL_SHARES": 500,
        "CHANGE_REASON": "噪声",
    },
]


class FakeEquityClient:
    """假 datacenter 客户端：按页返回预置记录，记录调用参数。"""

    def __init__(self, pages: list[list[dict]]) -> None:
        self._pages = pages
        self.calls: list[tuple[int, int]] = []

    def fetch_page(self, page_number: int, page_size: int) -> list[dict]:
        self.calls.append((page_number, page_size))
        if page_number > len(self._pages):
            raise AssertionError(f"越页请求: {page_number}")
        return self._pages[page_number - 1]


def test_share_structure_mapping_and_dedupe() -> None:
    client = FakeEquityClient([_SHARE_PAGE])
    ad = AkShareAdapter(equity_client=client, share_interval=0.0)
    df = ad.fetch_share_structure()
    contract.check_frame(df, Capability.SHARE_STRUCTURE, adapter="akshare")
    assert list(df.columns) == list(SCHEMAS[Capability.SHARE_STRUCTURE].columns)
    # 6 行原始 → 保留 2 行（空股本/空日期/非6位代码各丢 1，茅台同日去重 1）
    assert len(df) == 2
    mt = df[df["code"] == "600519"]
    assert len(mt) == 1
    assert mt["total_shares"].iloc[0] == pytest.approx(1256197801)  # 同日保留最后一条
    assert mt["change_date"].iloc[0] == pd.Timestamp("2023-12-31")
    pa = df[df["code"] == "000001"].iloc[0]
    assert pa["total_shares"] == pytest.approx(19405918198)
    assert pa["total_a_shares"] == pytest.approx(19405918198)


def test_share_structure_pagination_stops_on_short_page() -> None:
    page1 = [
        {"SECURITY_CODE": f"{i:06d}", "END_DATE": "2023-12-31", "TOTAL_SHARES": 1000}
        for i in range(500)
    ]
    page2 = [_SHARE_PAGE[0]]
    client = FakeEquityClient([page1, page2])
    ad = AkShareAdapter(equity_client=client, share_interval=0.0)
    df = ad.fetch_share_structure()
    assert client.calls == [(1, 500), (2, 500)]  # 末页不足一页即停
    assert len(df) == 501


def test_share_structure_empty_pages_returns_empty_frame() -> None:
    client = FakeEquityClient([[]])
    ad = AkShareAdapter(equity_client=client, share_interval=0.0)
    df = ad.fetch_share_structure()
    contract.check_frame(df, Capability.SHARE_STRUCTURE, adapter="akshare")
    assert df.empty


def test_datacenter_client_request_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}

    class FakeResp:
        def __enter__(self) -> FakeResp:
            return self

        def __exit__(self, *args: object) -> bool:
            return False

        def read(self) -> bytes:
            return json.dumps(
                {
                    "success": 1,
                    "code": 0,
                    "result": {
                        "count": 1,
                        "pages": 1,
                        "data": [
                            {
                                "SECURITY_CODE": "600519",
                                "END_DATE": "2023-12-31",
                                "TOTAL_SHARES": 1256197800,
                            }
                        ],
                    },
                }
            ).encode("utf-8")

    def fake_urlopen(req: Any, timeout: float | None = None) -> FakeResp:
        captured["url"] = str(req.full_url)
        captured["ua"] = str(req.headers.get("User-agent"))
        return FakeResp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = DatacenterEquityClient(retries=1, retry_wait=0.0)
    rows = client.fetch_page(1, 500)
    assert rows[0]["SECURITY_CODE"] == "600519"
    for fragment in (
        "reportName=RPT_F10_EH_EQUITY",
        "pageSize=500",
        "pageNumber=1",
        "sortColumns=SECUCODE",
        "source=HSF10",
    ):
        assert fragment in captured["url"], f"URL 缺少 {fragment}: {captured['url']}"
    assert "Mozilla" in captured["ua"], "datacenter 请求必须带浏览器 UA"


def test_datacenter_client_failure_wrapped_as_datasource_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_urlopen(req: Any, timeout: float | None = None) -> object:
        raise ConnectionError("RemoteDisconnected")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = DatacenterEquityClient(retries=2, retry_wait=0.0)
    with pytest.raises(DataSourceError, match="第 3 页"):
        client.fetch_page(3, 500)
