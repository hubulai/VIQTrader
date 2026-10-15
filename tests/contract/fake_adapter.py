"""契约测试用假数据源：内置确定性小数据集，声明全部能力。

用途：
- 验证契约测试套件本身有效（假实现通过即算合规）；
- 作为后续真实适配器（QMT/AkShare）实现时的参照样本。
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from viqtrader.data.base import Capability, Market, empty_frame

SAMPLE_CODE_A = "600519"  # 贵州茅台（A股）
SAMPLE_CODE_HK = "00700"  # 腾讯控股（港股通）

_STOCK_LIST = pd.DataFrame(
    {
        "code": [SAMPLE_CODE_A, "000001", SAMPLE_CODE_HK],
        "market": ["SH", "SZ", "HK"],
        "name": ["贵州茅台", "平安银行", "腾讯控股"],
        "industry": ["白酒", "银行", "互联网"],
        "list_date": pd.to_datetime(["2001-08-27", "1991-04-03", "2004-06-16"]),
        "is_hk_connect": pd.Series([False, False, True], dtype="bool"),
        "hk_connect_in_date": pd.to_datetime([None, None, "2017-03-17"]),
        "hk_connect_out_date": pd.to_datetime([None, None, None]),
    }
)

_TRADE_DAYS = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"])

_DAILY_BARS: dict[str, pd.DataFrame] = {
    SAMPLE_CODE_A: pd.DataFrame(
        {
            "code": [SAMPLE_CODE_A] * 3,
            "trade_date": _TRADE_DAYS,
            "open": [1680.0, 1690.0, 1675.0],
            "high": [1695.0, 1700.0, 1690.0],
            "low": [1670.0, 1680.0, 1665.0],
            "close": [1688.0, 1692.0, 1680.0],
            "volume": [25000, 26000, 24000],
            "amount": [4.21e9, 4.39e9, 4.04e9],
            "adj_factor": [1.0, 1.0, 1.0],
            "turnover_rate": [0.31, 0.32, 0.30],
        }
    ),
    SAMPLE_CODE_HK: pd.DataFrame(
        {
            "code": [SAMPLE_CODE_HK] * 3,
            "trade_date": _TRADE_DAYS,
            "open": [298.0, 301.0, 305.0],
            "high": [303.0, 306.0, 309.0],
            "low": [296.0, 299.0, 302.0],
            "close": [301.5, 304.8, 307.2],
            "volume": [18_500_000, 19_200_000, 17_800_000],
            "amount": [5.58e9, 5.84e9, 5.46e9],
            "adj_factor": [1.0, 1.0, 1.0],
            "turnover_rate": [0.20, 0.21, 0.19],
        }
    ),
}

_FINANCIAL_600519 = pd.DataFrame(
    {
        "code": [SAMPLE_CODE_A] * 2,
        "report_period": pd.to_datetime(["2023-09-30", "2023-12-31"]),
        "ann_date": pd.to_datetime(["2023-10-21", "2024-04-03"]),
        "revenue": [1.032e11, 1.476e11],
        "net_profit": [5.29e10, 7.47e10],
        "roe": [0.255, 0.341],
        "roa": [0.198, 0.263],
        "gross_margin": [0.918, 0.919],
        "net_margin": [0.531, 0.524],
        "debt_ratio": [0.161, 0.177],
        "ocf": [5.00e10, 6.66e10],
        "fcf": [4.80e10, 6.40e10],
        "eps": [42.09, 59.49],
        "bps": [168.5, 182.3],
        "total_assets": [2.62e11, 2.73e11],
        "total_equity": [2.10e11, 2.19e11],
    }
)

_DIVIDEND_600519 = pd.DataFrame(
    {
        "code": [SAMPLE_CODE_A],
        "ex_date": pd.to_datetime(["2023-06-30"]),
        "pay_date": pd.to_datetime(["2023-07-07"]),
        "dividend_per_share": [25.911],
        "dividend_type": ["现金"],
    }
)

_DISCLOSURE_600519 = pd.DataFrame(
    {
        "code": [SAMPLE_CODE_A],
        "ann_date": pd.to_datetime(["2024-04-03"]),
        "title": ["贵州茅台2023年年度报告"],
        "category": ["财报"],
        "summary": ["2023年营收1477亿元，归母净利747亿元，同比增长19%。"],
    }
)

_FX_RATE = pd.DataFrame(
    {
        "pair": ["HKD/CNY"] * 3,
        "trade_date": _TRADE_DAYS,
        "rate": [0.9123, 0.9131, 0.9118],
    }
)

_HK_CONNECT_HISTORY = pd.DataFrame(
    {
        "code": [SAMPLE_CODE_HK],
        "change_date": pd.to_datetime(["2017-03-17"]),
        "action": ["IN"],
        "announce_date": pd.to_datetime(["2017-03-10"]),
    }
)


class FakeAdapter:
    """声明全部能力的假实现。未知标的返回带契约列的空帧。"""

    @property
    def name(self) -> str:
        return "fake"

    def capabilities(self) -> frozenset[Capability]:
        return frozenset(Capability)

    def fetch_stock_list(self, market: Market | None = None) -> pd.DataFrame:
        if market is None:
            return _STOCK_LIST.copy()
        return _STOCK_LIST[_STOCK_LIST["market"] == market].reset_index(drop=True)

    def fetch_daily_bar(self, code: str, start: date, end: date) -> pd.DataFrame:
        df = _DAILY_BARS.get(code)
        if df is None:
            return empty_frame(Capability.DAILY_BAR)
        mask = df["trade_date"].between(pd.Timestamp(start), pd.Timestamp(end))
        return df[mask].reset_index(drop=True)

    def fetch_financial(self, code: str, periods: list[date] | None = None) -> pd.DataFrame:
        if code != SAMPLE_CODE_A:
            return empty_frame(Capability.FINANCIAL)
        df = _FINANCIAL_600519
        if periods is None:
            return df.copy()
        targets = {pd.Timestamp(p) for p in periods}
        return df[df["report_period"].isin(targets)].reset_index(drop=True)

    def fetch_dividend(self, code: str) -> pd.DataFrame:
        if code != SAMPLE_CODE_A:
            return empty_frame(Capability.DIVIDEND)
        return _DIVIDEND_600519.copy()

    def fetch_disclosure(self, code: str, start: date, end: date) -> pd.DataFrame:
        if code != SAMPLE_CODE_A:
            return empty_frame(Capability.DISCLOSURE)
        df = _DISCLOSURE_600519
        mask = df["ann_date"].between(pd.Timestamp(start), pd.Timestamp(end))
        return df[mask].reset_index(drop=True)

    def fetch_fx_rate(self, pair: str, start: date, end: date) -> pd.DataFrame:
        if pair != "HKD/CNY":
            return empty_frame(Capability.FX_RATE)
        df = _FX_RATE
        mask = df["trade_date"].between(pd.Timestamp(start), pd.Timestamp(end))
        return df[mask].reset_index(drop=True)

    def fetch_hk_connect_history(self) -> pd.DataFrame:
        return _HK_CONNECT_HISTORY.copy()
