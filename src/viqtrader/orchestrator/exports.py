"""初筛结果文件导出（Web 之外的查阅通道）。

value_candidate / swing_signal 全字段导出为 Excel 友好 CSV（utf-8-sig 带 BOM，
中文不乱码），落 <data_dir>/outputs/exports/，按业务日戳命名、同日重跑原子覆盖。
数据库仍是唯一事实源；swing 信号的人工状态流转（confirmed/rejected）不会回写
已导出文件，需人工状态变化后重跑 export_swing 刷新，属预期行为。
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path
from typing import Any

import duckdb

# 列序即表结构语义序：确定字段 → Agent 产物 → 人工闸
# created_at 转字符串：DuckDB 取 TIMESTAMPTZ 需 pytz，转 VARCHAR 免依赖且查阅无差
_VALUE_COLS = """
    screen_date, code, market, name, industry,
    roe_years, roe_mean, profit_years, fcf_mean, fcf_all_positive,
    debt_ratio_latest, revenue_cagr, profit_cagr,
    pe_ttm, pb, dv_ratio, pe_percentile, gate_passed, quality_score,
    CAST(created_at AS VARCHAR) AS created_at
"""

_SWING_COLS = """
    signal_id, code, market, name, signal_date,
    signal_type, pattern_desc, entry_ref, stop_ref, target_ref,
    interpretation, win_rate_note, status,
    screen_features_json, picker_risks_json, invalidation_json,
    interpreter_risks_json, picker_run_id, interpreter_run_id,
    CAST(created_at AS VARCHAR) AS created_at
"""


def _write_csv(conn: duckdb.DuckDBPyConnection, sql: str, params: list[Any], path: Path) -> int:
    """查询结果原子写 CSV（tmp + replace），返回行数。utf-8-sig 供 Excel 直开。"""
    cur = conn.execute(sql, params)
    rows = cur.fetchall()
    headers = [d[0] for d in cur.description]
    tmp = path.with_suffix(".csv.tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)
    tmp.replace(path)
    return len(rows)


def export_value_candidates(
    conn: duckdb.DuckDBPyConnection, exports_dir: Path
) -> tuple[Path, int] | None:
    """导出最新 screen_date 批次（周快照，批次内按闸门+质量排序）；表空返回 None。"""
    row = conn.execute("SELECT max(screen_date) FROM value_candidate").fetchone()
    latest = row[0] if row is not None else None
    if latest is None:
        return None
    path = exports_dir / f"value_candidates_{latest:%Y%m%d}.csv"
    n = _write_csv(
        conn,
        f"""
        SELECT {_VALUE_COLS} FROM value_candidate WHERE screen_date = ?
        -- isnan 置底：DuckDB 排序把 NaN 视为最大值，防历史脏批次假第一名
        ORDER BY gate_passed DESC,
                 CASE WHEN isnan(quality_score) THEN NULL ELSE quality_score END DESC NULLS LAST,
                 CASE WHEN isnan(roe_mean) THEN NULL ELSE roe_mean END DESC NULLS LAST
        """,
        [latest],
        path,
    )
    return path, n


def export_swing_signals(
    conn: duckdb.DuckDBPyConnection, exports_dir: Path, run_date: date
) -> tuple[Path, int]:
    """导出全历史信号（表小，全量便于跨日排序回看；最新信号在前）。"""
    path = exports_dir / f"swing_signals_{run_date:%Y%m%d}.csv"
    n = _write_csv(
        conn,
        f"SELECT {_SWING_COLS} FROM swing_signal ORDER BY signal_date DESC, code",
        [],
        path,
    )
    return path, n
