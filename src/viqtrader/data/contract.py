"""DataAdapter 契约检查套件（可被测试与同步质检复用）。

每个 check_* 函数校验一个能力的数据帧结构（列、主键、dtype、取值合理性）与
适配器行为（日期范围、空结果、未声明能力抛 UnsupportedCapabilityError）。
任何违规抛 ContractViolation；全部通过即视为合规实现。

使用方式：
- 契约测试（tests/contract）对每个适配器实例逐个调用；
- 1.6 数据质量校验可复用 check_frame 做单帧结构检查。
"""

from __future__ import annotations

from datetime import date

import pandas as pd
from pandas.api import types as pdt

from viqtrader.data.base import (
    HK_CONNECT_ACTIONS,
    MARKETS,
    SCHEMAS,
    Capability,
    DataAdapter,
    TableSchema,
    UnsupportedCapabilityError,
)


class ContractViolation(AssertionError):
    """契约违规。消息格式：[adapter=<name>] [capability=<cap>] <详情>"""


def _fail(adapter: str, capability: Capability, detail: str) -> ContractViolation:
    return ContractViolation(f"[adapter={adapter}] [capability={capability.value}] {detail}")


def _is_text_series(col: pd.Series) -> bool:
    return pdt.is_object_dtype(col) or pdt.is_string_dtype(col)


def check_frame(
    df: pd.DataFrame,
    capability: Capability,
    *,
    adapter: str = "unknown",
) -> None:
    """单帧结构检查：列齐全、主键非空唯一、各列 dtype 符合契约。"""
    schema: TableSchema = SCHEMAS[capability]
    if not isinstance(df, pd.DataFrame):
        raise _fail(adapter, capability, f"返回值必须是 pd.DataFrame，实际 {type(df).__name__}")

    missing = [c for c in schema.columns if c not in df.columns]
    if missing:
        raise _fail(adapter, capability, f"缺少契约列: {missing}")

    for col in schema.key:
        if df[col].isna().any():
            raise _fail(adapter, capability, f"主键列 {col} 存在缺失值")
    if not df.empty and df.duplicated(subset=list(schema.key)).any():
        raise _fail(adapter, capability, f"主键 {schema.key} 存在重复行")

    for col in schema.date_columns:
        if not pdt.is_datetime64_any_dtype(df[col]):
            actual = df[col].dtype
            raise _fail(adapter, capability, f"日期列 {col} dtype 应为 datetime64，实际 {actual}")
    for col in schema.numeric_columns:
        if not pdt.is_numeric_dtype(df[col]):
            raise _fail(adapter, capability, f"数值列 {col} dtype 应为数值型，实际 {df[col].dtype}")
    for col in schema.text_columns:
        if not _is_text_series(df[col]):
            raise _fail(adapter, capability, f"文本列 {col} dtype 应为文本型，实际 {df[col].dtype}")
    for col in schema.bool_columns:
        if not (pdt.is_bool_dtype(df[col]) or df[col].isna().all()):
            raise _fail(adapter, capability, f"布尔列 {col} dtype 应为 bool，实际 {df[col].dtype}")


def _check_value_sanity(df: pd.DataFrame, capability: Capability, adapter: str) -> None:
    """取值合理性（只对非空帧执行；缺失值不参与比较）。"""
    if df.empty:
        return

    if capability is Capability.DAILY_BAR:
        for col in ("open", "high", "low", "close"):
            s = df[col].dropna()
            if not (s > 0).all():
                raise _fail(adapter, capability, f"{col} 必须为正数")
        mask = df["high"].notna() & df["low"].notna()
        if not (df.loc[mask, "high"] >= df.loc[mask, "low"]).all():
            raise _fail(adapter, capability, "存在 high < low 的行")
        vol = df["volume"].dropna()
        if not (vol >= 0).all():
            raise _fail(adapter, capability, "volume 不能为负")

    elif capability is Capability.FINANCIAL:
        mask = df["ann_date"].notna() & df["report_period"].notna()
        if not (df.loc[mask, "ann_date"] >= df.loc[mask, "report_period"]).all():
            raise _fail(adapter, capability, "披露日 ann_date 早于报告期 report_period（数据异常）")

    elif capability is Capability.DIVIDEND:
        cash = df.loc[df["dividend_type"] == "现金", "dividend_per_share"].dropna()
        if not (cash >= 0).all():
            raise _fail(adapter, capability, "现金分红 dividend_per_share 不能为负")

    elif capability is Capability.DISCLOSURE:
        titles = df["title"].dropna().astype(str).str.strip()
        if (titles == "").any():
            raise _fail(adapter, capability, "title 不能为空字符串")

    elif capability is Capability.FX_RATE:
        if not (df["rate"] > 0).all():
            raise _fail(adapter, capability, "汇率 rate 必须为正数")

    elif capability is Capability.HK_CONNECT_HISTORY:
        bad = set(df["action"].dropna().unique()) - HK_CONNECT_ACTIONS
        if bad:
            raise _fail(adapter, capability, f"action 取值非法: {sorted(bad)}，应为 IN/OUT")

    elif capability is Capability.STOCK_LIST:
        bad = set(df["market"].dropna().unique()) - MARKETS
        if bad:
            raise _fail(adapter, capability, f"market 取值非法: {sorted(bad)}")
        names = df["name"].dropna().astype(str).str.strip()
        if (names == "").any():
            raise _fail(adapter, capability, "name 不能为空字符串")


def _check_fetch(
    adapter: DataAdapter,
    capability: Capability,
    fetch: str,
    *args: object,
) -> pd.DataFrame:
    """调用适配器方法并做结构+合理性检查，返回数据帧。"""
    if capability not in adapter.capabilities():
        raise _fail(adapter.name, capability, "能力未声明，无法执行契约检查")
    df: pd.DataFrame = getattr(adapter, fetch)(*args)
    check_frame(df, capability, adapter=adapter.name)
    _check_value_sanity(df, capability, adapter.name)
    return df


# ---------- 对外契约检查（每个能力一个入口） ----------


def check_stock_list(adapter: DataAdapter) -> None:
    df = _check_fetch(adapter, Capability.STOCK_LIST, "fetch_stock_list")
    # HK 标的的 is_hk_connect 必须是明确布尔值（风控依赖）
    if not df.empty:
        hk = df[df["market"] == "HK"]
        if not hk.empty and hk["is_hk_connect"].isna().any():
            raise _fail(adapter.name, Capability.STOCK_LIST, "HK 标的 is_hk_connect 不能缺失")


def check_daily_bar(adapter: DataAdapter, code: str, start: date, end: date) -> None:
    df = _check_fetch(adapter, Capability.DAILY_BAR, "fetch_daily_bar", code, start, end)
    if df.empty:
        return
    lo, hi = pd.Timestamp(start), pd.Timestamp(end)
    if not df["trade_date"].between(lo, hi).all():
        raise _fail(adapter.name, Capability.DAILY_BAR, "trade_date 超出请求的 [start, end] 范围")
    codes = set(df["code"].unique())
    if codes != {code}:
        extra = sorted(codes - {code})
        raise _fail(adapter.name, Capability.DAILY_BAR, f"返回了未请求的标的: {extra}")


def check_financial(adapter: DataAdapter, code: str) -> None:
    _check_fetch(adapter, Capability.FINANCIAL, "fetch_financial", code)


def check_dividend(adapter: DataAdapter, code: str) -> None:
    _check_fetch(adapter, Capability.DIVIDEND, "fetch_dividend", code)


def check_disclosure(adapter: DataAdapter, code: str, start: date, end: date) -> None:
    df = _check_fetch(adapter, Capability.DISCLOSURE, "fetch_disclosure", code, start, end)
    if df.empty:
        return
    lo, hi = pd.Timestamp(start), pd.Timestamp(end)
    if not df["ann_date"].between(lo, hi).all():
        raise _fail(adapter.name, Capability.DISCLOSURE, "ann_date 超出请求的 [start, end] 范围")


def check_fx_rate(adapter: DataAdapter, pair: str, start: date, end: date) -> None:
    df = _check_fetch(adapter, Capability.FX_RATE, "fetch_fx_rate", pair, start, end)
    if df.empty:
        return
    lo, hi = pd.Timestamp(start), pd.Timestamp(end)
    if not df["trade_date"].between(lo, hi).all():
        raise _fail(adapter.name, Capability.FX_RATE, "trade_date 超出请求的 [start, end] 范围")


def check_hk_connect_history(adapter: DataAdapter) -> None:
    _check_fetch(adapter, Capability.HK_CONNECT_HISTORY, "fetch_hk_connect_history")


UNKNOWN_CODE = "ZZZ_NONEXIST"


def check_empty_result(adapter: DataAdapter) -> None:
    """查询不存在标的：已声明能力必须返回带契约列的空 DataFrame（不抛异常、不返回 None）。"""
    day = date(2024, 1, 2)
    probes: dict[Capability, tuple[str, tuple[object, ...]]] = {
        Capability.DAILY_BAR: ("fetch_daily_bar", (UNKNOWN_CODE, day, day)),
        Capability.FINANCIAL: ("fetch_financial", (UNKNOWN_CODE,)),
        Capability.DIVIDEND: ("fetch_dividend", (UNKNOWN_CODE,)),
        Capability.DISCLOSURE: ("fetch_disclosure", (UNKNOWN_CODE, day, day)),
    }
    for capability, (method, args) in probes.items():
        if capability not in adapter.capabilities():
            continue
        df = getattr(adapter, method)(*args)
        check_frame(df, capability, adapter=adapter.name)
        if not df.empty:
            raise _fail(adapter.name, capability, f"不存在标的 {UNKNOWN_CODE} 应返回空 DataFrame")


def check_unsupported_raises(adapter: DataAdapter) -> None:
    """未声明的能力：调用对应方法必须抛 UnsupportedCapabilityError。"""
    day = date(2024, 1, 2)
    methods: dict[Capability, tuple[str, tuple[object, ...]]] = {
        Capability.STOCK_LIST: ("fetch_stock_list", ()),
        Capability.DAILY_BAR: ("fetch_daily_bar", ("600519", day, day)),
        Capability.FINANCIAL: ("fetch_financial", ("600519",)),
        Capability.DIVIDEND: ("fetch_dividend", ("600519",)),
        Capability.DISCLOSURE: ("fetch_disclosure", ("600519", day, day)),
        Capability.FX_RATE: ("fetch_fx_rate", ("HKD/CNY", day, day)),
        Capability.HK_CONNECT_HISTORY: ("fetch_hk_connect_history", ()),
    }
    supported = adapter.capabilities()
    for capability, (method, args) in methods.items():
        if capability in supported:
            continue
        try:
            getattr(adapter, method)(*args)
        except UnsupportedCapabilityError:
            continue
        raise _fail(
            adapter.name,
            capability,
            "未声明的能力调用时必须抛 UnsupportedCapabilityError",
        ) from None
