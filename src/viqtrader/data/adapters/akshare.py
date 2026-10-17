"""AkShare 数据适配器（VIQTrader P1.4）。

数据源分工硬约束（设计文档第六节）：
- QMT 负责 A股+港股通行情、A股财务 5 表、交易执行；
- AkShare/Tushare 负责 港股财务、公告、港股通名单、汇率、A股分红、行业映射与指数行情。

因此本适配器**不声明** A股行情能力，只覆盖 AkShare 侧（能力集合均由实测决定）：

- FINANCIAL  <- stock_financial_hk_report_em（三大报表）+
               stock_financial_hk_analysis_indicator_em，港股年度财务，按报告期合并
- DIVIDEND   <- stock_hk_dividend_payout_em（港股）+
               stock_dividend_cninfo（A股，巨潮），按代码位数路由市场
- FX_RATE    <- currency_boc_safe，中行外汇牌价（取「港元」列）
- STOCK_LIST <- stock_hk_ggt_components_em，港股通成份股名单
- INDUSTRY_MAP <- stock_board_industry_name_em + stock_board_industry_cons_em，
                 东财行业板块（约 86 个行业逐个取成份股，节流防限流），回填 dim_stock.industry
- INDEX_BAR    <- index_zh_a_hist，东财指数日线（如沪深300），入 fact_index_bar
- SHARE_STRUCTURE <- 东财 datacenter-web RPT_F10_EH_EQUITY（F10 股本变动历史，
                 urllib 标准库直连全量分页），入 fact_share_structure；
                 估值 market_cap/ps_ttm 的总股本来源

未声明能力（实测确认无可靠源，调用时抛 UnsupportedCapabilityError）：
- DAILY_BAR：属 QMT 分工。
- DISCLOSURE：A股公告有 stock_individual_notice_report，但**查无港股公告源**；
  能力在契约中不区分市场，若声明则港股标的会被静默返回空帧（等价假数据），故不声明。
- HK_CONNECT_HISTORY：AkShare 无港股通名单变动流水（IN/OUT）历史源。

金额/比率口径（源数据未做无依据换算）：
- 港股财报金额为源报表口径原值（东财 CURRENCY 列标注币种，如腾讯为人民币）；
- 比率为东财口径百分数 / 100（如 GROSS_PROFIT_RATIO=56.21 → 0.5621）；
- 汇率为中行牌价「每 100 单位外币兑人民币」，港元列需 / 100 得 HKD/CNY；
- A股分红「派息比例」为每 10 股派息（含税），/ 10 得每股税前现金（估值层 A股不扣税）。
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import date
from typing import Any, Protocol, cast, runtime_checkable

import pandas as pd

from ..base import (
    SCHEMAS,
    Capability,
    DataSourceError,
    Market,
    UnsupportedCapabilityError,
    empty_frame,
)

__all__ = [
    "AkShareAdapter",
    "AkShareSource",
    "DatacenterEquityClient",
    "EquityReportClient",
]

logger = logging.getLogger(__name__)


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

    def stock_dividend_cninfo(self, symbol: str) -> pd.DataFrame: ...

    def stock_hk_ggt_components_em(self) -> pd.DataFrame: ...

    def currency_boc_safe(self) -> pd.DataFrame: ...

    def stock_board_industry_name_em(self) -> pd.DataFrame: ...

    def stock_board_industry_cons_em(self, symbol: str) -> pd.DataFrame: ...

    def index_zh_a_hist(
        self, symbol: str, period: str, start_date: str, end_date: str
    ) -> pd.DataFrame: ...


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


# ---------------- 东财 datacenter-web 直连（股本结构，不属 akshare 库函数） ----------------

_DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_DATACENTER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
_SHARE_PAGE_SIZE = 500  # datacenter 单页上限（实测更大会被截断）
_SHARE_PAGE_INTERVAL = 0.2  # 页间限流间隔（秒），全量约 650 页


@runtime_checkable
class EquityReportClient(Protocol):
    """东财 datacenter-web F10 报表取数缝隙：默认 urllib 直连，测试可注入假实现。

    fetch_page 返回该页原始记录（dict 列表，字段为源大写命名）；任何失败
    （网络/限流/响应异常）重试穷尽后抛 DataSourceError。
    """

    def fetch_page(self, page_number: int, page_size: int) -> list[dict[str, Any]]: ...


class DatacenterEquityClient:
    """东财 datacenter-web RPT_F10_EH_EQUITY（股本变动历史）分页客户端。

    标准库 urllib 直连（与 LLM 通道同先例，不引入新依赖）；必须带浏览器 UA
    （实测缺失时服务端断连）。每页自带重试，UA/参数不进入任何持久化存储。
    """

    def __init__(self, *, retries: int = 3, retry_wait: float = 1.0, timeout: float = 30.0) -> None:
        self._retries = retries
        self._retry_wait = retry_wait
        self._timeout = timeout

    def fetch_page(self, page_number: int, page_size: int) -> list[dict[str, Any]]:
        params = {
            "reportName": "RPT_F10_EH_EQUITY",
            "columns": "ALL",
            "pageSize": page_size,
            "pageNumber": page_number,
            "sortColumns": "SECUCODE",
            "sortTypes": 1,
            "source": "HSF10",
            "client": "PC",
        }
        url = f"{_DATACENTER_URL}?{urllib.parse.urlencode(params)}"
        last: Exception | None = None
        for _attempt in range(max(1, self._retries)):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": _DATACENTER_UA})
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                result = payload.get("result")
                if payload.get("success") != 1 or not isinstance(result, dict):
                    raise ValueError(
                        f"datacenter 返回异常: code={payload.get('code')} "
                        f"message={payload.get('message')}"
                    )
                data = result.get("data") or []
                return [row for row in data if isinstance(row, dict)]
            except Exception as exc:  # 网络/解析/响应异常统一重试，穷尽后收口
                last = exc
                if self._retry_wait:
                    time.sleep(self._retry_wait)
        msg = (
            f"datacenter RPT_F10_EH_EQUITY 第 {page_number} 页拉取失败"
            f"（重试 {self._retries} 次）: {last}"
        )
        raise DataSourceError(msg) from last


# ---------------- 原始表 -> 标准化表（纯函数，供离线单测直接验证） ----------------

_FINANCIAL_COLUMNS = SCHEMAS[Capability.FINANCIAL].columns
_DIVIDEND_COLUMNS = SCHEMAS[Capability.DIVIDEND].columns


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


def _map_dividend_a(code: str, raw: pd.DataFrame) -> pd.DataFrame:
    """巨潮 A股分红配送（stock_dividend_cninfo）-> fact_dividend。

    口径（源数据未做无依据换算）：
    - 「派息比例」为每 10 股派息（含税），/10 得每股现金；A 股取**税前**值——
      估值层对 A股不扣税、仅对 HK 扣 20%，故此处保留含税原值，与港股分支区分；
    - 「除权日」-> ex_date、「派息日」-> pay_date；
    - 现金派息记为 dividend_type="现金"（估值层 _prepare_dividend 仅取该类型），
      纯送转（送股/转增）记为 "送转"（保留记录，不参与股息率）。

    与港股分支的差异：巨潮表无「分配类型」文本，故按有无现金派息判定类型；
    缺除权日的未实施预案（除权日为空）与零派息记录一律丢弃。
    """
    if raw is None or raw.empty or "除权日" not in raw.columns:
        return empty_frame(Capability.DIVIDEND)

    def _dt(name: str) -> pd.Series:
        if name not in raw.columns:
            return pd.Series(pd.NaT, index=raw.index, dtype="datetime64[ns]")
        return pd.to_datetime(raw[name], errors="coerce")

    def _num(name: str) -> pd.Series:
        if name not in raw.columns:
            return pd.Series(pd.NA, index=raw.index, dtype="float64")
        return pd.to_numeric(raw[name], errors="coerce")

    ex_date = _dt("除权日")
    pay_date = _dt("派息日")
    per_10 = _num("派息比例")
    stock = _num("送股比例").fillna(0.0) + _num("转增比例").fillna(0.0)

    frames: list[pd.DataFrame] = []
    cash_mask = ex_date.notna() & per_10.notna() & (per_10 > 0)
    if cash_mask.any():
        frames.append(
            pd.DataFrame(
                {
                    "code": code,
                    "ex_date": ex_date[cash_mask],
                    "pay_date": pay_date[cash_mask],
                    "dividend_per_share": per_10[cash_mask].to_numpy() / 10.0,
                    "dividend_type": "现金",
                }
            )
        )
    stock_mask = ex_date.notna() & (stock > 0)
    if stock_mask.any():
        frames.append(
            pd.DataFrame(
                {
                    "code": code,
                    "ex_date": ex_date[stock_mask],
                    "pay_date": pay_date[stock_mask],
                    "dividend_per_share": pd.Series(
                        float("nan"), index=ex_date[stock_mask].index, dtype="float64"
                    ).to_numpy(),
                    "dividend_type": "送转",
                }
            )
        )
    if not frames:
        return empty_frame(Capability.DIVIDEND)

    out = pd.concat(frames, ignore_index=True)
    out = out.drop_duplicates(subset=["code", "ex_date", "dividend_type"]).reset_index(drop=True)
    out = out.sort_values("ex_date").reset_index(drop=True)
    out["dividend_per_share"] = out["dividend_per_share"].astype("float64")
    out["code"] = out["code"].astype("object")
    out["dividend_type"] = out["dividend_type"].astype("object")
    return out[list(_DIVIDEND_COLUMNS)]


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


_INDUSTRY_MAP_COLUMNS = SCHEMAS[Capability.INDUSTRY_MAP].columns
_INDEX_BAR_COLUMNS = SCHEMAS[Capability.INDEX_BAR].columns
_SHARE_STRUCTURE_COLUMNS = SCHEMAS[Capability.SHARE_STRUCTURE].columns


def _map_industry_map(parts: list[tuple[str, pd.DataFrame]]) -> pd.DataFrame:
    """（行业名, 成份股原始帧）列表 -> 全市场 code->industry 映射。

    东财行业板块成份股列「代码」；同代码出现在多个板块时保留先出现者。
    """
    frames: list[pd.DataFrame] = []
    for industry, raw in parts:
        if raw is None or raw.empty or "代码" not in raw.columns:
            continue
        code = raw["代码"].astype("string").str.strip()
        frame = pd.DataFrame({"code": code, "industry": industry})
        frame = frame[frame["code"].notna() & (frame["code"] != "")]
        frames.append(frame)
    if not frames:
        return empty_frame(Capability.INDUSTRY_MAP)
    out = pd.concat(frames, ignore_index=True)
    out = out.drop_duplicates(subset=["code"]).reset_index(drop=True)
    out["code"] = out["code"].astype("object")
    out["industry"] = out["industry"].astype("object")
    return out[list(_INDUSTRY_MAP_COLUMNS)]


def _map_index_bar(code: str, raw: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
    """东财指数日线（index_zh_a_hist）-> fact_index_bar。

    源列：日期/开盘/收盘/最高/最低/成交量/成交额；指数无复权概念，不做额外处理。
    """
    if raw is None or raw.empty or "日期" not in raw.columns:
        return empty_frame(Capability.INDEX_BAR)

    def _num(name: str) -> pd.Series:
        if name not in raw.columns:
            return pd.Series(pd.NA, index=raw.index, dtype="float64")
        return pd.to_numeric(raw[name], errors="coerce")

    out = pd.DataFrame(
        {
            "code": code,
            "trade_date": pd.to_datetime(raw["日期"], errors="coerce"),
            "open": _num("开盘"),
            "high": _num("最高"),
            "low": _num("最低"),
            "close": _num("收盘"),
            "volume": _num("成交量"),
            "amount": _num("成交额"),
        }
    )
    out = out.dropna(subset=["trade_date"])
    mask = out["trade_date"].between(pd.Timestamp(start), pd.Timestamp(end))
    out = out[mask]
    out = out.drop_duplicates(subset=["code", "trade_date"]).sort_values("trade_date")
    out = out.reset_index(drop=True)
    out["code"] = out["code"].astype("object")
    return out[list(_INDEX_BAR_COLUMNS)]


def _map_share_structure(pages: list[list[dict[str, Any]]]) -> pd.DataFrame:
    """datacenter RPT_F10_EH_EQUITY 原始记录页列表 -> fact_share_structure。

    口径（源数据未做无依据换算）：
    - code 取 SECURITY_CODE（6 位数字，不带市场后缀）；
    - change_date 取 END_DATE（股本结构时点）：股本快照为客观事实数据，
      时点已实际生效，与东财 F10 展示口径一致；NOTICE_DATE（公告日）缺失率
      不可控，不作对齐键；
    - total_shares / total_a_shares / listed_a_shares 为源字段股数原值；
    - TOTAL_SHARES 为空或非正的行（债券/指数等非股票噪声）丢弃；
    - 同 (code, change_date) 多条时保留最后一条（页序即源顺序）。
    """
    rows = [row for page in pages for row in page]
    if not rows:
        return empty_frame(Capability.SHARE_STRUCTURE)
    raw = pd.DataFrame(rows)
    if "SECURITY_CODE" not in raw.columns or "END_DATE" not in raw.columns:
        return empty_frame(Capability.SHARE_STRUCTURE)

    def _num(name: str) -> pd.Series:
        if name not in raw.columns:
            return pd.Series(pd.NA, index=raw.index, dtype="Float64")
        return pd.to_numeric(raw[name], errors="coerce")

    def _text(name: str) -> pd.Series:
        if name not in raw.columns:
            return pd.Series(pd.NA, index=raw.index, dtype="string")
        return raw[name].astype("string").str.strip()

    out = pd.DataFrame(
        {
            "code": raw["SECURITY_CODE"].astype("string").str.strip(),
            # format="mixed"：源同列混有 "2023-12-31" 与 "2023-12-31 00:00:00" 两种格式，
            # 默认首元素格式推断会把后者 coerce 成 NaT
            "change_date": pd.to_datetime(raw["END_DATE"], errors="coerce", format="mixed"),
            "total_shares": _num("TOTAL_SHARES"),
            "total_a_shares": _num("TOTAL_A_SHARES"),
            "listed_a_shares": _num("LISTED_A_SHARES"),
            "change_reason": _text("CHANGE_REASON"),
        }
    )
    out = out.dropna(subset=["code", "change_date"])
    out = out[out["code"].str.fullmatch(r"\d{6}", na=False)]
    out = out[out["total_shares"].notna() & (out["total_shares"] > 0)]
    out = out.drop_duplicates(subset=["code", "change_date"], keep="last").reset_index(drop=True)
    out["total_shares"] = out["total_shares"].astype("float64")
    out["total_a_shares"] = out["total_a_shares"].astype("float64")
    out["listed_a_shares"] = out["listed_a_shares"].astype("float64")
    out["code"] = out["code"].astype("object")
    out["change_reason"] = out["change_reason"].astype("object")
    return out[list(_SHARE_STRUCTURE_COLUMNS)]


class AkShareAdapter:
    """AkShare/东财 数据适配器；覆盖港股财务/分红、港股通名单、汇率、行业、指数与股本结构。

    各能力的数据源映射见模块 docstring。
    """

    def __init__(
        self,
        source: AkShareSource | None = None,
        *,
        retries: int = 3,
        retry_wait: float = 1.0,
        industry_interval: float = 0.3,
        equity_client: EquityReportClient | None = None,
        share_interval: float = _SHARE_PAGE_INTERVAL,
        dividend_interval: float = 0.3,
    ) -> None:
        self._source = source if source is not None else _default_source()
        self._retries = retries
        self._retry_wait = retry_wait
        self._industry_interval = industry_interval
        self._equity_client: EquityReportClient = (
            equity_client
            if equity_client is not None
            else DatacenterEquityClient(retries=retries, retry_wait=retry_wait)
        )
        self._share_interval = share_interval
        self._dividend_interval = dividend_interval

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
                Capability.INDUSTRY_MAP,
                Capability.INDEX_BAR,
                Capability.SHARE_STRUCTURE,
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
        # 代码位数即市场：A股 6 位走巨潮分红配送，港股 5 位走东财港股派息
        if len(code) == 6:
            return self._fetch_dividend_a(code)
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

    def _fetch_dividend_a(self, code: str) -> pd.DataFrame:
        """A股分红：巨潮 stock_dividend_cninfo（交易所/巨潮口径，含派息日）。"""
        # 全市场逐只同步时限流防巨潮封禁（与 industry/share 节奏控制同先例）
        if self._dividend_interval:
            time.sleep(self._dividend_interval)
        try:
            raw = self._call(
                "stock_dividend_cninfo",
                lambda: self._source.stock_dividend_cninfo(symbol=code),
            )
        except DataSourceError:
            raise
        except Exception as exc:
            msg = f"[{self.name}] stock_dividend_cninfo 异常({code}): {exc}"
            raise DataSourceError(msg) from exc
        return _map_dividend_a(code, raw)

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

    def fetch_industry_map(self) -> pd.DataFrame:
        self._require(Capability.INDUSTRY_MAP)
        boards = self._call(
            "stock_board_industry_name_em", self._source.stock_board_industry_name_em
        )
        if boards is None or boards.empty or "板块名称" not in boards.columns:
            msg = f"[{self.name}] stock_board_industry_name_em 返回为空或缺少「板块名称」列"
            raise DataSourceError(msg)
        board_names = boards["板块名称"].astype("string").dropna().str.strip()
        names: list[str] = board_names[board_names != ""].drop_duplicates().tolist()
        # 约 86 个行业逐个取成份股，行业映射是归因行业段正确性的前提：
        # 任一行业重试后仍失败即整体报错（静默缺行业比失败重跑更危险）。
        parts: list[tuple[str, pd.DataFrame]] = []
        for i, industry in enumerate(names):
            if i:
                time.sleep(self._industry_interval)

            def _fetch_cons(name: str = industry) -> pd.DataFrame:
                return self._source.stock_board_industry_cons_em(symbol=name)

            raw = self._call(f"stock_board_industry_cons_em({industry})", _fetch_cons)
            parts.append((industry, raw))
        return _map_industry_map(parts)

    def fetch_share_structure(self) -> pd.DataFrame:
        """全市场股本结构变动历史：东财 F10 RPT_F10_EH_EQUITY 全量分页拉取。

        约 32 万条记录 / 650 页（pageSize=500），页间限流防限流；单页重试由
        DatacenterEquityClient 负责，任一页最终失败即整体报错（调用方不落库）。
        """
        self._require(Capability.SHARE_STRUCTURE)
        pages: list[list[dict[str, Any]]] = []
        page = 1
        while True:
            rows = self._equity_client.fetch_page(page, _SHARE_PAGE_SIZE)
            pages.append(rows)
            if len(rows) < _SHARE_PAGE_SIZE:  # 末页不足一页即结束
                break
            page += 1
            if page % 50 == 0:
                logger.info("[akshare] 股本结构已拉取 %d 页", page)
            if self._share_interval:
                time.sleep(self._share_interval)
        return _map_share_structure(pages)

    def fetch_index_bar(self, code: str, start: date, end: date) -> pd.DataFrame:
        self._require(Capability.INDEX_BAR)
        if not (code.isdigit() and len(code) == 6):
            return empty_frame(Capability.INDEX_BAR)
        try:
            raw = self._call(
                f"index_zh_a_hist({code})",
                lambda: self._source.index_zh_a_hist(
                    symbol=code,
                    period="daily",
                    start_date=start.strftime("%Y%m%d"),
                    end_date=end.strftime("%Y%m%d"),
                ),
            )
        except DataSourceError:
            raise
        except Exception as exc:
            msg = f"[{self.name}] index_zh_a_hist 异常({code}): {exc}"
            raise DataSourceError(msg) from exc
        return _map_index_bar(code, raw, start, end)

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
