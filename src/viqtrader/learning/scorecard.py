"""P5.3 Agent 评分卡聚合（纯函数聚合 + 幂等写入）。

口径（设计评审拍板）：
- 滚动窗口：window_end 之前 90 天内已判定的标签（label_date 落入窗口）；
- swing_pick：sample_size / hit_rate（全部标签）+ false_positive_rate
  （T+5 标签中未命中占比——最短周期判定「信号误报」，无 T+5 样本时 None）；
- valuation：sample_size / hit_rate + error_mean / error_std
  （detail 中的 valuation_error，正值=高估，与 P4.7 注入侧口径一致）；
- 样本为 0 时不写评分卡（保留历史行，注入侧按最新 window_end 取）。
- 幂等：按 (agent_name, decision_type, window_end) 先 DELETE 再 INSERT。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, timedelta

import duckdb

from .labeler import DECISION_SWING, DECISION_VALUATION

logger = logging.getLogger(__name__)

WINDOW_DAYS = 90  # 拍板口径：评分卡滚动 90 天窗口


@dataclass(frozen=True, slots=True)
class LabelFact:
    """聚合输入（outcome_label 行子集，valuation_error 已从 detail_json 解出）。"""

    label_kind: str
    horizon: str
    hit: bool
    valuation_error: float | None = None


@dataclass(frozen=True, slots=True)
class ScorecardMetrics:
    """一行评分卡（agent_scorecard 表行同构，不含主键）。"""

    sample_size: int
    hit_rate: float | None
    error_mean: float | None
    error_std: float | None
    false_positive_rate: float | None


# ---------------------------------------------------------------------------
# 纯函数聚合
# ---------------------------------------------------------------------------


def aggregate_swing(facts: list[LabelFact]) -> ScorecardMetrics | None:
    """波段评分卡：无样本返回 None。"""
    if not facts:
        return None
    hit_rate = sum(1 for f in facts if f.hit) / len(facts)
    t5 = [f for f in facts if f.label_kind == "t_n" and f.horizon == "T+5"]
    fpr = None
    if t5:
        fpr = sum(1 for f in t5 if not f.hit) / len(t5)
    return ScorecardMetrics(
        sample_size=len(facts),
        hit_rate=hit_rate,
        error_mean=None,
        error_std=None,
        false_positive_rate=fpr,
    )


def aggregate_valuation(facts: list[LabelFact]) -> ScorecardMetrics | None:
    """估值评分卡：误差均值/波动取 valuation_error；样本 <2 时 std 为 None。"""
    if not facts:
        return None
    hit_rate = sum(1 for f in facts if f.hit) / len(facts)
    errors = [f.valuation_error for f in facts if f.valuation_error is not None]
    error_mean = error_std = None
    if errors:
        error_mean = sum(errors) / len(errors)
        if len(errors) >= 2:
            var = sum((e - error_mean) ** 2 for e in errors) / (len(errors) - 1)
            error_std = var**0.5
    return ScorecardMetrics(
        sample_size=len(facts),
        hit_rate=hit_rate,
        error_mean=error_mean,
        error_std=error_std,
        false_positive_rate=None,
    )


# ---------------------------------------------------------------------------
# DB 装配
# ---------------------------------------------------------------------------


def _load_facts(
    conn: duckdb.DuckDBPyConnection,
    decision_type: str,
    *,
    window_end: date,
    window_days: int,
) -> list[LabelFact]:
    """窗口内已判定标签（label_date 落入 (window_end - days, window_end]）。"""
    window_start = window_end - timedelta(days=window_days)
    rows = conn.execute(
        """
        SELECT label_kind, horizon, hit, detail_json
        FROM outcome_label
        WHERE decision_type = ? AND label_date > ? AND label_date <= ?
        """,
        [decision_type, window_start, window_end],
    ).fetchall()
    facts: list[LabelFact] = []
    for kind, horizon, hit, detail_json in rows:
        valuation_error: float | None = None
        if detail_json:
            try:
                valuation_error = json.loads(str(detail_json)).get("valuation_error")
            except json.JSONDecodeError:
                logger.warning("[评分卡] detail_json 解析失败，估值误差按缺省: %s", detail_json)
        facts.append(
            LabelFact(
                label_kind=str(kind),
                horizon=str(horizon),
                hit=bool(hit),
                valuation_error=valuation_error,
            )
        )
    return facts


def _write_scorecard(
    conn: duckdb.DuckDBPyConnection,
    *,
    agent_name: str,
    window_end: date,
    metrics: ScorecardMetrics,
) -> None:
    conn.execute(
        "DELETE FROM agent_scorecard WHERE agent_name = ? AND decision_type = ? AND window_end = ?",
        [agent_name, agent_name, window_end],
    )
    conn.execute(
        """
        INSERT INTO agent_scorecard (
            agent_name, decision_type, window_end, sample_size,
            hit_rate, error_mean, error_std, false_positive_rate, metrics_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            agent_name,
            agent_name,
            window_end,
            metrics.sample_size,
            metrics.hit_rate,
            metrics.error_mean,
            metrics.error_std,
            metrics.false_positive_rate,
            None,
        ],
    )


def write_scorecards(
    conn: duckdb.DuckDBPyConnection,
    *,
    window_end: date,
    window_days: int = WINDOW_DAYS,
) -> tuple[str, ...]:
    """聚合两类决策并写入评分卡，返回实际写入的 agent_name 列表（幂等）。"""
    aggregators = {
        DECISION_SWING: aggregate_swing,
        DECISION_VALUATION: aggregate_valuation,
    }
    written: list[str] = []
    for agent_name, aggregate in aggregators.items():
        facts = _load_facts(conn, agent_name, window_end=window_end, window_days=window_days)
        metrics = aggregate(facts)
        if metrics is None:
            logger.info("[评分卡] %s 窗口内无样本，跳过", agent_name)
            continue
        _write_scorecard(conn, agent_name=agent_name, window_end=window_end, metrics=metrics)
        written.append(agent_name)
        logger.info(
            "[评分卡] %s 样本 %d，命中率 %s",
            agent_name,
            metrics.sample_size,
            f"{metrics.hit_rate:.1%}" if metrics.hit_rate is not None else "—",
        )
    return tuple(written)


__all__ = [
    "WINDOW_DAYS",
    "LabelFact",
    "ScorecardMetrics",
    "aggregate_swing",
    "aggregate_valuation",
    "write_scorecards",
]
