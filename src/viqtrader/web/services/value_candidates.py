"""价值筛选候选池服务（P1 修复：接通 DAG value_screen → 控制台页面）。

value_candidate 是 run_value_screen 每周六 DELETE+INSERT 覆盖写的周快照，
路由只消费最新 screen_date 那批；候选池供人工挑选做投委会→狩猎清单。
"""

from __future__ import annotations

from dataclasses import dataclass

import duckdb


@dataclass(frozen=True, slots=True)
class ValueCandidateRow:
    screen_date: str
    code: str
    market: str
    name: str
    industry: str | None
    roe_years: int | None
    roe_mean: float | None
    profit_years: int | None
    fcf_mean: float | None
    debt_ratio_latest: float | None
    revenue_cagr: float | None
    profit_cagr: float | None
    pe_ttm: float | None
    pb: float | None
    dv_ratio: float | None
    pe_percentile: float | None
    gate_passed: bool
    quality_score: float


def list_value_candidates(
    conn: duckdb.DuckDBPyConnection,
    *,
    limit: int = 80,
    gate_only: bool = False,
) -> tuple[str | None, list[ValueCandidateRow]]:
    """取最新 screen_date 的候选池；返回 (screen_date, rows)。

    gate_only=True 时只返回 gate_passed=TRUE（过估值闸门）的候选。
    """
    latest = conn.execute("SELECT max(screen_date) FROM value_candidate").fetchone()[0]
    if latest is None:
        return None, []

    where = "WHERE screen_date = ?"
    if gate_only:
        where += " AND gate_passed = TRUE"

    rows = conn.execute(
        f"""
        SELECT screen_date, code, market, name, industry,
               roe_years, roe_mean, profit_years, fcf_mean,
               debt_ratio_latest, revenue_cagr, profit_cagr,
               pe_ttm, pb, dv_ratio, pe_percentile,
               gate_passed, quality_score
        FROM value_candidate
        {where}
        -- isnan 置底：DuckDB 排序把 NaN 视为最大值，防历史脏批次假第一名
        ORDER BY gate_passed DESC,
                 CASE WHEN isnan(quality_score) THEN NULL ELSE quality_score END DESC NULLS LAST,
                 CASE WHEN isnan(roe_mean) THEN NULL ELSE roe_mean END DESC NULLS LAST
        LIMIT ?
        """,
        [latest, limit],
    ).fetchall()

    def _f(v) -> float | None:
        return float(v) if v is not None else None

    return str(latest), [
        ValueCandidateRow(
            str(r[0]),
            str(r[1]),
            str(r[2]),
            str(r[3]),
            str(r[4]) if r[4] is not None else None,
            int(r[5]) if r[5] is not None else None,
            _f(r[6]),
            int(r[7]) if r[7] is not None else None,
            _f(r[8]),
            _f(r[9]),
            _f(r[10]),
            _f(r[11]),
            _f(r[12]),
            _f(r[13]),
            _f(r[14]),
            _f(r[15]),
            bool(r[16]),
            float(r[17]),
        )
        for r in rows
    ]


def candidate_status_counts(conn: duckdb.DuckDBPyConnection) -> tuple[int, int]:
    """最新 screen_date 批次的 (gate_passed 数, 总候选数)。"""
    latest = conn.execute("SELECT max(screen_date) FROM value_candidate").fetchone()[0]
    if latest is None:
        return 0, 0
    row = conn.execute(
        "SELECT count(*) FILTER (WHERE gate_passed = TRUE), count(*) "
        "FROM value_candidate WHERE screen_date = ?",
        [latest],
    ).fetchone()
    return int(row[0]), int(row[1])
