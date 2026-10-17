"""复盘 review pipeline 编排（P4.6，设计方案 4.3 ⑥⑧）。

一节定量、两节定性，三段相互独立、失败隔离：

1. 归因段（定量→定性）：loader 装配 Brinson 输入 → compute_brinson 纯函数分解
   → 组合归因解读 Agent；装配返回 None（账本空/基准 universe 空）→ 跳过而非报错。
2. thesis 段：对**期初持仓**（与归因同口径，replay trade_date < start）逐仓复核；
   仅复核关联了 active thesis 的持仓（价值轨机制），无 thesis/已清仓/非 active 跳过并留痕；
   单仓失败不影响其他仓。
3. 风控段：区间 risk_event 非空才调用风控解读 Agent；无事件为正常状态（empty）。

任何一段的异常都收敛到返回结构中，pipeline 不向上抛（CLI 一次性跑完出完整复盘报告）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Any

import duckdb

from viqtrader.agents.contracts.review import (
    AttributionReviewOutput,
    RiskInterpretOutput,
    ThesisReviewOutput,
)
from viqtrader.agents.llm.base import LLMProvider
from viqtrader.agents.llm.runs import STATUS_SUCCESS
from viqtrader.agents.shared.review.attribution_agent import run_attribution_review_agent
from viqtrader.agents.shared.review.thesis_agent import (
    ThesisReviewInput,
    run_thesis_review_agent,
)
from viqtrader.agents.shared.risk_interpreter import (
    RiskEventInput,
    run_risk_interpreter_agent,
)
from viqtrader.attribution.brinson import compute_brinson
from viqtrader.attribution.loader import load_brinson_input, replay_holdings
from viqtrader.config.loader import Config

__all__ = [
    "AttributionSection",
    "ReviewPipelineResult",
    "RiskSection",
    "ThesisSectionItem",
    "run_review_pipeline",
]

_DISCLOSURE_LIMIT = 10  # 区间公告送入 thesis 复核的条数上限（prompt 长度控制）


@dataclass(frozen=True, slots=True)
class AttributionSection:
    """归因段结果。status: ok / unavailable / failed。"""

    status: str
    run_id: str | None = None
    agent_status: str | None = None  # SUCCESS / DEGRADED / FAILED
    active_return: float | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ThesisSectionItem:
    """单仓 thesis 复核结果。status: success / failed / skipped。"""

    code: str
    status: str
    position_id: str | None = None
    run_id: str | None = None
    thesis_status: str | None = None
    suggested_action: str | None = None
    reason: str | None = None  # skipped 原因或 failed 错误信息


@dataclass(frozen=True, slots=True)
class RiskSection:
    """风控段结果。status: ok / empty / failed。"""

    status: str
    run_id: str | None = None
    agent_status: str | None = None
    event_count: int = 0
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewPipelineResult:
    """一次组合复盘的完整结果（三段 + 数据质量 notes）。"""

    account_id: str
    start: str
    end: str
    attribution: AttributionSection
    thesis_reviews: tuple[ThesisSectionItem, ...]
    risk: RiskSection
    notes: tuple[str, ...] = ()


def run_review_pipeline(
    conn: duckdb.DuckDBPyConnection,
    config: Config,
    provider: LLMProvider,
    *,
    account_id: str,
    start: date,
    end: date,
    benchmark_code: str = "000300",
) -> ReviewPipelineResult:
    """跑完整复盘流水线；任何一段失败都隔离收敛，不向上抛。"""
    if start > end:
        raise ValueError(f"复盘区间非法：start={start} > end={end}")

    notes: list[str] = []
    attribution = _run_attribution_section(
        conn, config, provider, account_id, start, end, benchmark_code, notes
    )
    thesis_reviews = _run_thesis_section(conn, config, provider, account_id, start, end, notes)
    risk = _run_risk_section(conn, config, provider, account_id, start, end)

    return ReviewPipelineResult(
        account_id=account_id,
        start=start.isoformat(),
        end=end.isoformat(),
        attribution=attribution,
        thesis_reviews=tuple(thesis_reviews),
        risk=risk,
        notes=tuple(notes),
    )


# ---------------------------------------------------------------------------
# 归因段
# ---------------------------------------------------------------------------


def _run_attribution_section(
    conn: duckdb.DuckDBPyConnection,
    config: Config,
    provider: LLMProvider,
    account_id: str,
    start: date,
    end: date,
    benchmark_code: str,
    notes: list[str],
) -> AttributionSection:
    try:
        bundle = load_brinson_input(conn, account_id, start, end, benchmark_code=benchmark_code)
        if bundle is None:
            return AttributionSection(status="unavailable")
        notes.extend(bundle.notes)

        result = compute_brinson(
            account_id=account_id,
            start=start,
            end=end,
            segments=bundle.segments,
            cash_weight=bundle.cash_weight,
            index_return=bundle.index_return,
            new_positions=bundle.new_positions,
        )
        if result is None:
            return AttributionSection(status="unavailable")

        run_id, run = run_attribution_review_agent(conn, config, provider, result=result)
        if run.status == STATUS_SUCCESS and isinstance(
            run.validation.parsed, AttributionReviewOutput
        ):
            return AttributionSection(
                status="ok",
                run_id=run_id,
                agent_status=run.status,
                active_return=result.active_return,
            )
        return AttributionSection(
            status="failed",
            run_id=run_id,
            agent_status=run.status,
            active_return=result.active_return,
            error="; ".join(run.validation.errors) or "归因解读 Agent 未成功",
        )
    except Exception as exc:  # 归因段隔离：不影响 thesis / 风控段
        return AttributionSection(status="failed", error=f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# thesis 逐仓复核段
# ---------------------------------------------------------------------------


def _run_thesis_section(
    conn: duckdb.DuckDBPyConnection,
    config: Config,
    provider: LLMProvider,
    account_id: str,
    start: date,
    end: date,
    notes: list[str],
) -> list[ThesisSectionItem]:
    holdings = replay_holdings(conn, account_id, start)
    items: list[ThesisSectionItem] = []
    skipped = 0

    for code in sorted(holdings):
        try:
            item = _review_one_position(conn, config, provider, account_id, code, start, end)
        except Exception as exc:  # 单仓隔离
            item = ThesisSectionItem(
                code=code, status="failed", reason=f"{type(exc).__name__}: {exc}"
            )
        items.append(item)
        if item.status == "skipped":
            skipped += 1

    if skipped:
        notes.append(f"thesis 复核跳过 {skipped}/{len(items)} 个期初持仓（无 active thesis 关联）")
    return items


def _review_one_position(
    conn: duckdb.DuckDBPyConnection,
    config: Config,
    provider: LLMProvider,
    account_id: str,
    code: str,
    start: date,
    end: date,
) -> ThesisSectionItem:
    """单仓 thesis 复核装配 + 调用；不符合复核条件返回 skipped 项。"""
    pos = conn.execute(
        "SELECT position_id, mode, thesis_id FROM position "
        "WHERE account_id = ? AND code = ? LIMIT 1",
        [account_id, code],
    ).fetchone()
    if pos is None:
        return ThesisSectionItem(
            code=code, status="skipped", reason="position 表无持仓记录（可能区间内已清仓）"
        )
    position_id, mode, thesis_id = pos
    if not thesis_id:
        return ThesisSectionItem(
            code=code,
            position_id=str(position_id),
            status="skipped",
            reason="持仓未关联 thesis（波段轨/手工建仓不做 thesis 复核）",
        )

    thesis = conn.execute(
        "SELECT summary, assumptions, status FROM thesis WHERE thesis_id = ?",
        [thesis_id],
    ).fetchone()
    if thesis is None:
        return ThesisSectionItem(
            code=code,
            position_id=str(position_id),
            status="skipped",
            reason=f"thesis 记录不存在: {thesis_id}",
        )
    summary, assumptions, thesis_status = thesis
    if thesis_status != "active":
        return ThesisSectionItem(
            code=code,
            position_id=str(position_id),
            status="skipped",
            reason=f"thesis 状态为 {thesis_status}，非 active 不重复复核",
        )

    stock = conn.execute(
        "SELECT market, name FROM dim_stock WHERE code = ? LIMIT 1", [code]
    ).fetchone()
    market = str(stock[0]) if stock else "?"
    name = str(stock[1]) if stock else code

    review_input = ThesisReviewInput(
        position_id=str(position_id),
        code=code,
        market=market,
        name=name,
        mode=str(mode),
        start=start,
        end=end,
        thesis_summary=str(summary),
        thesis_assumptions=str(assumptions) if assumptions else "无结构化关键假设记录",
        period_facts=_build_period_facts(conn, code, start, end),
    )
    run_id, run = run_thesis_review_agent(conn, config, provider, review_input=review_input)
    if run.status == STATUS_SUCCESS and isinstance(run.validation.parsed, ThesisReviewOutput):
        parsed = run.validation.parsed
        return ThesisSectionItem(
            code=code,
            status="success",
            position_id=parsed.position_id,
            run_id=run_id,
            thesis_status=parsed.thesis_status,
            suggested_action=parsed.suggested_action,
        )
    return ThesisSectionItem(
        code=code,
        position_id=str(position_id),
        status="failed",
        run_id=run_id,
        reason="; ".join(run.validation.errors) or "thesis 复核 Agent 未成功",
    )


# ---------------------------------------------------------------------------
# 风控段
# ---------------------------------------------------------------------------


def _run_risk_section(
    conn: duckdb.DuckDBPyConnection,
    config: Config,
    provider: LLMProvider,
    account_id: str,
    start: date,
    end: date,
) -> RiskSection:
    try:
        events = _load_risk_events(conn, account_id, start, end)
        if not events:
            return RiskSection(status="empty", event_count=0)
        run_id, run = run_risk_interpreter_agent(
            conn,
            config,
            provider,
            account_id=account_id,
            start=start,
            end=end,
            events=events,
        )
        if run.status == STATUS_SUCCESS and isinstance(run.validation.parsed, RiskInterpretOutput):
            return RiskSection(
                status="ok", run_id=run_id, agent_status=run.status, event_count=len(events)
            )
        return RiskSection(
            status="failed",
            run_id=run_id,
            agent_status=run.status,
            event_count=len(events),
            error="; ".join(run.validation.errors) or "风控解读 Agent 未成功",
        )
    except Exception as exc:  # 风控段隔离
        return RiskSection(status="failed", error=f"{type(exc).__name__}: {exc}")


def _load_risk_events(
    conn: duckdb.DuckDBPyConnection, account_id: str, start: date, end: date
) -> list[RiskEventInput]:
    """区间风控事件（event_ts 为 TIMESTAMPTZ，CAST 去时区后按日过滤）。"""
    rows = conn.execute(
        """
        SELECT event_id, rule_key, severity, decision, message, code,
               CAST(event_ts AS TIMESTAMP)
        FROM risk_event
        WHERE account_id = ?
              AND CAST(event_ts AS DATE) BETWEEN ? AND ?
        ORDER BY event_ts, event_id
        """,
        [account_id, start, end],
    ).fetchall()
    return [
        RiskEventInput(
            event_id=str(r[0]),
            rule_key=str(r[1]),
            severity=str(r[2]),
            decision=str(r[3]),
            message="" if r[4] is None else str(r[4]),
            code=r[5],
            event_ts=r[6].isoformat() if r[6] is not None else None,
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# thesis 区间事实装配（行情 + 期间财报 + 期间公告）
# ---------------------------------------------------------------------------


def _fmt_scalar(value: Any) -> str:
    """None/NaN → 「—」；金额按亿/万缩写，其余保留有效数字。"""
    if value is None:
        return "—"
    num = float(value)
    if math.isnan(num):
        return "—"
    if abs(num) >= 1e8:
        return f"{num / 1e8:.2f}亿"
    if abs(num) >= 1e4:
        return f"{num / 1e4:.2f}万"
    return f"{num:.4g}"


def _build_period_facts(conn: duckdb.DuckDBPyConnection, code: str, start: date, end: date) -> str:
    """装配区间事实文本：行情要点 + 期间新披露财报 + 期间公告（point-in-time）。

    财报/公告按披露日 ann_date 落在 [start, end] 过滤，天然前视安全
    （区间复盘只看区间内已公开信息）。无任何事实时如实说明，不编造。
    """
    lines: list[str] = []

    p0 = _adj_close(conn, code, start)
    p1 = _adj_close(conn, code, end)
    if p0 is not None and p1 is not None:
        lines.append(f"行情：期初复权收盘 {p0:.2f}，期末 {p1:.2f}，区间收益 {p1 / p0 - 1:+.2%}")
    else:
        lines.append("行情：区间内缺有效收盘价，无法计算区间收益")
    rng = conn.execute(
        """
        SELECT max(high), min(low) FROM fact_daily_bar
        WHERE code = ? AND trade_date BETWEEN ? AND ? AND high IS NOT NULL
        """,
        [code, start, end],
    ).fetchone()
    if rng and rng[0] is not None:
        lines.append(f"区间未复权最高价 {float(rng[0]):.2f}，最低价 {float(rng[1]):.2f}")

    fins = conn.execute(
        """
        SELECT report_period, ann_date, revenue, net_profit, roe, gross_margin,
               debt_ratio, ocf
        FROM fact_financial
        WHERE code = ? AND ann_date BETWEEN ? AND ?
        ORDER BY ann_date
        """,
        [code, start, end],
    ).fetchall()
    if fins:
        lines.append("期间披露财报：")
        for period, ann, rev, np_, roe, gm, dr, ocf in fins:
            lines.append(
                f"- 报告期 {period}（披露日 {ann}）：营收 {_fmt_scalar(rev)}，"
                f"净利润 {_fmt_scalar(np_)}，ROE {_fmt_scalar(roe)}，"
                f"毛利率 {_fmt_scalar(gm)}，资产负债率 {_fmt_scalar(dr)}，"
                f"经营现金流 {_fmt_scalar(ocf)}"
            )
    else:
        lines.append("期间无新披露财报记录")

    discs = conn.execute(
        """
        SELECT ann_date, category, title, summary FROM fact_disclosure
        WHERE code = ? AND ann_date BETWEEN ? AND ?
        ORDER BY ann_date LIMIT ?
        """,
        [code, start, end, _DISCLOSURE_LIMIT],
    ).fetchall()
    if discs:
        lines.append("期间公告：")
        for ann, category, title, summary in discs:
            head = f"- {ann} [{category or '未分类'}] {title}"
            if summary:
                head += f"：{str(summary)[:120]}"
            lines.append(head)
    else:
        lines.append("期间无公告记录")

    return "\n".join(lines)


def _adj_close(conn: duckdb.DuckDBPyConnection, code: str, as_of: date) -> float | None:
    """as-of 复权收盘（backward 对齐）；与 loader 同口径，NaN/无数据返回 None。"""
    row = conn.execute(
        """
        SELECT close * COALESCE(adj_factor, 1.0) FROM fact_daily_bar
        WHERE code = ? AND trade_date <= ? AND close IS NOT NULL AND close > 0
        ORDER BY trade_date DESC LIMIT 1
        """,
        [code, as_of],
    ).fetchone()
    if not row:
        return None
    value = float(row[0])
    return value if value == value else None
