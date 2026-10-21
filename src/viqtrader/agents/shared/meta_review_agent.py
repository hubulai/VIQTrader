"""元复盘 Agent 装配（P7）：横切六类原始事实 → 提案草稿 → 反方审查 → 人工拍板。

输入红线（设计评审拍板）：只装配**原始事实**——评分卡历史序列、窗口内新判定
标签、案例库失败模式聚类、波段生命周期 KPI、风控事件、当前配置全文+历史快照。
**不装配任何其他 Agent 的观点输出**（agent_output 的消费结论不进 prompt，防递归污染）。

生效路径红线：本模块只负责产出与状态流转；提案批准后由人工手动合并 user.yaml，
平台不存在任何自动改写配置的路径。

分层：agents 层，可 import learning/store；不得 import orchestrator/web。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Any

import duckdb
import yaml

from viqtrader.agents.contracts.base import ContractRun, run_contract
from viqtrader.agents.contracts.meta_review import MetaReviewContract, MetaReviewOutput
from viqtrader.agents.contracts.meta_review_dissent import MetaReviewDissentContract
from viqtrader.agents.contracts.persist import persist_contract_run
from viqtrader.agents.llm.base import LLMProvider, Message
from viqtrader.agents.prompts.registry import PromptRegistry
from viqtrader.config.loader import Config

__all__ = [
    "DissentInput",
    "MetaReviewInput",
    "apply_dissent_result",
    "assemble_dissent_input",
    "assemble_meta_review_input",
    "list_proposals",
    "persist_proposals",
    "proposal_id_for",
    "render_yaml_snippet",
    "run_meta_review_agent",
    "run_meta_review_dissent",
    "transition_proposal",
]

# 提案状态机：draft → dissent_reviewed → approved / rejected → applied
_TRANSITIONS: dict[str, frozenset[str]] = {
    "approve": frozenset({"draft", "dissent_reviewed"}),
    "reject": frozenset({"draft", "dissent_reviewed", "approved"}),
    "applied": frozenset({"approved"}),
}
_NEW_STATUS = {"approve": "approved", "reject": "rejected", "applied": "applied"}

_CASE_DETAIL_CAP = 20  # 案例明细最多送入行数（聚类计数不受限）
_RISK_EVENT_CAP = 50  # 风控事件明细最多送入行数
_CONFIG_SNAPSHOT_CAP = 20  # 配置快照历史最多送入行数


# ---------------------------------------------------------------------------
# 输入装配（六节原始事实）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MetaReviewInput:
    """元复盘 prompt 的六节渲染文本（均为原始事实，无其他 Agent 观点）。"""

    window_start: date
    window_end: date
    window_line: str
    scorecard_series: str
    new_labels: str
    case_clusters: str
    lifecycle_kpi: str
    risk_events: str
    config_history: str

    def snapshot(self) -> dict[str, Any]:
        """agent_run.input_snapshot 用的可序列化视图。"""
        return {
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "scorecard_series": self.scorecard_series,
            "new_labels": self.new_labels,
            "case_clusters": self.case_clusters,
            "lifecycle_kpi": self.lifecycle_kpi,
            "risk_events": self.risk_events,
            "config_history": self.config_history,
        }


def _pct(value: Any) -> str:
    return "—" if value is None else f"{float(value):.1%}"


def _render_scorecards(conn: duckdb.DuckDBPyConnection) -> str:
    rows = conn.execute(
        """
        SELECT agent_name, decision_type, window_end, sample_size,
               hit_rate, error_mean, error_std, false_positive_rate
        FROM agent_scorecard
        ORDER BY agent_name, decision_type, window_end
        """
    ).fetchall()
    if not rows:
        return "（无评分卡记录）"
    lines = []
    for agent, dtype, wend, n, hit, err_mean, err_std, fp in rows:
        err = "—"
        if err_mean is not None:
            err = f"误差均值 {float(err_mean):+.3f}"
            if err_std is not None:
                err += f"（std {float(err_std):.3f}）"
        lines.append(
            f"- {agent}/{dtype}@{wend}：样本 {n}，命中率 {_pct(hit)}，误报率 {_pct(fp)}，{err}"
        )
    return "\n".join(lines)


def _render_new_labels(
    conn: duckdb.DuckDBPyConnection, window_start: date, window_end: date
) -> str:
    rows = conn.execute(
        """
        SELECT decision_type, decision_ref, code, label_kind, horizon,
               label_date, return_pct, hit, detail_json
        FROM outcome_label
        WHERE label_date IS NOT NULL AND label_date BETWEEN ? AND ?
        ORDER BY decision_type, decision_ref, label_kind, horizon
        """,
        [window_start, window_end],
    ).fetchall()
    if not rows:
        return "（窗口内无新判定标签）"
    lines = [f"窗口内新判定标签共 {len(rows)} 枚："]
    for dtype, dref, code, kind, horizon, label_date, ret, hit, detail in rows:
        ret_text = f"{float(ret):+.2%}" if ret is not None else "—"
        hit_text = "—" if hit is None else ("命中" if hit else "未命中")
        lines.append(
            f"- {dtype} {dref}（{code}）{kind}/{horizon}：判定日 {label_date}，"
            f"区间收益 {ret_text}，{hit_text}，明细 {detail or '{}'}"
        )
    return "\n".join(lines)


def _render_case_clusters(conn: duckdb.DuckDBPyConnection) -> str:
    rows = conn.execute(
        """
        SELECT case_id, code, industry, failure_mode, title, postmortem,
               CAST(created_at AS TIMESTAMP)
        FROM case_library ORDER BY created_at DESC
        """
    ).fetchall()
    if not rows:
        return "（案例库为空）"

    clusters: dict[str, list[str]] = {}
    success = 0
    for case_id, _code, _industry, failure_mode, title, _pm, _ts in rows:
        if failure_mode is None:
            success += 1
        else:
            clusters.setdefault(str(failure_mode), []).append(f"{case_id}《{title}》")

    lines = ["失败模式聚类："]
    if clusters:
        for mode, cases in sorted(clusters.items(), key=lambda kv: -len(kv[1])):
            lines.append(f"- 「{mode}」x{len(cases)}：{'；'.join(cases)}")
    else:
        lines.append("- （无失败案例）")
    lines.append(f"成功案例（failure_mode 为空）x{success}")

    lines.append(f"案例明细（最近 {_CASE_DETAIL_CAP} 条）：")
    for case_id, code, industry, _fm, title, postmortem, _ts in rows[:_CASE_DETAIL_CAP]:
        summary = (postmortem or "").strip().replace("\n", " ")[:120]
        lines.append(f"- {case_id} [{code}/{industry or '—'}]《{title}》 {summary}")
    return "\n".join(lines)


def _render_lifecycle_kpi(conn: duckdb.DuckDBPyConnection) -> str:
    rows = conn.execute(
        """
        SELECT d.name, d.version, s.strategy_id, s.stage, s.capital_pct,
               s.kpi_json, CAST(s.updated_at AS TIMESTAMP)
        FROM strategy_stage s
        JOIN strategy_definition d ON d.strategy_id = s.strategy_id
        ORDER BY d.name, d.version
        """
    ).fetchall()
    if not rows:
        return "（无波段策略生命周期记录）"
    lines = []
    for name, version, sid, stage, capital_pct, kpi_json, updated in rows:
        lines.append(
            f"- 策略 {name} v{version}（{sid}）：阶段 {stage}，资金配额 {float(capital_pct):.1%}，"
            f"KPI {kpi_json or '{}'}（更新于 {updated}）"
        )
    return "\n".join(lines)


def _render_risk_events(
    conn: duckdb.DuckDBPyConnection, window_start: date, window_end: date
) -> str:
    counts = conn.execute(
        """
        SELECT severity, count(*) FROM risk_event
        WHERE CAST(event_ts AS DATE) BETWEEN ? AND ?
        GROUP BY severity
        """,
        [window_start, window_end],
    ).fetchall()
    if not counts:
        return "（窗口内无风控事件）"
    total = sum(int(c) for _, c in counts)
    summary = " / ".join(f"{sev} {int(c)}" for sev, c in sorted(counts))
    lines = [f"窗口内风控事件共 {total} 条（{summary}），最近 {_RISK_EVENT_CAP} 条："]
    rows = conn.execute(
        """
        SELECT CAST(event_ts AS TIMESTAMP), severity, rule_key, code, decision, message
        FROM risk_event
        WHERE CAST(event_ts AS DATE) BETWEEN ? AND ?
        ORDER BY event_ts DESC
        LIMIT ?
        """,
        [window_start, window_end, _RISK_EVENT_CAP],
    ).fetchall()
    for ts, severity, rule_key, code, decision, message in rows:
        lines.append(f"- [{severity}] {ts} {rule_key}（{code or '—'}）{decision}：{message or '—'}")
    return "\n".join(lines)


def _render_config_history(conn: duckdb.DuckDBPyConnection, config: Config) -> str:
    lines = ["当前生效配置全文（default+user 合并，快照口径）：", config.canonical_yaml()]
    rows = conn.execute(
        """
        SELECT config_hash, CAST(recorded_at AS TIMESTAMP), note
        FROM config_snapshot ORDER BY recorded_at DESC LIMIT ?
        """,
        [_CONFIG_SNAPSHOT_CAP],
    ).fetchall()
    lines.append(f"历史配置快照（最近 {_CONFIG_SNAPSHOT_CAP} 条）：")
    if not rows:
        lines.append("- （无快照记录）")
    for config_hash, recorded, note in rows:
        lines.append(f"- {str(config_hash)[:12]}… @{recorded} {note or ''}")
    return "\n".join(lines)


def assemble_meta_review_input(
    conn: duckdb.DuckDBPyConnection,
    config: Config,
    *,
    window_start: date,
    window_end: date,
) -> MetaReviewInput:
    """装配六节原始事实；空节渲染占位符，空库安全。"""
    days = (window_end - window_start).days
    return MetaReviewInput(
        window_start=window_start,
        window_end=window_end,
        window_line=f"复盘窗口：{window_start} ~ {window_end}（{days} 天）",
        scorecard_series=_render_scorecards(conn),
        new_labels=_render_new_labels(conn, window_start, window_end),
        case_clusters=_render_case_clusters(conn),
        lifecycle_kpi=_render_lifecycle_kpi(conn),
        risk_events=_render_risk_events(conn, window_start, window_end),
        config_history=_render_config_history(conn, config),
    )


# ---------------------------------------------------------------------------
# 元复盘主 Agent（deep 模型：横切推理为重任务）
# ---------------------------------------------------------------------------


def run_meta_review_agent(
    conn: duckdb.DuckDBPyConnection,
    config: Config,
    provider: LLMProvider,
    *,
    review_input: MetaReviewInput,
) -> tuple[str, ContractRun]:
    """执行元复盘 Agent，返回 (run_id, 契约运行结果)。

    SUCCESS 时 parsed 为 MetaReviewOutput；DEGRADED/FAILED 不落 agent_output。
    横切学习为重推理任务，使用深度模型（llm.model_deep）。
    """
    registry = PromptRegistry()
    contract = MetaReviewContract()
    prompt = registry.render(
        "meta_review",
        window=review_input.window_line,
        scorecard_series=review_input.scorecard_series,
        new_labels=review_input.new_labels,
        case_clusters=review_input.case_clusters,
        lifecycle_kpi=review_input.lifecycle_kpi,
        risk_events=review_input.risk_events,
        config_history=review_input.config_history,
        schema_hint=contract.schema_hint,
    )
    model = str(config.get("llm.model_deep"))
    run = run_contract(provider, contract, [Message("user", prompt)], model)
    run_id = persist_contract_run(
        conn,
        config,
        run,
        agent_name="meta_review",
        prompt_version=registry.version("meta_review"),
        input_snapshot=review_input.snapshot(),
    )
    return run_id, run


def proposal_id_for(window_end: date, seq: int) -> str:
    """learning_proposal 主键口径：窗口止日 + 序号（确定性幂等锚点）。"""
    return f"meta_review:{window_end.isoformat()}:{seq}"


def persist_proposals(
    conn: duckdb.DuckDBPyConnection,
    *,
    window_start: date,
    window_end: date,
    output: MetaReviewOutput,
) -> tuple[list[str], int]:
    """逐条落 learning_proposal（draft）。返回 (新插入 id 列表, 幂等跳过数)。

    同 proposal_id 已存在时跳过（保留原状态——重跑 --run 不得覆盖已审查/已拍板提案）。
    """
    inserted: list[str] = []
    skipped = 0
    for seq, proposal in enumerate(output.proposals, start=1):
        proposal_id = proposal_id_for(window_end, seq)
        exists = conn.execute(
            "SELECT 1 FROM learning_proposal WHERE proposal_id = ?", [proposal_id]
        ).fetchone()
        if exists is not None:
            skipped += 1
            continue
        conn.execute(
            """
            INSERT INTO learning_proposal (
                proposal_id, window_start, window_end, kind, title,
                payload_json, rationale, evidence_json, confidence, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft')
            """,
            [
                proposal_id,
                window_start,
                window_end,
                proposal.kind,
                proposal.title,
                json.dumps(proposal.payload, ensure_ascii=False, default=str),
                proposal.rationale,
                json.dumps(list(proposal.evidence_refs), ensure_ascii=False),
                proposal.confidence,
            ],
        )
        inserted.append(proposal_id)
    return inserted, skipped


# ---------------------------------------------------------------------------
# 反方审查（cheap 模型：方法论审查为轻任务）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DissentInput:
    """反方审查 prompt 的渲染文本。"""

    proposal_ids: tuple[str, ...]
    window_line: str
    proposals_text: str
    evidence_digest: str


def assemble_dissent_input(
    conn: duckdb.DuckDBPyConnection,
    config: Config,
    *,
    proposal_ids: list[str],
) -> DissentInput | None:
    """按提案 id 装配反方输入；提案不存在返回 None。

    evidence_digest 复用元复盘同窗口的六节原始事实，供反方核验统计有效性与
    递归污染（证据必须指向原始事实而非其他 Agent 观点）。
    """
    if not proposal_ids:
        return None
    placeholders = ", ".join("?" for _ in proposal_ids)
    rows = conn.execute(
        f"""
        SELECT proposal_id, window_start, window_end, kind, title,
               payload_json, rationale, evidence_json, confidence
        FROM learning_proposal
        WHERE proposal_id IN ({placeholders})
        ORDER BY proposal_id
        """,
        list(proposal_ids),
    ).fetchall()
    if not rows:
        return None

    window_start = min(r[1] for r in rows)
    window_end = max(r[2] for r in rows)

    blocks: list[str] = []
    ids: list[str] = []
    for index, (
        pid,
        _ws,
        _we,
        kind,
        title,
        payload_json,
        rationale,
        evidence_json,
        conf,
    ) in enumerate(rows):
        ids.append(str(pid))
        blocks.append(
            f"[{index}] {pid}（kind={kind}，confidence={conf}）《{title}》\n"
            f"    payload: {payload_json}\n"
            f"    rationale: {rationale}\n"
            f"    evidence_refs: {evidence_json}"
        )

    raw = assemble_meta_review_input(conn, config, window_start=window_start, window_end=window_end)
    digest = (
        f"{raw.scorecard_series}\n\n{raw.new_labels}\n\n{raw.case_clusters}\n\n"
        f"{raw.lifecycle_kpi}\n\n{raw.risk_events}\n\n{raw.config_history}"
    )
    return DissentInput(
        proposal_ids=tuple(ids),
        window_line=raw.window_line,
        proposals_text="\n\n".join(blocks),
        evidence_digest=digest,
    )


def run_meta_review_dissent(
    conn: duckdb.DuckDBPyConnection,
    config: Config,
    provider: LLMProvider,
    *,
    dissent_input: DissentInput,
) -> tuple[str, ContractRun]:
    """执行元复盘反方审查，返回 (run_id, 契约运行结果)。轻任务用便宜模型。"""
    registry = PromptRegistry()
    contract = MetaReviewDissentContract()
    prompt = registry.render(
        "meta_review_dissent",
        window=dissent_input.window_line,
        proposals=dissent_input.proposals_text,
        evidence_digest=dissent_input.evidence_digest,
        schema_hint=contract.schema_hint,
    )
    model = str(config.get("llm.model_cheap"))
    run = run_contract(provider, contract, [Message("user", prompt)], model)
    run_id = persist_contract_run(
        conn,
        config,
        run,
        agent_name="meta_review_dissent",
        prompt_version=registry.version("meta_review_dissent"),
        input_snapshot={
            "proposal_ids": list(dissent_input.proposal_ids),
            "proposals_text": dissent_input.proposals_text,
            "evidence_digest": dissent_input.evidence_digest,
        },
    )
    return run_id, run


def apply_dissent_result(
    conn: duckdb.DuckDBPyConnection,
    run_id: str,
    proposal_ids: tuple[str, ...],
) -> str:
    """反方审查 SUCCESS 后：提案 draft → dissent_reviewed 并记录 dissent_output_id。

    返回 dissent agent_output.output_id。仅更新仍为 draft 的提案（已拍板不动）。
    """
    row = conn.execute("SELECT output_id FROM agent_output WHERE run_id = ?", [run_id]).fetchone()
    if row is None:
        raise ValueError(f"agent_output 缺失（run 非 SUCCESS？）: {run_id}")
    output_id = str(row[0])
    for pid in proposal_ids:
        conn.execute(
            """
            UPDATE learning_proposal
            SET status = 'dissent_reviewed', dissent_output_id = ?, updated_at = now()
            WHERE proposal_id = ? AND status = 'draft'
            """,
            [output_id, pid],
        )
    return output_id


# ---------------------------------------------------------------------------
# 状态机与 YAML 片段（人工拍板闸）
# ---------------------------------------------------------------------------


def transition_proposal(
    conn: duckdb.DuckDBPyConnection,
    proposal_id: str,
    action: str,
    *,
    applied_config_hash: str | None = None,
) -> tuple[str, str]:
    """提案状态迁移。返回 (旧状态, 新状态)；同状态重复操作幂等返回。

    - action ∈ approve / reject / applied；
    - 提案不存在抛 LookupError；非法迁移抛 ValueError；
    - applied 必须携带 applied_config_hash（人工合并配置后的快照哈希）。
    """
    if action not in _NEW_STATUS:
        raise ValueError(f"未知操作: {action}（可选 {sorted(_NEW_STATUS)}）")
    row = conn.execute(
        "SELECT status FROM learning_proposal WHERE proposal_id = ?", [proposal_id]
    ).fetchone()
    if row is None:
        raise LookupError(f"提案不存在: {proposal_id}")
    old = str(row[0])
    new = _NEW_STATUS[action]
    if old == new:
        return old, new  # 幂等
    if old not in _TRANSITIONS[action]:
        raise ValueError(
            f"非法状态迁移: {old} → {new}（{action} 仅允许 {sorted(_TRANSITIONS[action])}）"
        )
    if action == "applied":
        if not applied_config_hash:
            raise ValueError("applied 必须携带 applied_config_hash")
        conn.execute(
            """
            UPDATE learning_proposal
            SET status = ?, applied_config_hash = ?, updated_at = now()
            WHERE proposal_id = ?
            """,
            [new, applied_config_hash, proposal_id],
        )
    else:
        conn.execute(
            """
            UPDATE learning_proposal
            SET status = ?, updated_at = now()
            WHERE proposal_id = ?
            """,
            [new, proposal_id],
        )
    return old, new


def render_yaml_snippet(kind: str, payload: dict[str, Any]) -> str | None:
    """param_change 提案 → 人工合并 user.yaml 用的 YAML 片段；其他 kind 返回 None。"""
    if kind != "param_change":
        return None
    config_path = payload.get("config_path")
    if not isinstance(config_path, str) or not config_path:
        return None
    parts = config_path.split(".")
    nested: dict[str, Any] = {}
    node = nested
    for part in parts[:-1]:
        nxt: dict[str, Any] = {}
        node[part] = nxt
        node = nxt
    node[parts[-1]] = payload.get("proposed")
    body = yaml.safe_dump(nested, allow_unicode=True, sort_keys=False).rstrip()
    return (
        f"# config_path: {config_path}\n"
        f"# 当前值: {payload.get('current')!r} → 建议值: {payload.get('proposed')!r}\n"
        f"{body}"
    )


def list_proposals(
    conn: duckdb.DuckDBPyConnection,
    *,
    window_start: date | None = None,
    window_end: date | None = None,
) -> list[dict[str, Any]]:
    """列出提案（可选窗口过滤），payload/evidence 已解析为对象。"""
    sql = """
        SELECT proposal_id, window_start, window_end, kind, title, payload_json,
               rationale, evidence_json, confidence, status, dissent_output_id,
               applied_config_hash, CAST(created_at AS TIMESTAMP), CAST(updated_at AS TIMESTAMP)
        FROM learning_proposal
    """
    params: list[Any] = []
    if window_start is not None and window_end is not None:
        sql += " WHERE window_start >= ? AND window_end <= ?"
        params.extend([window_start, window_end])
    sql += " ORDER BY window_end DESC, proposal_id"
    rows = conn.execute(sql, params).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "proposal_id": row[0],
                "window_start": str(row[1]),
                "window_end": str(row[2]),
                "kind": row[3],
                "title": row[4],
                "payload": json.loads(row[5]),
                "rationale": row[6],
                "evidence_refs": json.loads(row[7]),
                "confidence": row[8],
                "status": row[9],
                "dissent_output_id": row[10],
                "applied_config_hash": row[11],
                "created_at": str(row[12]),
                "updated_at": str(row[13]),
            }
        )
    return out
