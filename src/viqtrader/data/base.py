"""数据适配器统一契约（DataAdapter Protocol）。

任何数据源实现（QMT / AkShare / Tushare）必须满足本协议，并通过
tests/contract 下的契约测试套件。对应设计文档第六节「适配器接口约定」。

统一约定：
- 所有 fetch_* 返回 pd.DataFrame，列名与 SCHEMAS 中对应能力一致（允许多余列，入库时裁剪）。
- 日期列统一为 datetime64[ns]（NaT 表示缺失）；关键键列不允许缺失。
- 查询无结果时返回带正确列的空 DataFrame（见 empty_frame），不得返回 None 或抛异常。
- 数据源不支持某项能力时，调用对应方法必须抛 UnsupportedCapabilityError。
- code 不带市场后缀（A股 6 位 / 港股 5 位），市场信息由 market 列或参数承载。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable

import pandas as pd

Market = Literal["SH", "SZ", "BJ", "HK"]
MARKETS: frozenset[str] = frozenset({"SH", "SZ", "BJ", "HK"})

HK_CONNECT_ACTIONS: frozenset[str] = frozenset({"IN", "OUT"})


class DataSourceError(Exception):
    """数据源调用失败（网络、限流、源数据异常等）。"""


class UnsupportedCapabilityError(DataSourceError):
    """数据源不支持所请求的能力。"""


class Capability(StrEnum):
    STOCK_LIST = "stock_list"
    DAILY_BAR = "daily_bar"
    FINANCIAL = "financial"
    DIVIDEND = "dividend"
    DISCLOSURE = "disclosure"
    FX_RATE = "fx_rate"
    HK_CONNECT_HISTORY = "hk_connect_history"
    INDUSTRY_MAP = "industry_map"
    INDEX_BAR = "index_bar"
    SHARE_STRUCTURE = "share_structure"


@dataclass(frozen=True)
class TableSchema:
    """一张数据表的列契约：列集合、主键、各类型列清单。"""

    columns: tuple[str, ...]
    key: tuple[str, ...]
    date_columns: tuple[str, ...] = field(default=())
    numeric_columns: tuple[str, ...] = field(default=())
    text_columns: tuple[str, ...] = field(default=())
    bool_columns: tuple[str, ...] = field(default=())


SCHEMAS: dict[Capability, TableSchema] = {
    # dim_stock
    Capability.STOCK_LIST: TableSchema(
        columns=(
            "code",
            "market",
            "name",
            "industry",
            "list_date",
            "is_hk_connect",
            "hk_connect_in_date",
            "hk_connect_out_date",
        ),
        key=("code", "market"),
        date_columns=("list_date", "hk_connect_in_date", "hk_connect_out_date"),
        text_columns=("code", "market", "name", "industry"),
        bool_columns=("is_hk_connect",),
    ),
    # fact_daily_bar
    Capability.DAILY_BAR: TableSchema(
        columns=(
            "code",
            "trade_date",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "amount",
            "adj_factor",
            "turnover_rate",
        ),
        key=("code", "trade_date"),
        date_columns=("trade_date",),
        numeric_columns=(
            "open",
            "high",
            "low",
            "close",
            "volume",
            "amount",
            "adj_factor",
            "turnover_rate",
        ),
        text_columns=("code",),
    ),
    # fact_financial
    Capability.FINANCIAL: TableSchema(
        columns=(
            "code",
            "report_period",
            "ann_date",
            "revenue",
            "net_profit",
            "roe",
            "roa",
            "gross_margin",
            "net_margin",
            "debt_ratio",
            "ocf",
            "fcf",
            "eps",
            "bps",
            "total_assets",
            "total_equity",
        ),
        key=("code", "report_period"),
        date_columns=("report_period", "ann_date"),
        numeric_columns=(
            "revenue",
            "net_profit",
            "roe",
            "roa",
            "gross_margin",
            "net_margin",
            "debt_ratio",
            "ocf",
            "fcf",
            "eps",
            "bps",
            "total_assets",
            "total_equity",
        ),
        text_columns=("code",),
    ),
    # fact_dividend
    Capability.DIVIDEND: TableSchema(
        columns=("code", "ex_date", "pay_date", "dividend_per_share", "dividend_type"),
        key=("code", "ex_date", "dividend_type"),
        date_columns=("ex_date", "pay_date"),
        numeric_columns=("dividend_per_share",),
        text_columns=("code", "dividend_type"),
    ),
    # fact_disclosure
    Capability.DISCLOSURE: TableSchema(
        columns=("code", "ann_date", "title", "category", "summary"),
        key=("code", "ann_date", "title"),
        date_columns=("ann_date",),
        text_columns=("code", "title", "category", "summary"),
    ),
    # fact_fx_rate
    Capability.FX_RATE: TableSchema(
        columns=("pair", "trade_date", "rate"),
        key=("pair", "trade_date"),
        date_columns=("trade_date",),
        numeric_columns=("rate",),
        text_columns=("pair",),
    ),
    # dim_hk_connect_history
    Capability.HK_CONNECT_HISTORY: TableSchema(
        columns=("code", "change_date", "action", "announce_date"),
        key=("code", "change_date", "action"),
        date_columns=("change_date", "announce_date"),
        text_columns=("code", "action"),
    ),
    # 行业映射（code -> industry，回填 dim_stock.industry，Brinson 归因行业段依赖）
    Capability.INDUSTRY_MAP: TableSchema(
        columns=("code", "industry"),
        key=("code",),
        text_columns=("code", "industry"),
    ),
    # fact_index_bar（指数日线，与个股 fact_daily_bar 分表存放）
    Capability.INDEX_BAR: TableSchema(
        columns=("code", "trade_date", "open", "high", "low", "close", "volume", "amount"),
        key=("code", "trade_date"),
        date_columns=("trade_date",),
        numeric_columns=("open", "high", "low", "close", "volume", "amount"),
        text_columns=("code",),
    ),
    # fact_share_structure（股本结构变动历史，估值 market_cap/ps_ttm 依赖）
    Capability.SHARE_STRUCTURE: TableSchema(
        columns=(
            "code",
            "change_date",
            "total_shares",
            "total_a_shares",
            "listed_a_shares",
            "change_reason",
        ),
        key=("code", "change_date"),
        date_columns=("change_date",),
        numeric_columns=("total_shares", "total_a_shares", "listed_a_shares"),
        text_columns=("code", "change_reason"),
    ),
}


def empty_frame(capability: Capability) -> pd.DataFrame:
    """生成某能力的空 DataFrame（带契约列与正确 dtype），供「无结果」场景返回。"""
    schema = SCHEMAS[capability]
    dtypes: dict[str, str] = {}
    for col in schema.date_columns:
        dtypes[col] = "datetime64[ns]"
    for col in schema.numeric_columns:
        dtypes[col] = "float64"
    for col in schema.bool_columns:
        dtypes[col] = "bool"
    frame = pd.DataFrame(
        {col: pd.Series(dtype=dtypes.get(col, "object")) for col in schema.columns}
    )
    return frame


@runtime_checkable
class DataAdapter(Protocol):
    """数据源适配器协议。实现类必须声明能力集合并通过契约测试套件。"""

    @property
    def name(self) -> str:
        """数据源标识（如 "qmt" / "akshare"），用于审计与错误信息。"""
        ...

    def capabilities(self) -> frozenset[Capability]:
        """本数据源支持的能力集合；未声明的能力调用时必须抛 UnsupportedCapabilityError。"""
        ...

    def fetch_stock_list(self, market: Market | None = None) -> pd.DataFrame:
        """标的主数据；market 为 None 时返回该源覆盖的全部市场。"""
        ...

    def fetch_daily_bar(self, code: str, start: date, end: date) -> pd.DataFrame:
        """日线行情（含首尾两日）；trade_date 必须落在 [start, end] 内。"""
        ...

    def fetch_financial(self, code: str, periods: list[date] | None = None) -> pd.DataFrame:
        """财务数据；periods 为 None 时返回该源可用的全部报告期。

        ann_date 为披露日（point-in-time 近似的关键），允许缺失但应尽量提供。
        """
        ...

    def fetch_dividend(self, code: str) -> pd.DataFrame:
        """分红历史（现金/送股）。"""
        ...

    def fetch_disclosure(self, code: str, start: date, end: date) -> pd.DataFrame:
        """公告列表与摘要文本（供 LLM 使用）。"""
        ...

    def fetch_fx_rate(self, pair: str, start: date, end: date) -> pd.DataFrame:
        """汇率日线，pair 形如 "HKD/CNY"。"""
        ...

    def fetch_hk_connect_history(self) -> pd.DataFrame:
        """港股通名单变动流水（IN/OUT），用于「调出只能卖不能买」的风控硬约束。"""
        ...

    def fetch_industry_map(self) -> pd.DataFrame:
        """全市场行业映射（code -> industry），Brinson 归因的行业段划分依赖。"""
        ...

    def fetch_index_bar(self, code: str, start: date, end: date) -> pd.DataFrame:
        """指数日线行情（如沪深300 code="000300"）；trade_date 必须落在 [start, end] 内。"""
        ...

    def fetch_share_structure(self) -> pd.DataFrame:
        """全市场股本结构变动历史（A 股），按 (code, change_date) 承载时点快照。"""
        ...
