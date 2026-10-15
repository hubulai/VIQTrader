"""P1.6 数据质量校验器（全离线，不触网）。

对已同步到 DuckDB 的数据做健康检查，输出 DataQualityReport 列表。
任何一项硬约束违反（如 ann_date < report_period）都会进入 warnings，
调用方可据此阻断入库或标注风险等级。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import duckdb
import pandas as pd

from viqtrader.config.loader import Config

from .base import (
    MARKETS,
    SCHEMAS,
    Capability,
    TableSchema,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 报告结构
# ---------------------------------------------------------------------------


@dataclass
class DataQualityReport:
    """单个 (code, capability) 的数据质量检查结果。"""

    code: str
    capability: str
    total_rows: int = 0
    date_range: tuple[date, date] | None = None
    gaps: list[tuple[date, date]] = field(default_factory=list)
    zero_volume_days: list[date] = field(default_factory=list)
    duplicate_keys: list[str] = field(default_factory=list)
    null_rate: dict[str, float] = field(default_factory=dict)
    financial_gaps: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 内部：DuckDB → DataFrame
# ---------------------------------------------------------------------------

_CAP_TABLE: dict[Capability, str] = {
    Capability.STOCK_LIST: "dim_stock",
    Capability.DAILY_BAR: "fact_daily_bar",
    Capability.FINANCIAL: "fact_financial",
    Capability.DIVIDEND: "fact_dividend",
    Capability.FX_RATE: "fact_fx_rate",
}


def _load_data(
    conn: duckdb.DuckDBPyConnection,
    cap: Capability,
    code: str | None = None,
) -> pd.DataFrame:
    """从 DuckDB 加载某能力的数据。FX_RATE 用 pair 作 code。"""
    table = _CAP_TABLE[cap]
    if cap is Capability.FX_RATE:
        if code is None:
            return conn.execute(f"SELECT * FROM {table}").df()
        return conn.execute(f"SELECT * FROM {table} WHERE pair = ?", [code]).df()
    if cap is Capability.STOCK_LIST:
        if code is None:
            return conn.execute(f"SELECT * FROM {table}").df()
        return conn.execute(
            f"SELECT * FROM {table} WHERE code IN ({','.join(['?'] * len([code]))})",
            [code],
        ).df()
    if code is None:
        return conn.execute(f"SELECT * FROM {table}").df()
    return conn.execute(f"SELECT * FROM {table} WHERE code = ?", [code]).df()


# ---------------------------------------------------------------------------
# 通用检查项
# ---------------------------------------------------------------------------


def _check_duplicate_keys(df: pd.DataFrame, schema: TableSchema) -> list[str]:
    """主键重复检测。"""
    if df.empty:
        return []
    dup = df.duplicated(subset=list(schema.key), keep=False)
    if not dup.any():
        return []
    dup_rows = df[dup]
    keys = dup_rows[list(schema.key)].drop_duplicates()
    return [str(tuple(k)) for k in keys.itertuples(index=False, name=None)]


def _check_null_rate(df: pd.DataFrame, schema: TableSchema, threshold: float) -> dict[str, float]:
    """关键列 null 比例。"""
    if df.empty:
        return {}
    critical_cols: list[str] = list(schema.key) + list(schema.numeric_columns)
    result: dict[str, float] = {}
    for col in critical_cols:
        if col not in df.columns:
            continue
        rate = float(df[col].isna().sum()) / len(df) if len(df) > 0 else 0.0
        if rate >= threshold:
            result[col] = round(rate, 4)
    return result


def _check_date_continuity(df: pd.DataFrame, date_col: str) -> list[tuple[date, date]]:
    """日期连续性检查：diff(days) != 1 的区间。"""
    if df.empty or date_col not in df.columns:
        return []
    s = pd.to_datetime(df[date_col]).dropna().sort_values().reset_index(drop=True)
    if len(s) < 2:
        return []
    gaps: list[tuple[date, date]] = []
    diffs = s.diff().dt.days
    for i in range(1, len(s)):
        d = diffs.iloc[i]
        if d > 1:
            gap_start = (s.iloc[i - 1] + pd.Timedelta(days=1)).date()
            gap_end = (s.iloc[i] - pd.Timedelta(days=1)).date()
            if gap_start <= gap_end:
                gaps.append((gap_start, gap_end))
    return gaps


# ---------------------------------------------------------------------------
# 各能力专项检查
# ---------------------------------------------------------------------------


def _check_daily_bar(df: pd.DataFrame, report: DataQualityReport) -> None:
    """日线专项检查。"""
    if df.empty:
        return

    # 停牌检测：volume=0 且 amount=0
    has_vol = "volume" in df.columns and "amount" in df.columns
    if has_vol:
        vol_zero = df["volume"].fillna(0) == 0
        amt_zero = df["amount"].fillna(0) == 0
        halted = df[vol_zero & amt_zero]
        if not halted.empty:
            report.zero_volume_days = [
                d.date() if isinstance(d, pd.Timestamp) else d
                for d in pd.to_datetime(halted["trade_date"]).tolist()
            ]

    # 前视偏差 / 日波动合理性：close 的日涨跌 > 50% 告警
    has_close = "close" in df.columns
    if has_close and len(df) >= 2:
        sorted_df = df.sort_values("trade_date").reset_index(drop=True)
        sorted_df["prev_close"] = sorted_df["close"].shift(1)
        sorted_df["pct_change"] = (sorted_df["close"] - sorted_df["prev_close"]) / sorted_df[
            "prev_close"
        ]
        extreme = sorted_df["pct_change"].abs() > 0.50
        # 过滤掉初始 NaN 行
        extreme = extreme & sorted_df["prev_close"].notna()
        if extreme.any():
            n = int(extreme.sum())
            report.warnings.append(f"有 {n} 天 close 日涨跌超过 ±50%，请检查数据或是否为复权异常")


def _check_financial(df: pd.DataFrame, report: DataQualityReport) -> None:
    """财务专项检查。"""
    if df.empty:
        return

    # 硬约束：ann_date < report_period
    both = df["ann_date"].notna() & df["report_period"].notna()
    if both.any():
        ann_dt = pd.to_datetime(df.loc[both, "ann_date"])
        rep_dt = pd.to_datetime(df.loc[both, "report_period"])
        bad = ann_dt < rep_dt
        if bad.any():
            count = int(bad.sum())
            report.warnings.append(f"前视偏差硬约束违反：{count} 行 ann_date < report_period")

    # revenue 连续 2 季 gap > 2x
    has_revenue = "revenue" in df.columns
    if has_revenue and len(df) >= 2:
        sorted_df = df.sort_values("report_period").reset_index(drop=True)
        rev = sorted_df["revenue"].dropna()
        if len(rev) >= 2:
            rev_next = rev.shift(-1)
            ratio = rev_next / rev.replace(0, pd.NA)
            big_jump = (ratio.abs() > 2).fillna(False)
            # 只看正数 revenue 的跳跃
            pos_mask = (rev > 0) & (rev_next > 0)
            if (big_jump & pos_mask).any():
                n = int((big_jump & pos_mask).sum())
                report.warnings.append(f"发现 {n} 次 revenue 环比波动超 2x，可能漏数")

    # roe / roa / gross_margin 比率范围 ±200%
    for col in ("roe", "roa", "gross_margin", "net_margin", "debt_ratio"):
        if col in df.columns:
            s = df[col].dropna()
            out_of_range = (s < -2.0) | (s > 2.0)
            if out_of_range.any():
                n = int(out_of_range.sum())
                report.warnings.append(f"{col} 有 {n} 行超出 [-200%, 200%] 范围")


def _check_dividend(df: pd.DataFrame, report: DataQualityReport) -> None:
    """分红专项检查。"""
    if df.empty:
        return

    today_ts = pd.Timestamp(date.today())

    # ex_date 不能是未来日期
    if "ex_date" in df.columns:
        future = df["ex_date"].notna() & (pd.to_datetime(df["ex_date"]) > today_ts)
        if future.any():
            n = int(future.sum())
            report.warnings.append(f"{n} 条 ex_date 为未来日期")

    # dividend_per_share ≥ 0
    if "dividend_per_share" in df.columns:
        neg = df["dividend_per_share"].notna() & (df["dividend_per_share"] < 0)
        if neg.any():
            n = int(neg.sum())
            report.warnings.append(f"{n} 条 dividend_per_share < 0")

    # ex_date ≤ pay_date
    if "ex_date" in df.columns and "pay_date" in df.columns:
        both = df["ex_date"].notna() & df["pay_date"].notna()
        if both.any():
            wrong = pd.to_datetime(df.loc[both, "ex_date"]) > pd.to_datetime(
                df.loc[both, "pay_date"]
            )
            if wrong.any():
                n = int(wrong.sum())
                report.warnings.append(f"{n} 条 ex_date > pay_date")


def _check_fx_rate(df: pd.DataFrame, report: DataQualityReport) -> None:
    """汇率专项检查。"""
    if df.empty:
        return
    if "rate" in df.columns:
        bad_rate = df["rate"].notna() & (df["rate"] <= 0)
        if bad_rate.any():
            n = int(bad_rate.sum())
            report.warnings.append(f"{n} 条 rate ≤ 0")


def _check_stock_list(df: pd.DataFrame, report: DataQualityReport) -> None:
    """标的主数据专项检查。"""
    if df.empty:
        return
    # 市场字段范围
    if "market" in df.columns:
        bad = set(df["market"].dropna().unique()) - MARKETS
        if bad:
            report.warnings.append(f"market 非法取值: {sorted(bad)}")
    # is_hk_connect 应为 bool
    if "is_hk_connect" in df.columns:
        non_bool = df["is_hk_connect"].notna() & (~df["is_hk_connect"].isin([True, False]))
        if non_bool.any():
            n = int(non_bool.sum())
            report.warnings.append(f"{n} 条 is_hk_connect 非布尔值")


# ---------------------------------------------------------------------------
# 核心检查器
# ---------------------------------------------------------------------------


class DataQualityChecker:
    """数据质量检查器。

    使用方式::

        checker = DataQualityChecker(conn, config)
        reports = checker.check(codes=["00700", "600519"], capabilities=[...])
    """

    def __init__(
        self,
        conn: duckdb.DuckDBPyConnection,
        config: Config | dict[str, Any] | None = None,
    ) -> None:
        self.conn = conn
        if isinstance(config, Config):
            self.null_warn_threshold: float = float(
                config.get("data.sync.quality_check_null_warn_threshold", 0.30)
            )
        elif isinstance(config, dict):
            sync_cfg = config.get("data", {}).get("sync", {})
            self.null_warn_threshold = float(
                sync_cfg.get("quality_check_null_warn_threshold", 0.30)
            )
        else:
            self.null_warn_threshold = 0.30

    def check(
        self,
        codes: list[str] | None = None,
        capabilities: list[Capability] | None = None,
    ) -> list[DataQualityReport]:
        """执行检查，返回报告列表。"""
        reports: list[DataQualityReport] = []

        caps = capabilities or [
            Capability.DAILY_BAR,
            Capability.FINANCIAL,
            Capability.DIVIDEND,
            Capability.FX_RATE,
            Capability.STOCK_LIST,
        ]
        target_codes = codes or []

        for cap in caps:
            schema = SCHEMAS[cap]

            if cap is Capability.STOCK_LIST:
                df = _load_data(self.conn, cap)
                reports.append(self._check_single(cap, "__all__", df, schema))
            elif cap is Capability.FX_RATE:
                # FX_RATE 的 code 是 pair
                if not target_codes:
                    df_all = _load_data(self.conn, cap)
                    pairs = df_all["pair"].unique().tolist() if not df_all.empty else []
                else:
                    pairs = target_codes
                for pair in pairs:
                    df = _load_data(self.conn, cap, pair)
                    reports.append(self._check_single(cap, pair, df, schema))
            else:
                if not target_codes:
                    # 查出库里全部 code
                    table = _CAP_TABLE[cap]
                    codes_in_db = self.conn.execute(f"SELECT DISTINCT code FROM {table}").fetchall()
                    target = [r[0] for r in codes_in_db]
                else:
                    target = target_codes
                for code in target:
                    df = _load_data(self.conn, cap, code)
                    reports.append(self._check_single(cap, code, df, schema))

        return reports

    # ---------------- 单个 (cap, code) ----------------

    def _check_single(
        self,
        cap: Capability,
        code: str,
        df: pd.DataFrame,
        schema: TableSchema,
    ) -> DataQualityReport:
        report = DataQualityReport(code=code, capability=cap.value)

        # 基础统计
        report.total_rows = len(df)
        if df.empty:
            report.warnings.append("空数据集：尚未同步任何数据")
            return report

        date_col = schema.date_columns[0] if schema.date_columns else None
        if date_col and date_col in df.columns:
            dates = pd.to_datetime(df[date_col]).dropna()
            if not dates.empty:
                report.date_range = (dates.min().date(), dates.max().date())

        # 通用检查
        report.duplicate_keys = _check_duplicate_keys(df, schema)
        if report.duplicate_keys:
            report.warnings.append(f"发现 {len(report.duplicate_keys)} 组主键重复")

        report.null_rate = _check_null_rate(df, schema, self.null_warn_threshold)
        if report.null_rate:
            report.warnings.append(
                f"关键列 null 比例超阈值（>{self.null_warn_threshold:.0%}）: "
                + ", ".join(f"{c}={v:.1%}" for c, v in report.null_rate.items())
            )

        # 日期连续性（有 date_col 的能力）
        if date_col and date_col in df.columns:
            report.gaps = _check_date_continuity(df, date_col)

        # 专项检查
        if cap is Capability.DAILY_BAR:
            _check_daily_bar(df, report)
        elif cap is Capability.FINANCIAL:
            _check_financial(df, report)
        elif cap is Capability.DIVIDEND:
            _check_dividend(df, report)
        elif cap is Capability.FX_RATE:
            _check_fx_rate(df, report)
        elif cap is Capability.STOCK_LIST:
            _check_stock_list(df, report)

        return report
