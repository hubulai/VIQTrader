"""P5.3 打标配装（learning 层唯一 IO 边界）。

扫描决策源表 → 调 labels.py 纯函数打标 → 幂等写入 outcome_label：
- 波段：swing_signal（含完整 entry/stop/target 参考位的信号），
  T+5/10/20 + first_touch 四枚标签，信号成败都收（无论人工是否确认，
  评分卡度量的是 Agent 产出质量本身）；
- 价值：committee_session 关联估值产出（valuation 契约 JSON 的
  intrinsic_value_range 中枢作为估值误差的「预测值」），T+20/60/250。

幂等：以 (decision_type, decision_ref, label_kind, horizon) 主键为锚，
已存在的标签跳过不覆盖；未到期/未触位返回 None 不落库，下次重跑继续观察。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date

import duckdb

from .labels import (
    SWING_T_N_HORIZONS,
    TOUCH_HORIZON,
    VALUE_T_N_HORIZONS,
    BarRow,
    OutcomeLabel,
    t_n_label,
    touch_label,
    value_eval_label,
)

logger = logging.getLogger(__name__)

# outcome_label.decision_type 与 agent_run/agent_scorecard 的 agent_name 保持一致，
# 评分卡聚合与 P4.7 注入侧（agents/context.py）按同一取值查询，勿改名。
DECISION_SWING = "swing_pick"
DECISION_VALUATION = "valuation"


@dataclass(frozen=True, slots=True)
class LabelReport:
    """一次打标运行的结果。"""

    scanned: int = 0
    inserted: int = 0
    pending: int = 0  # 未到期 / 未触位（留待下次）
    warnings: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# 内部：取数
# ---------------------------------------------------------------------------


def _bars_after(conn: duckdb.DuckDBPyConnection, code: str, decision_date: date) -> list[BarRow]:
    """决策日之后的后复权日线（升序）。"""
    rows = conn.execute(
        """
        SELECT trade_date, high * adj_factor, low * adj_factor, close * adj_factor
        FROM fact_daily_bar
        WHERE code = ? AND trade_date > ?
        ORDER BY trade_date
        """,
        [code, decision_date],
    ).fetchall()
    return [BarRow(trade_date=r[0], high=r[1], low=r[2], close=r[3]) for r in rows]


def _base_close(conn: duckdb.DuckDBPyConnection, code: str, decision_date: date) -> float | None:
    """决策日（含）之前最近一个交易日的后复权收盘；无数据返回 None。"""
    row = conn.execute(
        """
        SELECT close * adj_factor FROM fact_daily_bar
        WHERE code = ? AND trade_date <= ?
        ORDER BY trade_date DESC LIMIT 1
        """,
        [code, decision_date],
    ).fetchone()
    return None if row is None or row[0] is None else float(row[0])


def _existing_labels(
    conn: duckdb.DuckDBPyConnection, decision_type: str
) -> set[tuple[str, str, str]]:
    """已落库标签锚点集合 (decision_ref, label_kind, horizon)。"""
    rows = conn.execute(
        "SELECT decision_ref, label_kind, horizon FROM outcome_label WHERE decision_type = ?",
        [decision_type],
    ).fetchall()
    return {(str(r[0]), str(r[1]), str(r[2])) for r in rows}


def _insert_labels(conn: duckdb.DuckDBPyConnection, labels: list[OutcomeLabel]) -> int:
    for lbl in labels:
        conn.execute(
            """
            INSERT INTO outcome_label (
                decision_type, decision_ref, code, decision_date,
                label_kind, horizon, label_date, return_pct, hit, detail_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                lbl.decision_type,
                lbl.decision_ref,
                lbl.code,
                lbl.decision_date,
                lbl.label_kind,
                lbl.horizon,
                lbl.label_date,
                lbl.return_pct,
                lbl.hit,
                json.dumps(lbl.detail, ensure_ascii=False),
            ],
        )
    return len(labels)


# ---------------------------------------------------------------------------
# 波段信号打标
# ---------------------------------------------------------------------------


def label_swing_signals(conn: duckdb.DuckDBPyConnection) -> LabelReport:
    """扫描 swing_signal 打 T+N + 触位标签（幂等）。"""
    rows = conn.execute(
        """
        SELECT signal_id, code, signal_date, entry_ref, stop_ref, target_ref
        FROM swing_signal
        WHERE entry_ref IS NOT NULL AND stop_ref IS NOT NULL AND target_ref IS NOT NULL
        ORDER BY signal_date, code
        """
    ).fetchall()
    existing = _existing_labels(conn, DECISION_SWING)

    inserted = pending = 0
    warnings: list[str] = []
    for signal_id, code, signal_date, entry_ref, stop_ref, target_ref in rows:
        sid = str(signal_id)
        base = _base_close(conn, code, signal_date)
        if base is None:
            warnings.append(f"{sid} {code} 无决策日（含）前行情，跳过")
            continue
        bars = _bars_after(conn, code, signal_date)

        new_labels: list[OutcomeLabel] = []
        for horizon_days in SWING_T_N_HORIZONS:
            anchor = (sid, "t_n", f"T+{horizon_days}")
            if anchor in existing:
                continue
            lbl = t_n_label(
                decision_type=DECISION_SWING,
                decision_ref=sid,
                code=code,
                decision_date=signal_date,
                horizon_days=horizon_days,
                base_close=base,
                bars_after=bars,
            )
            if lbl is None:
                pending += 1
            else:
                new_labels.append(lbl)

        touch_anchor = (sid, "touch", TOUCH_HORIZON)
        if touch_anchor not in existing:
            lbl = touch_label(
                decision_type=DECISION_SWING,
                decision_ref=sid,
                code=code,
                decision_date=signal_date,
                entry_ref=float(entry_ref),
                stop_ref=float(stop_ref),
                target_ref=float(target_ref),
                bars_after=bars,
            )
            if lbl is None:
                pending += 1
            else:
                new_labels.append(lbl)

        inserted += _insert_labels(conn, new_labels)

    for w in warnings:
        logger.warning("[打标-波段] %s", w)
    return LabelReport(
        scanned=len(rows), inserted=inserted, pending=pending, warnings=tuple(warnings)
    )


# ---------------------------------------------------------------------------
# 价值估值打标
# ---------------------------------------------------------------------------


def label_valuation_sessions(conn: duckdb.DuckDBPyConnection) -> LabelReport:
    """扫描 committee_session 关联估值产出打 value_eval 标签（幂等）。"""
    rows = conn.execute(
        """
        SELECT cs.session_id, cs.code, CAST(cs.started_at AS DATE), ao.output_json
        FROM committee_session cs
        JOIN agent_output ao ON ao.output_id = cs.valuation_output_id
        ORDER BY cs.started_at
        """
    ).fetchall()
    existing = _existing_labels(conn, DECISION_VALUATION)

    inserted = pending = 0
    warnings: list[str] = []
    for session_id, code, decision_date, output_json in rows:
        sid = str(session_id)
        estimate_mid: float | None = None
        try:
            payload = json.loads(str(output_json))
            rng = payload.get("intrinsic_value_range")
            if isinstance(rng, list) and len(rng) == 2:
                estimate_mid = (float(rng[0]) + float(rng[1])) / 2.0
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            warnings.append(f"{sid} 估值产出 JSON 解析失败（估值误差缺省）: {exc}")

        base = _base_close(conn, code, decision_date)
        if base is None:
            warnings.append(f"{sid} {code} 无决策日（含）前行情，跳过")
            continue
        bars = _bars_after(conn, code, decision_date)

        new_labels: list[OutcomeLabel] = []
        for horizon_days in VALUE_T_N_HORIZONS:
            anchor = (sid, "value_eval", f"T+{horizon_days}")
            if anchor in existing:
                continue
            lbl = value_eval_label(
                decision_type=DECISION_VALUATION,
                decision_ref=sid,
                code=code,
                decision_date=decision_date,
                horizon_days=horizon_days,
                base_close=base,
                bars_after=bars,
                estimate_mid=estimate_mid,
            )
            if lbl is None:
                pending += 1
            else:
                new_labels.append(lbl)
        inserted += _insert_labels(conn, new_labels)

    for w in warnings:
        logger.warning("[打标-价值] %s", w)
    return LabelReport(
        scanned=len(rows), inserted=inserted, pending=pending, warnings=tuple(warnings)
    )


__all__ = [
    "DECISION_SWING",
    "DECISION_VALUATION",
    "LabelReport",
    "label_swing_signals",
    "label_valuation_sessions",
]
