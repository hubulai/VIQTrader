"""P7 元复盘单测：契约校验 / 六节装配 / 提案落库幂等 / 反方审查 / 状态机 / YAML 片段。"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import pytest

from viqtrader.agents.contracts.base import ContractError
from viqtrader.agents.contracts.meta_review import MetaReviewContract, MetaReviewOutput
from viqtrader.agents.contracts.meta_review_dissent import MetaReviewDissentContract
from viqtrader.agents.llm.fake import FakeProvider
from viqtrader.agents.shared.meta_review_agent import (
    apply_dissent_result,
    assemble_dissent_input,
    assemble_meta_review_input,
    list_proposals,
    persist_proposals,
    render_yaml_snippet,
    run_meta_review_agent,
    run_meta_review_dissent,
    transition_proposal,
)

WINDOW_START = date(2026, 1, 1)
WINDOW_END = date(2026, 4, 1)

# ---------------------------------------------------------------------------
# 契约校验
# ---------------------------------------------------------------------------


def _valid_payload() -> dict[str, Any]:
    return {
        "window_summary": "窗口内新增 2 枚波段标签，均触止损。",
        "sample_caveat": "样本仅 2 枚，全部按定性处理。",
        "proposals": [
            {
                "kind": "param_change",
                "title": "放宽波段止损",
                "payload": {
                    "config_path": "swing_track.stop_loss_pct",
                    "current": 0.03,
                    "proposed": 0.05,
                },
                "rationale": "两笔均为止损过紧被震出；放宽或降低换手但放大单笔亏损。",
                "evidence_refs": ["输入② outcome_label swing_pick sig1 touch 未命中"],
                "confidence": "low",
            },
            {
                "kind": "prompt_change",
                "title": "初筛 prompt 强调量能确认",
                "payload": {"target": "swing_pick", "direction": "强调放量确认，不代笔"},
                "rationale": "失败模式聚类显示假突破占比高。",
                "evidence_refs": ["输入③ 案例库 失败模式「假突破」x2"],
                "confidence": "medium",
            },
        ],
        "data_refs": ["输入②", "输入③"],
    }


def test_meta_review_contract_valid() -> None:
    out = MetaReviewContract().parse(_valid_payload())
    assert len(out.proposals) == 2
    assert out.proposals[0].kind == "param_change"
    assert out.proposals[0].payload["proposed"] == 0.05
    assert out.proposals[1].confidence == "medium"
    assert out.sample_caveat is not None


def test_meta_review_contract_allows_empty_proposals() -> None:
    payload = _valid_payload()
    payload["proposals"] = []
    out = MetaReviewContract().parse(payload)
    assert out.proposals == ()


def test_meta_review_contract_rejects_bad_kind() -> None:
    payload = _valid_payload()
    payload["proposals"][0]["kind"] = "auto_trade"
    with pytest.raises(ContractError, match="kind"):
        MetaReviewContract().parse(payload)


def test_meta_review_contract_rejects_empty_evidence() -> None:
    payload = _valid_payload()
    payload["proposals"][0]["evidence_refs"] = []
    with pytest.raises(ContractError, match="evidence_refs"):
        MetaReviewContract().parse(payload)


def test_meta_review_contract_rejects_bad_confidence() -> None:
    payload = _valid_payload()
    payload["proposals"][0]["confidence"] = "certain"
    with pytest.raises(ContractError, match="confidence"):
        MetaReviewContract().parse(payload)


def test_meta_review_contract_param_change_requires_config_path() -> None:
    payload = _valid_payload()
    del payload["proposals"][0]["payload"]["config_path"]
    with pytest.raises(ContractError, match="config_path"):
        MetaReviewContract().parse(payload)


def test_meta_review_contract_prompt_change_requires_direction() -> None:
    payload = _valid_payload()
    del payload["proposals"][1]["payload"]["direction"]
    with pytest.raises(ContractError, match="direction"):
        MetaReviewContract().parse(payload)


def test_meta_review_contract_rejects_empty_data_refs() -> None:
    payload = _valid_payload()
    payload["data_refs"] = []
    with pytest.raises(ContractError, match="data_refs"):
        MetaReviewContract().parse(payload)


def _valid_dissent_payload() -> dict[str, Any]:
    return {
        "overall": "两条提案方法论上均可放行，但样本量均不足。",
        "verdicts": [
            {
                "proposal_index": 0,
                "approved": False,
                "verdict": "2 笔样本不足以支持调参。",
                "concerns": ["统计有效性不足", "对噪声调参嫌疑"],
                "questions": ["能否等满 20 笔再评估？"],
            },
            {
                "proposal_index": 1,
                "approved": True,
                "verdict": "案例聚类为原始事实，方法论可放行。",
                "concerns": [],
                "questions": [],
            },
        ],
        "data_refs": ["输入：待审提案", "输入：原始事实"],
    }


def test_dissent_contract_valid() -> None:
    out = MetaReviewDissentContract().parse(_valid_dissent_payload())
    assert len(out.verdicts) == 2
    assert out.verdicts[0].approved is False
    assert out.verdicts[1].concerns == ()


def test_dissent_contract_reject_requires_concerns() -> None:
    payload = _valid_dissent_payload()
    payload["verdicts"][0]["concerns"] = []
    with pytest.raises(ContractError, match="concerns"):
        MetaReviewDissentContract().parse(payload)


def test_dissent_contract_rejects_negative_index() -> None:
    payload = _valid_dissent_payload()
    payload["verdicts"][0]["proposal_index"] = -1
    with pytest.raises(ContractError, match="proposal_index"):
        MetaReviewDissentContract().parse(payload)


# ---------------------------------------------------------------------------
# 六节装配
# ---------------------------------------------------------------------------


def test_assemble_input_empty_db_safe(conn, config) -> None:  # type: ignore[no-untyped-def]
    review_input = assemble_meta_review_input(
        conn, config, window_start=WINDOW_START, window_end=WINDOW_END
    )
    assert "无评分卡记录" in review_input.scorecard_series
    assert "无新判定标签" in review_input.new_labels
    assert "案例库为空" in review_input.case_clusters
    assert "无波段策略生命周期记录" in review_input.lifecycle_kpi
    assert "无风控事件" in review_input.risk_events
    assert "无快照记录" in review_input.config_history
    assert "llm" in review_input.config_history  # 当前配置全文仍渲染


def _seed_facts(conn) -> None:  # type: ignore[no-untyped-def]
    conn.execute(
        """
        INSERT INTO agent_scorecard (
            agent_name, decision_type, window_end, sample_size,
            hit_rate, false_positive_rate
        ) VALUES ('swing_pick', 'swing_pick', '2026-03-31', 12, 0.42, 0.25)
        """
    )
    conn.execute(
        """
        INSERT INTO outcome_label (
            decision_type, decision_ref, code, decision_date,
            label_kind, horizon, label_date, return_pct, hit, detail_json
        ) VALUES ('swing_pick', 'sig1', '600000', '2026-01-05', 'touch', 'first_touch',
                  '2026-01-08', -0.05, FALSE, '{"side":"stop"}')
        """
    )
    conn.execute(
        """
        INSERT INTO case_library (case_id, code, industry, failure_mode, title, postmortem)
        VALUES ('swing_pick:sig1', '600000', '银行', '假突破', '假突破回落', '缩量冲高。')
        """
    )
    conn.execute(
        """
        INSERT INTO strategy_definition (
            strategy_id, name, track, version, config_hash, code_version
        ) VALUES ('brk_v1', 'breakout', 'swing', 'v1', 'hash', '0.1.0')
        """
    )
    conn.execute(
        """
        INSERT INTO strategy_stage (strategy_id, stage, capital_pct, kpi_json)
        VALUES ('brk_v1', 'PROBATION', 0.05, '{"sharpe": 0.6}')
        """
    )
    conn.execute(
        """
        INSERT INTO risk_event (
            event_id, event_ts, account_id, rule_key, code, severity, decision, message,
            config_hash, code_version
        ) VALUES ('ev1', '2026-02-01 10:00:00', 'acc_swing', 'stop_loss', '600000',
                  'WARN', 'ALERT', '触发止损', 'hash', '0.1.0')
        """
    )
    conn.execute(
        "INSERT INTO config_snapshot (config_hash, yaml, note) VALUES ('abc123', 'x: 1', '基线')"
    )


def test_assemble_input_renders_seeded_facts(conn, config) -> None:  # type: ignore[no-untyped-def]
    _seed_facts(conn)
    review_input = assemble_meta_review_input(
        conn, config, window_start=WINDOW_START, window_end=WINDOW_END
    )
    assert "swing_pick/swing_pick@2026-03-31" in review_input.scorecard_series
    assert "42.0%" in review_input.scorecard_series
    assert "sig1" in review_input.new_labels
    assert "-5.00%" in review_input.new_labels
    assert "「假突破」x1" in review_input.case_clusters
    assert "breakout vv1" in review_input.lifecycle_kpi
    assert "PROBATION" in review_input.lifecycle_kpi
    assert "WARN 1" in review_input.risk_events
    assert "abc123" in review_input.config_history


# ---------------------------------------------------------------------------
# 提案落库幂等
# ---------------------------------------------------------------------------


def _output() -> MetaReviewOutput:
    return MetaReviewContract().parse(_valid_payload())


def test_persist_proposals_insert_and_idempotent_skip(conn) -> None:  # type: ignore[no-untyped-def]
    inserted, skipped = persist_proposals(
        conn, window_start=WINDOW_START, window_end=WINDOW_END, output=_output()
    )
    assert inserted == ["meta_review:2026-04-01:1", "meta_review:2026-04-01:2"]
    assert skipped == 0

    again_inserted, again_skipped = persist_proposals(
        conn, window_start=WINDOW_START, window_end=WINDOW_END, output=_output()
    )
    assert again_inserted == []
    assert again_skipped == 2

    rows = list_proposals(conn, window_start=WINDOW_START, window_end=WINDOW_END)
    assert len(rows) == 2
    assert rows[0]["status"] == "draft"
    assert rows[0]["payload"]["config_path"] == "swing_track.stop_loss_pct"
    assert rows[0]["evidence_refs"]


def test_run_meta_review_agent_success_persists_run(conn, config) -> None:  # type: ignore[no-untyped-def]
    review_input = assemble_meta_review_input(
        conn, config, window_start=WINDOW_START, window_end=WINDOW_END
    )
    provider = FakeProvider(content=json.dumps(_valid_payload()))
    run_id, run = run_meta_review_agent(conn, config, provider, review_input=review_input)
    assert run.status == "SUCCESS"
    audit = conn.execute(
        "SELECT agent_name, status, prompt_version FROM agent_run WHERE run_id = ?", [run_id]
    ).fetchone()
    assert audit == ("meta_review", "SUCCESS", "meta_review.v1")


# ---------------------------------------------------------------------------
# 反方审查
# ---------------------------------------------------------------------------


def test_dissent_flow_updates_proposals(conn, config) -> None:  # type: ignore[no-untyped-def]
    inserted, _ = persist_proposals(
        conn, window_start=WINDOW_START, window_end=WINDOW_END, output=_output()
    )
    dissent_input = assemble_dissent_input(conn, config, proposal_ids=inserted)
    assert dissent_input is not None
    assert dissent_input.proposal_ids == tuple(inserted)
    assert "[0]" in dissent_input.proposals_text and "[1]" in dissent_input.proposals_text

    provider = FakeProvider(content=json.dumps(_valid_dissent_payload()))
    run_id, run = run_meta_review_dissent(conn, config, provider, dissent_input=dissent_input)
    assert run.status == "SUCCESS"

    output_id = apply_dissent_result(conn, run_id, dissent_input.proposal_ids)
    rows = list_proposals(conn, window_start=WINDOW_START, window_end=WINDOW_END)
    assert all(r["status"] == "dissent_reviewed" for r in rows)
    assert all(r["dissent_output_id"] == output_id for r in rows)


def test_assemble_dissent_input_none_for_missing(conn, config) -> None:  # type: ignore[no-untyped-def]
    assert assemble_dissent_input(conn, config, proposal_ids=["meta_review:2026-04-01:9"]) is None
    assert assemble_dissent_input(conn, config, proposal_ids=[]) is None


# ---------------------------------------------------------------------------
# 状态机与 YAML 片段
# ---------------------------------------------------------------------------


def test_transition_approve_then_applied(conn, config) -> None:  # type: ignore[no-untyped-def]
    inserted, _ = persist_proposals(
        conn, window_start=WINDOW_START, window_end=WINDOW_END, output=_output()
    )
    pid = inserted[0]
    assert transition_proposal(conn, pid, "approve") == ("draft", "approved")
    assert transition_proposal(conn, pid, "approve") == ("approved", "approved")  # 幂等
    with pytest.raises(ValueError, match="applied_config_hash"):
        transition_proposal(conn, pid, "applied")
    old, new = transition_proposal(
        conn, pid, "applied", applied_config_hash=str(config.content_hash())
    )
    assert (old, new) == ("approved", "applied")
    row = conn.execute(
        "SELECT status, applied_config_hash FROM learning_proposal WHERE proposal_id = ?", [pid]
    ).fetchone()
    assert row[0] == "applied"
    assert row[1] == str(config.content_hash())
    # applied 终态：不可再拒绝
    with pytest.raises(ValueError, match="非法状态迁移"):
        transition_proposal(conn, pid, "reject")


def test_transition_reject_and_errors(conn) -> None:  # type: ignore[no-untyped-def]
    inserted, _ = persist_proposals(
        conn, window_start=WINDOW_START, window_end=WINDOW_END, output=_output()
    )
    pid = inserted[1]
    assert transition_proposal(conn, pid, "reject") == ("draft", "rejected")
    with pytest.raises(ValueError, match="非法状态迁移"):
        transition_proposal(conn, pid, "approve")
    with pytest.raises(LookupError, match="提案不存在"):
        transition_proposal(conn, "meta_review:2099-01-01:1", "approve")
    with pytest.raises(ValueError, match="未知操作"):
        transition_proposal(conn, pid, "revive")


def test_render_yaml_snippet() -> None:
    snippet = render_yaml_snippet(
        "param_change",
        {"config_path": "swing_track.stop_loss_pct", "current": 0.03, "proposed": 0.05},
    )
    assert snippet is not None
    assert "swing_track:" in snippet
    assert "stop_loss_pct: 0.05" in snippet
    assert "当前值: 0.03" in snippet
    assert render_yaml_snippet("thesis_retire", {"target": "600000"}) is None
