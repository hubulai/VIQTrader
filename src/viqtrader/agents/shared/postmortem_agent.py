"""案例尸检 Agent（P5.3）：LLM 草拟案例复盘，人工确认后才入 case_library。

输入 PostmortemInput 由调用方（scripts/postmortem.py）装配：决策快照（信号/估值
原始输入）+ 该决策的全部结果标签。本模块只做 prompt 渲染与契约运行，
草稿落文件与确认入库由 CLI 闸口负责（v1 无任何自动入库路径）。

分层：agents 层，可 import learning（下层）读取标签；不得 import orchestrator/web。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import duckdb

from viqtrader.agents.context import build_learning_context
from viqtrader.agents.contracts.base import ContractRun, run_contract
from viqtrader.agents.contracts.persist import persist_contract_run
from viqtrader.agents.contracts.postmortem import PostmortemContract
from viqtrader.agents.llm.base import LLMProvider, Message
from viqtrader.agents.prompts.registry import PromptRegistry
from viqtrader.config.loader import Config
from viqtrader.learning.labeler import DECISION_SWING, DECISION_VALUATION

__all__ = [
    "PostmortemCandidate",
    "PostmortemInput",
    "assemble_case_input",
    "case_id_for",
    "run_postmortem_agent",
    "scan_postmortem_candidates",
]


@dataclass(frozen=True, slots=True)
class PostmortemInput:
    """尸检草稿的装配输入（CLI 负责从库中组装）。"""

    decision_type: str  # swing_pick / valuation
    decision_ref: str  # signal_id / session_id
    code: str
    market: str | None
    industry: str | None  # 案例检索维度（dim_stock 反查结果，可 None）
    thesis_type: str | None  # 波段=signal_type；价值可 None
    decision_snapshot: str  # 决策原始输入渲染文本
    outcome_labels: str  # 全部结果标签渲染文本


def run_postmortem_agent(
    conn: duckdb.DuckDBPyConnection,
    config: Config,
    provider: LLMProvider,
    *,
    case_input: PostmortemInput,
) -> tuple[str, ContractRun]:
    """执行案例尸检 Agent，返回 (run_id, 契约运行结果)。

    成功时 parsed 为 PostmortemOutput；DEGRADED/FAILED 时 agent_output 不落库。
    尸检为轻量复盘任务，使用便宜模型（llm.model_cheap）。
    """
    registry = PromptRegistry()
    learning_context = build_learning_context(
        conn,
        config,
        agent_name="postmortem",
        decision_type="postmortem",
        code=case_input.code,
        market=case_input.market,
        thesis_type=case_input.thesis_type,
    )
    contract = PostmortemContract()
    prompt = registry.render(
        "postmortem",
        decision_snapshot=case_input.decision_snapshot,
        outcome_labels=case_input.outcome_labels,
        learning_context=learning_context,
        schema_hint=contract.schema_hint,
    )
    model = str(config.get("llm.model_cheap"))
    run = run_contract(provider, contract, [Message("user", prompt)], model)
    run_id = persist_contract_run(
        conn,
        config,
        run,
        agent_name="postmortem",
        prompt_version=registry.version("postmortem"),
        input_snapshot={
            "decision_type": case_input.decision_type,
            "decision_ref": case_input.decision_ref,
            "code": case_input.code,
            "decision_snapshot": case_input.decision_snapshot,
            "outcome_labels": case_input.outcome_labels,
            "learning_context": learning_context,
        },
    )
    return run_id, run


# ---------------------------------------------------------------------------
# 装配与候选扫描（IO 边界，供 CLI 使用）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PostmortemCandidate:
    """一个可尸检的决策（已有 ≥1 枚已判定标签，且未入库、无草稿）。"""

    decision_type: str
    decision_ref: str
    code: str
    label_count: int


def case_id_for(decision_type: str, decision_ref: str) -> str:
    """case_library 主键口径：决策类型 + 决策引用（天然幂等锚点）。"""
    return f"{decision_type}:{decision_ref}"


def _load_industry(conn: duckdb.DuckDBPyConnection, code: str) -> str | None:
    row = conn.execute(
        "SELECT industry FROM dim_stock WHERE code = ? AND industry IS NOT NULL LIMIT 1",
        [code],
    ).fetchone()
    return None if row is None else str(row[0])


def _render_labels(conn: duckdb.DuckDBPyConnection, decision_ref: str) -> tuple[str, int]:
    rows = conn.execute(
        """
        SELECT label_kind, horizon, label_date, return_pct, hit, detail_json
        FROM outcome_label WHERE decision_ref = ? ORDER BY label_kind, horizon
        """,
        [decision_ref],
    ).fetchall()
    lines: list[str] = []
    for kind, horizon, label_date, return_pct, hit, detail_json in rows:
        ret = f"{float(return_pct):+.2%}" if return_pct is not None else "—"
        lines.append(
            f"- {kind}/{horizon}：判定日 {label_date}，区间收益 {ret}，"
            f"命中 {'是' if hit else '否'}，明细 {detail_json or '{}'}"
        )
    return "\n".join(lines), len(rows)


def _assemble_swing(conn: duckdb.DuckDBPyConnection, decision_ref: str) -> PostmortemInput | None:
    row = conn.execute(
        """
        SELECT code, market, name, signal_date, signal_type, pattern_desc,
               entry_ref, stop_ref, target_ref, screen_features_json
        FROM swing_signal WHERE signal_id = ?
        """,
        [decision_ref],
    ).fetchone()
    if row is None:
        return None
    code, market, name, signal_date, signal_type, pattern_desc = row[:6]
    entry_ref, stop_ref, target_ref, features_json = row[6:]
    snapshot = (
        f"信号 {decision_ref}：{code} {name}（{market}），初筛日 {signal_date}\n"
        f"形态：{signal_type or '—'}（{pattern_desc or '—'}）\n"
        f"参考位：进场 {entry_ref} / 止损 {stop_ref} / 目标 {target_ref}\n"
        f"初筛特征：{features_json}"
    )
    labels_text, _ = _render_labels(conn, decision_ref)
    return PostmortemInput(
        decision_type=DECISION_SWING,
        decision_ref=decision_ref,
        code=str(code),
        market=str(market),
        industry=_load_industry(conn, str(code)),
        thesis_type=None if signal_type is None else str(signal_type),
        decision_snapshot=snapshot,
        outcome_labels=labels_text,
    )


def _assemble_valuation(
    conn: duckdb.DuckDBPyConnection, decision_ref: str
) -> PostmortemInput | None:
    row = conn.execute(
        """
        SELECT cs.code, cs.started_at, ao.output_json
        FROM committee_session cs
        JOIN agent_output ao ON ao.output_id = cs.valuation_output_id
        WHERE cs.session_id = ?
        """,
        [decision_ref],
    ).fetchone()
    if row is None:
        return None
    code, started_at, output_json = row
    snapshot = f"投委会 {decision_ref}：{code}，召开于 {started_at}\n估值产出 JSON：{output_json}"
    labels_text, _ = _render_labels(conn, decision_ref)
    return PostmortemInput(
        decision_type=DECISION_VALUATION,
        decision_ref=decision_ref,
        code=str(code),
        market=None,
        industry=_load_industry(conn, str(code)),
        thesis_type=None,
        decision_snapshot=snapshot,
        outcome_labels=labels_text,
    )


def assemble_case_input(
    conn: duckdb.DuckDBPyConnection, decision_type: str, decision_ref: str
) -> PostmortemInput | None:
    """按决策类型装配尸检输入；决策不存在返回 None。"""
    if decision_type == DECISION_SWING:
        return _assemble_swing(conn, decision_ref)
    if decision_type == DECISION_VALUATION:
        return _assemble_valuation(conn, decision_ref)
    raise ValueError(f"未知决策类型: {decision_type}")


def scan_postmortem_candidates(
    conn: duckdb.DuckDBPyConnection, *, drafts_dir: Path
) -> list[PostmortemCandidate]:
    """扫描可尸检决策：有已判定标签，未入 case_library，草稿文件也不存在。"""
    rows = conn.execute(
        """
        SELECT decision_type, decision_ref, max(code), count(*)
        FROM outcome_label
        GROUP BY decision_type, decision_ref
        ORDER BY decision_type, decision_ref
        """
    ).fetchall()
    existing_cases = {r[0] for r in conn.execute("SELECT case_id FROM case_library").fetchall()}
    candidates: list[PostmortemCandidate] = []
    for decision_type, decision_ref, code, label_count in rows:
        if case_id_for(str(decision_type), str(decision_ref)) in existing_cases:
            continue
        draft_file = drafts_dir / f"{decision_type}-{decision_ref}.json"
        if draft_file.exists():
            continue
        candidates.append(
            PostmortemCandidate(
                decision_type=str(decision_type),
                decision_ref=str(decision_ref),
                code=str(code),
                label_count=int(label_count),
            )
        )
    return candidates
