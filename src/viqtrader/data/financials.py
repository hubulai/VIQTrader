"""fact_financial 口径助手：把「单季流量」还原为 as-reported 累计口径。

DB 契约（见 data/adapters/qmt.py `_map_financial`，2026-10-15 QMT 实测确认）：

- 流量列 ``revenue`` / ``net_profit`` / ``ocf`` / ``fcf`` / ``eps`` 落库为**单季口径**
  （QMT 原始为年初至今累计，落库前已差分；设计意图供估值层 ``rolling(4).sum`` 得 TTM）；
- 比率列 ``roe`` / ``roa`` / ``gross_margin`` / ``net_margin`` / ``debt_ratio`` 为
  **年初至今累计（YTD）原值**（Q4 行即年度值）；
- ``bps`` / ``total_assets`` / ``total_equity`` 为**期末时点值**。

**凡把单季流量当年度/累计值直接消费的模块均属误用**（估值层 rolling(4).sum 除外）。
本模块统一提供还原逻辑，避免各处重复实现漂移：

- :func:`ytd_flow`：单个报告期的累计流量 = 同年 Q1..本报告期单季值求和；
- :func:`reported_financials`：按报告期还原的 as-reported 财报序列（供 Agent / 回测消费）。
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

import duckdb

QUARTER_MONTHS: tuple[int, ...] = (3, 6, 9, 12)

# 流量列（落库为单季口径，还原累计须同年求和）
FLOW_COLUMNS: tuple[str, ...] = ("revenue", "net_profit", "ocf", "fcf", "eps")

_FETCH_COLUMNS: tuple[str, ...] = (
    "revenue",
    "net_profit",
    "ocf",
    "fcf",
    "eps",
    "roe",
    "roa",
    "gross_margin",
    "net_margin",
    "debt_ratio",
    "bps",
)

__all__ = [
    "FLOW_COLUMNS",
    "QUARTER_MONTHS",
    "ReportedFinancial",
    "reported_financials",
    "ytd_flow",
]


@dataclass(frozen=True, slots=True)
class ReportedFinancial:
    """单个报告期的 **as-reported 累计口径** 财报（流量为 YTD 累计、比率/每股为原值）。"""

    code: str
    report_period: date
    ann_date: date | None
    revenue: float | None
    net_profit: float | None
    ocf: float | None
    fcf: float | None
    eps: float | None
    roe: float | None
    roa: float | None
    gross_margin: float | None
    net_margin: float | None
    debt_ratio: float | None
    bps: float | None


def _norm(value: Any) -> float | None:
    """NaN/None → None（DuckDB DOUBLE 的 NaN 与 NULL 语义不同，须显式判）。"""
    if value is None:
        return None
    num = float(value)
    return None if math.isnan(num) else num


def ytd_flow(by_month: Mapping[int, float | None], end_month: int) -> float | None:
    """截至 end_month 的 as-reported 累计流量 = 同年 Q1..end_month 各单季值求和。

    任一季度缺失 → None（宁缺勿错，避免部分季度求和失真）。
    """
    if end_month not in QUARTER_MONTHS:
        return None
    vals = [by_month.get(m) for m in QUARTER_MONTHS if m <= end_month]
    if any(v is None for v in vals):
        return None
    return float(sum(v for v in vals if v is not None))


# 每个季度月 → (报告期日期, 公告日, 列名→原始值)
_Cell = tuple[date, date | None, dict[str, float | None]]


def _ytd_from_group(group: Mapping[int, _Cell], column: str, end_month: int) -> float | None:
    """同年各季度单季值 → 截至 end_month 的累计值。"""
    return ytd_flow({m: _norm(group[m][2][column]) for m in group}, end_month)


def reported_financials(
    conn: duckdb.DuckDBPyConnection,
    code: str,
    *,
    ann_before: date | None = None,
    ann_after: date | None = None,
    limit: int | None = None,
) -> list[ReportedFinancial]:
    """该 code 全部季度行 → 按报告期还原为 as-reported 累计口径（报告期降序）。

    口径：流量列按同年 Q1..本报告期求和（任一季度缺失 → None）；比率列与 bps 原值透传。

    ``ann_before`` / ``ann_after``（含端点）用于**筛选返回的报告期**（按该期自身公告日）。
    ``ann_before`` 下推 SQL（安全的 as-of 上界：累计披露天然早于本期公告日）；``ann_after``
    仅在 Python 侧过滤——**不得下推**，否则同年更早季度会被剔除而破坏 YTD 累计。

    ``limit`` 在过滤后按报告期降序截断（取最近 N 期）。
    """
    sql = (
        "SELECT report_period, ann_date, " + ", ".join(_FETCH_COLUMNS) + " "
        "FROM fact_financial "
        "WHERE code = ? AND month(report_period) IN (3, 6, 9, 12)"
    )
    params: list[Any] = [code]
    if ann_before is not None:
        sql += " AND ann_date IS NOT NULL AND ann_date <= ?"
        params.append(ann_before)
    sql += " ORDER BY report_period"
    rows = conn.execute(sql, params).fetchall()

    grouped: dict[int, dict[int, _Cell]] = defaultdict(dict)
    for row in rows:
        period: date = row[0]
        cells: dict[str, float | None] = {col: row[i + 2] for i, col in enumerate(_FETCH_COLUMNS)}
        grouped[period.year][period.month] = (period, row[1], cells)

    out: list[ReportedFinancial] = []
    for months in grouped.values():
        for month, (period, ann, cells) in months.items():
            if ann_after is not None and (ann is None or ann < ann_after):
                continue
            out.append(
                ReportedFinancial(
                    code=code,
                    report_period=period,
                    ann_date=ann,
                    revenue=_ytd_from_group(months, "revenue", month),
                    net_profit=_ytd_from_group(months, "net_profit", month),
                    ocf=_ytd_from_group(months, "ocf", month),
                    fcf=_ytd_from_group(months, "fcf", month),
                    eps=_ytd_from_group(months, "eps", month),
                    roe=_norm(cells["roe"]),
                    roa=_norm(cells["roa"]),
                    gross_margin=_norm(cells["gross_margin"]),
                    net_margin=_norm(cells["net_margin"]),
                    debt_ratio=_norm(cells["debt_ratio"]),
                    bps=_norm(cells["bps"]),
                )
            )

    out.sort(key=lambda r: r.report_period, reverse=True)
    return out[:limit] if limit is not None else out
