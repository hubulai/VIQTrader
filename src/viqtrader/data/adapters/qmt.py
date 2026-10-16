"""QMT（迅投 miniQMT）数据适配器（VIQTrader P1.3）。

数据源分工硬约束（设计文档第六节）：
- QMT 负责 A股+港股通行情、A股财务 5 表、交易执行；
- AkShare/Tushare 负责港股财务、分红、港股通名单、汇率。

本适配器声明能力：
- DAILY_BAR  <- xtdata.download_history_data + get_market_data_ex（A股/港股通日线，
               原始价 + front_ratio 推导复权因子）
- FINANCIAL  <- xtdata.download_financial_data2（异步回调）+ get_financial_data，
               5 表 Balance/Income/CashFlow/PershareIndex/Capital，
               report_type='report_time'（实测纪律：必须异步接口 + report_time，
               否则数据为空；PershareIndex 提供 ROE/EPS/毛利率等核心指标）
- STOCK_LIST <- xtdata.get_stock_list_in_sector（'沪深京A股'，覆盖 SH/SZ/BJ）。
               实测终端无港股通成份板块（'香港联交所股票' 为全部港股非港股通名单），
               港股通成份名单按分工归 AkShare，market='HK' 返回空帧。

未声明能力（按分工归 AkShare，调用抛 UnsupportedCapabilityError）：
- DIVIDEND / DISCLOSURE / FX_RATE / HK_CONNECT_HISTORY

运行前提：
- miniQMT 终端已启动并登录（xtdata 通过本地端口与终端通信，无终端则连接失败）；
- xtquant SDK 可用（随终端或券商渠道分发，不入依赖清单；懒加载 import，
  离线单测通过 QMTSource 假实现注入，无需 xtquant）。

代码口径：
- 契约 code 不带市场后缀；QMT 内部使用 '600519.SH' / '00700.HK' 格式，双向转换见
  to_qmt_code / from_qmt_code；6 位 A股按号段推断市场（60/68→SH，00/30→SZ，4/8/92→BJ）。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import date
from typing import Protocol, cast, runtime_checkable

import pandas as pd

from ..base import (
    Capability,
    DataSourceError,
    Market,
    UnsupportedCapabilityError,
    empty_frame,
)

__all__ = ["QMTAdapter", "QMTSource", "from_qmt_code", "infer_market", "to_qmt_code"]


# ---------------------------------------------------------------------------
# 代码与市场转换
# ---------------------------------------------------------------------------


def infer_market(code: str) -> Market:
    """按号段推断 6 位 A股 / 5 位港股的市场。"""
    if len(code) == 5 and code.isdigit():
        return "HK"
    if not (len(code) == 6 and code.isdigit()):
        raise DataSourceError(f"无法识别的代码: {code!r}")
    if code[:2] in ("60", "68") or code[:3] in ("601", "603", "605", "688"):
        return "SH"
    if code[:2] in ("00", "30") or code[:3] in ("001", "002", "003", "301"):
        return "SZ"
    if code[0] in ("4", "8") or code[:2] == "92":
        return "BJ"
    raise DataSourceError(f"无法按号段推断市场: {code!r}")


def to_qmt_code(code: str, market: Market | None = None) -> str:
    """契约 code → QMT 代码（'600519.SH' / '00700.HK'）。"""
    m = market or infer_market(code)
    return f"{code}.{m}"


def from_qmt_code(qmt_code: str) -> tuple[str, Market]:
    """QMT 代码 → (契约 code, market)。"""
    code, _, suffix = qmt_code.partition(".")
    if suffix not in ("SH", "SZ", "BJ", "HK"):
        raise DataSourceError(f"无法识别的 QMT 代码: {qmt_code!r}")
    return code, cast(Market, suffix)


# ---------------------------------------------------------------------------
# 取数缝隙 Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class QMTSource(Protocol):
    """取数缝隙：默认指向 xtquant.xtdata，测试可注入假实现离线验证映射。

    方法签名对齐 xtdata 同名函数（仅保留本适配器用到的参数）。
    """

    def download_history_data(
        self, stock_code: str, period: str, start_time: str, end_time: str
    ) -> None: ...

    def get_market_data_ex(
        self,
        field_list: list[str],
        stock_list: list[str],
        period: str,
        start_time: str,
        end_time: str,
        count: int,
        dividend_type: str,
        fill_data: bool,
    ) -> dict[str, pd.DataFrame]: ...

    def download_financial_data2(
        self,
        stock_list: list[str],
        table_list: list[str],
        start_time: str,
        end_time: str,
        callback: Callable[[object], None],
    ) -> None: ...

    def get_financial_data(
        self,
        stock_list: list[str],
        table_list: list[str],
        start_time: str,
        end_time: str,
        report_type: str,
    ) -> dict[str, dict[str, pd.DataFrame]]: ...

    def get_stock_list_in_sector(self, sector_name: str) -> list[str]: ...


def _default_source() -> QMTSource:
    """默认取数源 = xtquant.xtdata（懒加载；无 SDK 时报清晰错误）。"""
    try:
        from xtquant import xtdata
    except ImportError as exc:
        raise DataSourceError(
            "xtquant SDK 不可用：请安装随 miniQMT 终端分发的 xtquant 包，并确认终端已启动登录。"
        ) from exc
    return cast(QMTSource, xtdata)


# ---------------------------------------------------------------------------
# 映射：日线行情
# ---------------------------------------------------------------------------

_BAR_FIELDS = ["open", "high", "low", "close", "volume", "amount"]


def _map_daily_bar(raw: pd.DataFrame, ratio: pd.DataFrame | None, code: str) -> pd.DataFrame:
    """QMT get_market_data_ex 输出 → fact_daily_bar schema。

    raw / ratio 的行索引为时间戳（QMT 返回毫秒 epoch 或 'YYYYMMDD' 字符串，
    两种均兼容）；dividend_type='front_ratio' 的 close 与原始 close 之比即复权因子。
    """
    if raw.empty:
        return empty_frame(Capability.DAILY_BAR)
    # 先从原始索引解析日期，再重置为 RangeIndex，避免构造 DataFrame 时对齐膨胀
    trade_date = _parse_qmt_index(raw.index)
    raw = raw.reset_index(drop=True)
    frame = pd.DataFrame(
        {
            "code": code,
            "trade_date": trade_date,
            "open": pd.to_numeric(raw["open"], errors="coerce"),
            "high": pd.to_numeric(raw["high"], errors="coerce"),
            "low": pd.to_numeric(raw["low"], errors="coerce"),
            "close": pd.to_numeric(raw["close"], errors="coerce"),
            "volume": pd.to_numeric(raw["volume"], errors="coerce"),
            "amount": pd.to_numeric(raw["amount"], errors="coerce"),
        }
    )
    if ratio is not None and not ratio.empty and "close" in ratio.columns:
        ratio_close = pd.to_numeric(ratio["close"].reset_index(drop=True), errors="coerce")
        factor = ratio_close / frame["close"]
        frame["adj_factor"] = factor.replace([float("inf"), float("-inf")], pd.NA)
    else:
        frame["adj_factor"] = pd.NA
    frame["turnover_rate"] = pd.NA  # QMT 日线无换手率字段，估值引擎不依赖
    return frame.reset_index(drop=True)


def _parse_qmt_index(index: pd.Index) -> pd.Series:
    """QMT 行索引兼容：毫秒 epoch（>=1e11）/ YYYYMMDD 整数 / 日期字符串。"""
    if pd.api.types.is_integer_dtype(index):
        if len(index) and index[0] >= 10**11:  # 毫秒 epoch
            return pd.Series(
                pd.to_datetime(index, unit="ms").to_numpy(),
                index=range(len(index)),
            )
        return pd.Series(  # YYYYMMDD 整数按字符串解析
            pd.to_datetime(index.astype(str), format="%Y%m%d").to_numpy(),
            index=range(len(index)),
        )
    return pd.Series(pd.to_datetime(index.astype(str)).to_numpy(), index=range(len(index)))


# ---------------------------------------------------------------------------
# 映射：A股财务 5 表
# ---------------------------------------------------------------------------

# QMT 字段名 → fact_financial 列名。
# 字段名经光大金阳光 QMT（xtdata get_financial_data, report_type='report_time'）
# 2026-10-15 实测确认：英文 snake_case；行索引为 RangeIndex，报告期在 m_timetag，
# 公告日在 m_anntime，均为 'YYYYMMDD' 字符串。
_FINANCIAL_FIELD_MAP: dict[str, dict[str, str]] = {
    "Income": {
        "revenue": "revenue",
        "net_profit_excl_min_int_inc": "net_profit",  # 归母净利润
    },
    "Balance": {
        "tot_assets": "total_assets",
        "total_equity": "total_equity",
    },
    "CashFlow": {
        "net_cash_flows_oper_act": "ocf",
        "cash_pay_acq_const_fiolta": "capex",  # 购建固定/无形/其他长期资产支付现金
    },
    "PershareIndex": {
        "s_fa_eps_basic": "eps",
        "s_fa_bps": "bps",
        "equity_roe": "roe",  # 加权 ROE
        "sales_gross_profit": "gross_margin",
        "inc_net_profit_rate": "net_margin",
        "gear_ratio": "debt_ratio",  # 资产负债率
    },
}
# 注：PershareIndex 无直接 ROA 字段，roa 留空（如需后续自算）。

_RATIO_FIELDS = {"roe", "gross_margin", "net_margin", "debt_ratio", "roa"}


def _map_financial(tables: dict[str, pd.DataFrame], code: str) -> pd.DataFrame:
    """QMT 5 表输出 → fact_financial schema。

    report_type='report_time' 下：m_timetag=报告期，m_anntime=公告日（point-in-time
    关键）；比率字段为百分数口径（如 ROE=34.19），统一 / 100 存小数。
    利润/现金流/每股收益为年初至今累计口径，本函数折算为单季落库——
    平台契约统一单季口径，估值层 rolling(4) 直接得 TTM。
    """
    pieces: list[pd.DataFrame] = []
    for table_name, field_map in _FINANCIAL_FIELD_MAP.items():
        raw = tables.get(table_name)
        if raw is None or raw.empty:
            continue
        piece = _extract_financial_table(raw, field_map)
        if piece is not None:
            pieces.append(piece)
    if not pieces:
        return empty_frame(Capability.FINANCIAL)

    # 同表同报告期多版本（原披露 + 调整后）：先按报告期去重，保留公告日最新一行
    # （na_position='first' 保证有公告日的版本胜出）；跨表公告日可能不一致，
    # 按 report_period 合并、公告日取各表最晚值——任何字段的调整版本都在该日
    # 之后才可见，回测按 ann_date 过滤时保守无前视，且保证主键唯一。
    deduped = [
        p.sort_values(["report_period", "ann_date"], na_position="first").drop_duplicates(
            subset=["report_period"], keep="last"
        )
        for p in pieces
    ]
    merged = deduped[0]
    for piece in deduped[1:]:
        merged = merged.merge(piece, on=["report_period"], how="outer", suffixes=("", "_r"))
        if "ann_date_r" in merged.columns:
            merged["ann_date"] = merged[["ann_date", "ann_date_r"]].max(axis=1)
            merged = merged.drop(columns=["ann_date_r"])
    merged = merged.sort_values("report_period").reset_index(drop=True)

    merged.insert(0, "code", code)
    # QMT 利润/现金流/每股收益为年初至今累计口径 → 平台契约统一为单季：
    # 单季 = 本期累计 - 上一报告期累计（须报告期为上一季度）；Q1 单季 = Q1 累计，
    # 跨年 Q1 不差分。派生行可见性 = 本期公告日（累计披露总晚于上期，无前视）。
    # 缺上一期时保守保留累计原值（宁缺勿错），估值层 TTM 不受污染。
    flow_cols = [c for c in ("revenue", "net_profit", "ocf", "eps") if c in merged.columns]
    if "capex" in merged.columns:
        flow_cols.append("capex")
    merged = merged.sort_values(["code", "report_period"]).reset_index(drop=True)
    prev_period = merged.groupby("code", sort=False)["report_period"].shift(1)
    prev_vals = merged.groupby("code", sort=False)[flow_cols].shift(1)
    cur_year, cur_qtr = merged["report_period"].dt.year, merged["report_period"].dt.quarter
    prev_year, prev_qtr = prev_period.dt.year, prev_period.dt.quarter
    # 按（年, 季度）判定上一报告期恰好相邻——日期减 3 个月会因季末日不等（06-30→03-30）误判
    contiguous = ((cur_year == prev_year) & (cur_qtr == prev_qtr + 1)) | (
        (cur_year == prev_year + 1) & (cur_qtr == 1) & (prev_qtr == 4)
    )
    not_q1 = cur_qtr != 1
    for col in flow_cols:
        merged[col] = (merged[col] - prev_vals[col]).where(contiguous & not_q1, merged[col])
    if "capex" in merged.columns:
        merged["fcf"] = merged["ocf"] - merged["capex"]
        merged = merged.drop(columns=["capex"])
    else:
        merged["fcf"] = pd.NA
    for col in _RATIO_FIELDS:
        if col in merged.columns:
            merged[col] = pd.to_numeric(merged[col], errors="coerce") / 100.0

    schema_cols = [
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
    ]
    for col in schema_cols:
        if col not in merged.columns:
            merged[col] = pd.NA
    return merged[schema_cols]


def _extract_financial_table(raw: pd.DataFrame, field_map: dict[str, str]) -> pd.DataFrame | None:
    """单表抽取：m_timetag 报告期 + m_anntime 公告日，重命名目标字段。"""
    if "m_timetag" not in raw.columns or "m_anntime" not in raw.columns:
        return None
    out = pd.DataFrame(
        {
            "report_period": pd.to_datetime(
                raw["m_timetag"].astype(str), format="%Y%m%d", errors="coerce"
            ),
            "ann_date": pd.to_datetime(
                raw["m_anntime"].astype(str), format="%Y%m%d", errors="coerce"
            ),
        }
    )
    for src, dst in field_map.items():
        out[dst] = pd.to_numeric(raw[src], errors="coerce") if src in raw.columns else pd.NA
    return out


# ---------------------------------------------------------------------------
# 映射：标的主数据
# ---------------------------------------------------------------------------


def _map_stock_list(qmt_codes: list[str], is_hk_connect: bool) -> pd.DataFrame:
    """QMT 板块成份代码列表 → dim_stock schema（名称/行业/上市日 QMT 侧不取，留空）。"""
    rows = []
    for qc in qmt_codes:
        code, market = from_qmt_code(qc)
        rows.append(
            {
                "code": code,
                "market": market,
                "name": pd.NA,
                "industry": pd.NA,
                "list_date": pd.NaT,
                "is_hk_connect": is_hk_connect,
                "hk_connect_in_date": pd.NaT,
                "hk_connect_out_date": pd.NaT,
            }
        )
    if not rows:
        return empty_frame(Capability.STOCK_LIST)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 适配器
# ---------------------------------------------------------------------------

# 实测：'沪深京A股' 覆盖 SH/SZ/BJ（'沪深A股' 不含北交所）。
# 终端无「港股通」板块（get_sector_list 中只有 '香港联交所股票' = 全部港股 2863 只，
# 并非港股通成份名单）→ 港股通名单按分工由 AkShare 适配器提供，QMT 不返回 HK 主数据。
_A_SECTOR = "沪深京A股"
_FINANCIAL_TABLES = ["Balance", "Income", "CashFlow", "PershareIndex", "Capital"]


class QMTAdapter:
    """QMT 数据源适配器。所有取数经 QMTSource 缝隙，默认懒加载 xtquant.xtdata。"""

    def __init__(
        self,
        source: QMTSource | None = None,
        *,
        max_retries: int = 3,
        retry_backoff: float = 2.0,
        financial_timeout_s: float = 120.0,
    ) -> None:
        self._source = source
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff
        self._financial_timeout_s = financial_timeout_s

    @property
    def name(self) -> str:
        return "qmt"

    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.DAILY_BAR, Capability.FINANCIAL, Capability.STOCK_LIST})

    # -- 内部 ----------------------------------------------------------

    def _src(self) -> QMTSource:
        if self._source is None:
            self._source = _default_source()
        return self._source

    def _call(self, fn: Callable[[], pd.DataFrame]) -> pd.DataFrame:
        last: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                return fn()
            except UnsupportedCapabilityError:
                raise
            except Exception as exc:
                last = exc
                if attempt < self._max_retries - 1:
                    time.sleep(self._retry_backoff * (attempt + 1))
        raise DataSourceError(f"QMT 取数失败（重试 {self._max_retries} 次）: {last}")

    # -- 契约方法 --------------------------------------------------------

    def fetch_daily_bar(self, code: str, start: date, end: date) -> pd.DataFrame:
        qmt_code = to_qmt_code(code)
        s, e = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")

        def _fetch() -> pd.DataFrame:
            src = self._src()
            src.download_history_data(qmt_code, "1d", s, e)
            raw_map = src.get_market_data_ex(
                _BAR_FIELDS,
                [qmt_code],
                period="1d",
                start_time=s,
                end_time=e,
                count=-1,
                dividend_type="none",
                fill_data=False,
            )
            ratio_map = src.get_market_data_ex(
                ["close"],
                [qmt_code],
                period="1d",
                start_time=s,
                end_time=e,
                count=-1,
                dividend_type="front_ratio",
                fill_data=False,
            )
            raw = raw_map.get(qmt_code, pd.DataFrame())
            ratio = ratio_map.get(qmt_code)
            return _map_daily_bar(raw, ratio, code)

        return self._call(_fetch)

    def fetch_financial(self, code: str, periods: list[date] | None = None) -> pd.DataFrame:
        if infer_market(code) == "HK":
            raise UnsupportedCapabilityError("港股财务按分工归 AkShare 适配器")
        qmt_code = to_qmt_code(code)
        # 财务下载按时间范围（公告日口径）；periods 为空时默认近 5 年
        end = date.today()
        start = date(end.year - 5, 1, 1) if not periods else min(periods)
        s, e = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")

        def _fetch() -> pd.DataFrame:
            src = self._src()
            # 实测回调逐表触发，载荷 {'total': 5, 'finished': N}，等全部完成
            progress: dict[str, int] = {"finished": 0, "total": 0}

            def _cb(data: object) -> None:
                if isinstance(data, dict):
                    progress["finished"] = int(data.get("finished", 0))
                    progress["total"] = int(data.get("total", 0))

            # 注意：本版本 xtdata 的 download_financial_data2 无 report_type 参数，
            # report_type 只在 get_financial_data 侧指定
            src.download_financial_data2([qmt_code], _FINANCIAL_TABLES, s, e, _cb)
            deadline = time.monotonic() + self._financial_timeout_s
            while time.monotonic() < deadline:
                if progress["total"] and progress["finished"] >= progress["total"]:
                    break
                time.sleep(0.5)
            else:
                raise DataSourceError("QMT 财务数据下载超时（异步回调未完成）")
            tables = src.get_financial_data([qmt_code], _FINANCIAL_TABLES, s, e, "report_time").get(
                qmt_code, {}
            )
            frame = _map_financial(tables, code)
            if periods:
                keep = {pd.Timestamp(p) for p in periods}
                frame = frame[frame["report_period"].isin(keep)].reset_index(drop=True)
            return frame

        return self._call(_fetch)

    def fetch_stock_list(self, market: Market | None = None) -> pd.DataFrame:
        def _fetch() -> pd.DataFrame:
            if market == "HK":
                # xtdata 无港股通成份板块；港股通名单归 AkShare 适配器
                return empty_frame(Capability.STOCK_LIST)
            src = self._src()
            codes = src.get_stock_list_in_sector(_A_SECTOR)
            if market is not None:
                codes = [c for c in codes if from_qmt_code(c)[1] == market]
            return _map_stock_list(codes, is_hk_connect=False)

        return self._call(_fetch)

    # -- 不支持的能力 -----------------------------------------------------

    def fetch_dividend(self, code: str) -> pd.DataFrame:
        raise UnsupportedCapabilityError("分红数据按分工归 AkShare 适配器")

    def fetch_disclosure(self, code: str, start: date, end: date) -> pd.DataFrame:
        raise UnsupportedCapabilityError("公告数据本版本无可靠源，未声明该能力")

    def fetch_fx_rate(self, pair: str, start: date, end: date) -> pd.DataFrame:
        raise UnsupportedCapabilityError("汇率数据按分工归 AkShare 适配器")

    def fetch_hk_connect_history(self) -> pd.DataFrame:
        raise UnsupportedCapabilityError("港股通名单变动流水本版本无可靠源，未声明该能力")
