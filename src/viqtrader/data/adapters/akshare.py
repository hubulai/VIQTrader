"""AkShare 数据适配器（VIQTrader P1.4）。

数据源分工硬约束（设计文档第六节）：
- QMT 负责 A股+港股通行情、A股财务 5 表、交易执行；
- AkShare/Tushare 负责 港股财务、公告、港股通名单、汇率。

因此本适配器**不声明** A股行情能力，只覆盖 AkShare 侧（能力集合均由实测决定）：

- FINANCIAL  <- stock_financial_hk_report_em（三大报表）+
               stock_financial_hk_analysis_indicator_em，港股年度财务，按报告期合并
- DIVIDEND   <- stock_hk_dividend_payout_em，港股分红派息
- FX_RATE    <- currency_boc_safe，中行外汇牌价（取「港元」列）
- STOCK_LIST <- stock_hk_ggt_components_em，港股通成份股名单

未声明能力（实测确认无可靠源，调用时抛 UnsupportedCapabilityError）：
- DAILY_BAR：属 QMT 分工。
- DISCLOSURE：A股公告有 stock_individual_notice_report，但**查无港股公告源**；
  能力在契约中不区分市场，若声明则港股标的会被静默返回空帧（等价假数据），故不声明。
- HK_CONNECT_HISTORY：AkShare 无港股通名单变动流水（IN/OUT）历史源。

金额/比率口径（源数据未做无依据换算）：
- 港股财报金额为源报表口径原值（东财 CURRENCY 列标注币种，如腾讯为人民币）；
- 比率为东财口径百分数 / 100（如 GROSS_PROFIT_RATIO=56.21 → 0.5621）；
- 汇率为中行牌价「每 100 单位外币兑人民币」，港元列需 / 100 得 HKD/CNY。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import date
from typing import Protocol, cast, runtime_checkable

import pandas as pd

from ..base import (
    SCHEMAS,
    Capability,
    DataSourceError,
    Market,
    UnsupportedCapabilityError,
    empty_frame,
)

__all__ = ["AkShareAdapter", "AkShareSource"]


def _is_valid_code(code: str) -> bool:
    """港股代码为 5 位数字（A股 6 位）；非法代码直接返回空帧而非触发网络异常。"""
    return code.isdigit() and len(code) in (5, 6)


@runtime_checkable
class AkShareSource(Protocol):
    """取数缝隙：默认指向真实 akshare 模块，测试可注入假实现以离线验证映射。

    方法名与 akshare 顶层函数一致，返回原始 DataFrame（未标准化）。
    """

    def stock_financial_hk_report_em(
        self, stock: str, symbol: str, indicator: str
    ) -> pd.DataFrame: ...

    def stock_financial_hk_analysis_indicator_em(
        self, symbol: str, indicator: str
    ) -> pd.DataFrame: ...

    def stock_hk_dividend_payout_em(self, symbol: str) -> pd.DataFrame: ...

    def stock_hk_ggt_components_em(self) -> pd.DataFrame: ...

    def currency_boc_safe(self) -> pd.DataFrame: ...


def _default_source() -> AkShareSource:
    """默认取数源 = akshare 模块（延迟 import，避免离线单测付出导入成本）。"""
    import akshare as ak

    return cast("AkShareSource", ak)


def _with_retry(
    call: Callable[[], pd.DataFrame], *, retries: int, wait: float, label: str
) -> pd.DataFrame:
    """带重试的取数：AkShare 部分接口（如港股通名单）偶发断连，重试后再失败即包装为错误。"""
    last: Exception | None = None
    for _attempt in range(max(1, retries)):
        try:
            result = call()
            if not isinstance(result, pd.DataFrame):
                msg = f"{label} 返回类型异常: {type(result).__name__}"
                raise DataSourceError(msg)
            return result
        except DataSourceError:
            raise
        except Exception as exc:  # 数据源可抛任意异常，需统一收口为 DataSourceError
            last = exc
            if wait:
                time.sleep(wait)
    msg = f"[akshare] 调用 {label} 失败（重试 {retries} 次）: {last}"
    raise DataSourceError(msg) from last


# ---------------- 原始表 -> 标准化表（纯函数，供离线单测直接验证） ----------------

_FINANCIAL_COLUMNS = SCHEMAS[Capability.FINANCIAL].columns


def _indicator_wide(df: pd.DataFrame) -> pd.DataFrame:
    """指标表（单行宽表）：索引为报告期。"""
    if df is None or df.empty or "REPORT_DATE" not in df.columns:
        return pd.DataFrame()
    wide = df.drop_duplicates(subset=["REPORT_DATE"]).copy()
    wide.index = pd.to_datetime(wide["REPORT_DATE"], errors="coerce")
    return wide[wide.index.notna()]


def _items_wide(df: pd.DataFrame) -> pd.DataFrame:
    """三大报表（长表 STD_ITEM_NAME/AMOUNT）：透视成（报告期, 科目）宽表。"""
    if df is None or df.empty or not {"REPORT_DATE", "STD_ITEM_NAME", "AMOUNT"} <= set(df.columns):
        return pd.DataFrame()
    wide = df.pivot_table(
        index="REPORT_DATE",
        columns="STD_ITEM_NAME",
        values="AMOUNT",
        aggfunc="first",
    )
    wide.index = pd.to_datetime(wide.index, errors="coerce")
    return wide[wide.index.notna()]


def _map_financial(
    code: str,
    indicator_raw: pd.DataFrame,
    income_raw: pd.DataFrame,
    balance_raw: pd.DataFrame,
    cashflow_raw: pd.DataFrame,
    periods: list[date] | None = None,
) -> pd.DataFrame:
    """合并三大报表 + 指标表为「报告期一行」。指标口径优先，报表口径兜底。"""
    w_ind = _indicator_wide(indicator_raw)
    w_inc = _items_wide(income_raw)  # 利润表
    w_bal = _items_wide(balance_raw)  # 资产负债表
    w_cf = _items_wide(cashflow_raw)  # 现金流量表

    periods_idx = sorted({ts for w in (w_ind, w_inc, w_bal, w_cf) for ts in w.index})
    if not periods_idx:
        return empty_frame(Capability.FINANCIAL)
    index = pd.DatetimeIndex(periods_idx, name="report_period")

    def col(wide: pd.DataFrame, name: str) -> pd.Series:
        if name in wide.columns:
            return wide[name].reindex(index).astype("float64")
        return pd.Series(index=index, dtype="float64")

    revenue = (
        col(w_ind, "OPERATE_INCOME")
        .combine_first(col(w_inc, "营业额"))
        .combine_first(col(w_inc, "营运收入"))
    )
    net_profit = (
        col(w_ind, "HOLDER_PROFIT")
        .combine_first(col(w_inc, "股东应占溢利"))
        .combine_first(col(w_inc, "除税后溢利"))
    )
    eps = col(w_ind, "BASIC_EPS").combine_first(col(w_inc, "每股基本盈利"))
    bps = col(w_ind, "BPS")
    total_assets = col(w_bal, "总资产")
    total_equity = (
        col(w_bal, "总权益")
        .combine_first(col(w_bal, "净资产"))
        .combine_first(col(w_bal, "股东权益"))
    )
    total_liab = col(w_bal, "总负债")
    ocf = col(w_cf, "经营业务现金净额")
    gross_profit = col(w_inc, "毛利")

    # 比率：指标表口径（百分数 / 100）优先，缺失时用报表口径自行计算作为兜底。
    roe = col(w_ind, "ROE_AVG").divide(100.0).combine_first(net_profit.divide(total_equity))
    roa = col(w_ind, "ROA").divide(100.0).combine_first(net_profit.divide(total_assets))
    gross_margin = (
        col(w_ind, "GROSS_PROFIT_RATIO").divide(100.0).combine_first(gross_profit.divide(revenue))
    )
    net_margin = (
        col(w_ind, "NET_PROFIT_RATIO").divide(100.0).combine_first(net_profit.divide(revenue))
    )
    debt_ratio = (
        col(w_ind, "DEBT_ASSET_RATIO").divide(100.0).combine_first(total_liab.divide(total_assets))
    )

    out = pd.DataFrame(
        {
            "code": code,
            "report_period": index,
            "ann_date": pd.NaT,  # 东财三大报表/指标均无披露日，如实留空（不做无依据回填）
            "revenue": revenue,
            "net_profit": net_profit,
            "roe": roe,
            "roa": roa,
            "gross_margin": gross_margin,
            "net_margin": net_margin,
            "debt_ratio": debt_ratio,
            "ocf": ocf,
            "fcf": pd.Series(index=index, dtype="float64"),  # 三表未直接给自由现金流，留空
            "eps": eps,
            "bps": bps,
            "total_assets": total_assets,
            "total_equity": total_equity,
        }
    ).reset_index(drop=True)

    out["report_period"] = pd.to_datetime(out["report_period"], errors="coerce")
    out["ann_date"] = pd.to_datetime(out["ann_date"], errors="coerce")
    if periods is not None:
        targets = pd.DatetimeIndex(pd.to_datetime(list(periods), errors="coerce"))
        out = out[out["report_period"].isin(targets)]
    out = (
        out.drop_duplicates(subset=["code", "report_period"])
        .sort_values("report_period")
        .reset_index(drop=True)
    )
    for column in _FINANCIAL_COLUMNS:
        if column in out.columns and column not in ("code", "report_period", "ann_date"):
            out[column] = out[column].astype("float64")
    out["code"] = out["code"].astype("object")
    return out[list(_FINANCIAL_COLUMNS)]


def _map_dividend(code: str, raw: pd.DataFrame) -> pd.DataFrame:
    """东财港股分红派息 -> fact_dividend。分红方案文本中解析每股现金分红。"""
    if raw is None or raw.empty:
        return empty_frame(Capability.DIVIDEND)
    ex_col = raw.get("除净日")
    pay_col = raw.get("发放日")
    plan = raw.get("分红方案")
    plan_kind = raw.get("分配类型")
    ex_date = pd.to_datetime(
        ex_col if ex_col is not None else pd.Series(index=raw.index, dtype="object"),
        errors="coerce",
    )
    pay_date = pd.to_datetime(
        pay_col if pay_col is not None else pd.Series(index=raw.index, dtype="object"),
        errors="coerce",
    )
    plan = plan.astype("string") if plan is not None else pd.Series(index=raw.index, dtype="string")
    plan_kind = (
        plan_kind.astype("string")
        if plan_kind is not None
        else pd.Series(index=raw.index, dtype="string")
    )
    per_share = plan.str.extract(r"每股派(?:港币|港元)?([0-9]+(?:\.[0-9]+)?)", expand=False)
    out = pd.DataFrame(
        {
            "code": code,
            "ex_date": ex_date,
            "pay_date": pay_date,
            "dividend_per_share": pd.to_numeric(per_share, errors="coerce"),
            "dividend_type": plan_kind.map({"年度分配": "现金", "中期分配": "现金"}).fillna("特别"),
        }
    )
    out = out.dropna(subset=["ex_date"])  # 主键 ex_date 不可缺失（不分红行除净日为空）
    out = out.drop_duplicates(subset=["code", "ex_date", "dividend_type"]).reset_index(drop=True)
    out["dividend_per_share"] = out["dividend_per_share"].astype("float64")
    out["code"] = out["code"].astype("object")
    out["dividend_type"] = out["dividend_type"].astype("object")
    return out


def _map_stock_list(raw: pd.DataFrame) -> pd.DataFrame:
    """港股通成份股 -> dim_stock（仅 HK 标的，is_hk_connect 恒为 True）。"""
    if raw is None or raw.empty or "代码" not in raw.columns:
        return empty_frame(Capability.STOCK_LIST)
    code = raw["代码"].astype("string").str.zfill(5)
    name = raw["名称"].astype("string").str.strip() if "名称" in raw.columns else code
    count = len(raw)
    out = pd.DataFrame(
        {
            "code": code.to_numpy(),
            "market": ["HK"] * count,
            "name": name.to_numpy(),
            "industry": pd.Series([pd.NA] * count, dtype="object").to_numpy(),
            "list_date": pd.to_datetime(pd.Series([pd.NaT] * count)),
            "is_hk_connect": pd.Series([True] * count, dtype="bool").to_numpy(),
            "hk_connect_in_date": pd.to_datetime(pd.Series([pd.NaT] * count)),
            "hk_connect_out_date": pd.to_datetime(pd.Series([pd.NaT] * count)),
        }
    )
    out = out[out["name"].notna() & (out["name"] != "")]
    out = out.drop_duplicates(subset=["code", "market"]).reset_index(drop=True)
    return out


# 中行牌价列名（金额为「每 100 单位外币兑人民币」）。pair 采用 ISO 命名。
_BOC_FX_COLUMNS: dict[str, str] = {"HKD/CNY": "港元"}


def _map_fx_rate(raw: pd.DataFrame, pair: str, start: date, end: date) -> pd.DataFrame:
    column = _BOC_FX_COLUMNS.get(pair)
    if column is None or raw is None or raw.empty or column not in raw.columns:
        return empty_frame(Capability.FX_RATE)
    trade_date = pd.to_datetime(raw["日期"], errors="coerce")
    rate = pd.to_numeric(raw[column], errors="coerce") / 100.0
    mask = trade_date.between(pd.Timestamp(start), pd.Timestamp(end)) & (rate > 0)
    out = pd.DataFrame({"pair": pair, "trade_date": trade_date, "rate": rate})[mask].reset_index(
        drop=True
    )
    out = out.drop_duplicates(subset=["pair", "trade_date"]).reset_index(drop=True)
    out["rate"] = out["rate"].astype("float64")
    out["pair"] = out["pair"].astype("object")
    return out


class AkShareAdapter:
    """AkShare 数据适配器；仅覆盖港股财务/分红、港股通名单、汇率（见模块 docstring）。"""

    def __init__(
        self,
        source: AkShareSource | None = None,
        *,
        retries: int = 3,
        retry_wait: float = 1.0,
    ) -> None:
        self._source = source if source is not None else _default_source()
        self._retries = retries
        self._retry_wait = retry_wait

    @property
    def name(self) -> str:
        return "akshare"

    def capabilities(self) -> frozenset[Capability]:
        return frozenset(
            {
                Capability.STOCK_LIST,
                Capability.FINANCIAL,
                Capability.DIVIDEND,
                Capability.FX_RATE,
            }
        )

    def _require(self, capability: Capability) -> None:
        if capability not in self.capabilities():
            msg = f"[{self.name}] 不支持能力 {capability.value}"
            raise UnsupportedCapabilityError(msg)

    def _call(self, label: str, call: Callable[[], pd.DataFrame]) -> pd.DataFrame:
        return _with_retry(call, retries=self._retries, wait=self._retry_wait, label=label)

    # ---------------- 已声明能力 ----------------

    def fetch_stock_list(self, market: Market | None = None) -> pd.DataFrame:
        self._require(Capability.STOCK_LIST)
        if market is not None and market != "HK":
            return empty_frame(Capability.STOCK_LIST)
        try:
            raw = self._call("stock_hk_ggt_components_em", self._source.stock_hk_ggt_components_em)
        except DataSourceError:
            raise
        except Exception as exc:
            msg = f"[{self.name}] stock_hk_ggt_components_em 异常: {exc}"
            raise DataSourceError(msg) from exc
        return _map_stock_list(raw)

    def fetch_financial(self, code: str, periods: list[date] | None = None) -> pd.DataFrame:
        self._require(Capability.FINANCIAL)
        if not _is_valid_code(code):
            return empty_frame(Capability.FINANCIAL)
        try:
            indicator = self._call(
                "stock_financial_hk_analysis_indicator_em",
                lambda: self._source.stock_financial_hk_analysis_indicator_em(
                    symbol=code, indicator="年度"
                ),
            )
            income = self._call(
                "stock_financial_hk_report_em",
                lambda: self._source.stock_financial_hk_report_em(
                    stock=code, symbol="利润表", indicator="年度"
                ),
            )
            balance = self._call(
                "stock_financial_hk_report_em",
                lambda: self._source.stock_financial_hk_report_em(
                    stock=code, symbol="资产负债表", indicator="年度"
                ),
            )
            cashflow = self._call(
                "stock_financial_hk_report_em",
                lambda: self._source.stock_financial_hk_report_em(
                    stock=code, symbol="现金流量表", indicator="年度"
                ),
            )
        except DataSourceError:
            raise
        except Exception as exc:
            msg = f"[{self.name}] 港股财务取数/映射异常({code}): {exc}"
            raise DataSourceError(msg) from exc
        return _map_financial(code, indicator, income, balance, cashflow, periods)

    def fetch_dividend(self, code: str) -> pd.DataFrame:
        self._require(Capability.DIVIDEND)
        if not _is_valid_code(code):
            return empty_frame(Capability.DIVIDEND)
        try:
            raw = self._call(
                "stock_hk_dividend_payout_em",
                lambda: self._source.stock_hk_dividend_payout_em(symbol=code),
            )
        except DataSourceError:
            raise
        except Exception as exc:
            msg = f"[{self.name}] stock_hk_dividend_payout_em 异常({code}): {exc}"
            raise DataSourceError(msg) from exc
        return _map_dividend(code, raw)

    def fetch_fx_rate(self, pair: str, start: date, end: date) -> pd.DataFrame:
        self._require(Capability.FX_RATE)
        if pair not in _BOC_FX_COLUMNS:
            return empty_frame(Capability.FX_RATE)
        try:
            raw = self._call("currency_boc_safe", self._source.currency_boc_safe)
        except DataSourceError:
            raise
        except Exception as exc:
            msg = f"[{self.name}] currency_boc_safe 异常: {exc}"
            raise DataSourceError(msg) from exc
        return _map_fx_rate(raw, pair, start, end)

    # ---------------- 未声明能力：必须显式抛错 ----------------

    def fetch_daily_bar(self, code: str, start: date, end: date) -> pd.DataFrame:
        self._require(Capability.DAILY_BAR)
        return empty_frame(Capability.DAILY_BAR)  # pragma: no cover - 声明后才会执行

    def fetch_disclosure(self, code: str, start: date, end: date) -> pd.DataFrame:
        self._require(Capability.DISCLOSURE)
        return empty_frame(Capability.DISCLOSURE)  # pragma: no cover

    def fetch_hk_connect_history(self) -> pd.DataFrame:
        self._require(Capability.HK_CONNECT_HISTORY)
        return empty_frame(Capability.HK_CONNECT_HISTORY)  # pragma: no cover
