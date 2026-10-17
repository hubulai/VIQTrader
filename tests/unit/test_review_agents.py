"""P4.6 复盘/风控三 Agent 入口测试（内存库 + FakeProvider，全离线）。

覆盖：成功路径落 agent_run + agent_output、模型选择（cheap/deep）、
空学习库降级文案注入、DEGRADED 不落 agent_output、空事件 fail-fast。
pipeline 编排与失败隔离在 a2 的 test_review_pipeline 覆盖。
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from viqtrader.agents.llm.fake import FakeProvider
from viqtrader.agents.llm.runs import STATUS_DEGRADED, STATUS_SUCCESS
from viqtrader.agents.shared.review.attribution_agent import run_attribution_review_agent
from viqtrader.agents.shared.review.thesis_agent import (
    ThesisReviewInput,
    run_thesis_review_agent,
)
from viqtrader.agents.shared.risk_interpreter import (
    RiskEventInput,
    run_risk_interpreter_agent,
)
from viqtrader.attribution.brinson import SegmentInput, compute_brinson

_EMPTY_LEARNING = "暂无历史案例与评分卡记录"


def _brinson_result():
    segments = [
        SegmentInput("食品饮料", 0.30, 0.10, 0.20, 0.05),
        SegmentInput("银行", 0.40, 0.02, 0.80, 0.01),
    ]
    result = compute_brinson(
        account_id="acc_value",
        start=date(2026, 7, 1),
        end=date(2026, 9, 30),
        segments=segments,
        cash_weight=0.30,
        index_return=0.03,
    )
    assert result is not None  # 测试前置：构造合法输入
    return result


_ATTRIBUTION_JSON = json.dumps(
    {
        "summary": "组合收益 3.8%，基准 1.8%，超额 2.0%。",
        "allocation_comment": "超配食品饮料贡献配置效应。",
        "selection_comment": "食品饮料段内选股显著跑赢。",
        "key_findings": ["现金拖累 -0.5%"],
        "data_refs": ["Brinson 归因结果 segments"],
    },
    ensure_ascii=False,
)

_THESIS_JSON = json.dumps(
    {
        "position_id": "pos_001",
        "thesis_status": "intact",
        "changes": ["无显著变化"],
        "suggested_action": "hold",
        "reasoning": "成本优势与渠道壁垒均未出现裂痕，继续持有。",
        "data_refs": ["区间事实：2026 中报", "thesis 关键假设"],
    },
    ensure_ascii=False,
)

_RISK_JSON = json.dumps(
    {
        "overall_risk": "warning",
        "items": [
            {
                "rule_key": "swing.max_positions",
                "interpretation": "波段持仓数达到上限。",
                "suggested_action": "新开仓前先压缩至 4 只以内。",
            }
        ],
        "summary": "区间 1 条 WARN，无 CRITICAL。",
        "data_refs": ["风控事件 swing.max_positions"],
    },
    ensure_ascii=False,
)


def _thesis_input() -> ThesisReviewInput:
    return ThesisReviewInput(
        position_id="pos_001",
        code="600519",
        market="SH",
        name="贵州茅台",
        mode="value",
        start=date(2026, 7, 1),
        end=date(2026, 9, 30),
        thesis_summary="品牌护城河带来定价权，量价齐升。",
        thesis_assumptions='["毛利率稳定在 90% 以上", "批价不大幅下跌"]',
        period_facts="2026 中报：毛利率 91.8%，同比 +0.2pt；批价区间 2400~2500。",
    )


def _risk_events() -> list[RiskEventInput]:
    return [
        RiskEventInput(
            event_id="evt_1",
            rule_key="swing.max_positions",
            severity="WARN",
            decision="DENY",
            message="波段持仓数已达上限 5 只",
            code="600001",
            event_ts="2026-08-11T10:00:00",
        )
    ]


# ---------- 组合归因解读 ----------


def test_attribution_agent_success_persists(conn, config) -> None:
    provider = FakeProvider(content=_ATTRIBUTION_JSON)
    result = _brinson_result()

    run_id, run = run_attribution_review_agent(conn, config, provider, result=result)
    assert run.status == STATUS_SUCCESS

    row = conn.execute(
        "SELECT agent_name, model, prompt_version, status, input_snapshot "
        "FROM agent_run WHERE run_id = ?",
        [run_id],
    ).fetchone()
    assert row[0] == "attribution_review"
    assert row[1] == config.get("llm.model_cheap")  # 解读类走便宜模型
    assert row[2] == "attribution_review.v1"
    assert row[3] == "SUCCESS"
    snapshot = json.loads(row[4])
    assert snapshot["account_id"] == "acc_value"
    assert snapshot["brinson_result"]["active_return"] > 0

    out = conn.execute("SELECT output_json FROM agent_output WHERE run_id = ?", [run_id]).fetchone()
    assert out is not None
    assert "食品饮料" in json.loads(out[0])["allocation_comment"]

    prompt = provider.calls[0][0][0].content
    assert "组合归因解读 Agent" in prompt
    assert "acc_value" in prompt
    assert _EMPTY_LEARNING in prompt  # 空学习库降级注入


def test_attribution_agent_degraded_no_output_row(conn, config) -> None:
    provider = FakeProvider(content="这不是JSON")
    run_id, run = run_attribution_review_agent(conn, config, provider, result=_brinson_result())
    assert run.status == STATUS_DEGRADED
    assert (
        conn.execute(
            "SELECT count(*) FROM agent_run WHERE run_id = ? AND status = 'DEGRADED'",
            [run_id],
        ).fetchone()[0]
        == 1
    )
    assert (
        conn.execute("SELECT count(*) FROM agent_output WHERE run_id = ?", [run_id]).fetchone()[0]
        == 0
    )


# ---------- 单持仓 thesis 复核 ----------


def test_thesis_agent_success_uses_deep_model(conn, config) -> None:
    provider = FakeProvider(content=_THESIS_JSON)
    run_id, run = run_thesis_review_agent(conn, config, provider, review_input=_thesis_input())
    assert run.status == STATUS_SUCCESS
    assert provider.calls[0][1] == config.get("llm.model_deep")  # 卖出级判断走强模型

    row = conn.execute(
        "SELECT prompt_version, input_snapshot FROM agent_run WHERE run_id = ?",
        [run_id],
    ).fetchone()
    assert row[0] == "thesis_review.v1"
    snapshot = json.loads(row[1])
    assert snapshot["position_id"] == "pos_001"
    assert "2026 中报" in snapshot["period_facts"]

    prompt = provider.calls[0][0][0].content
    assert "持仓 thesis 复核 Agent" in prompt
    assert "600519" in prompt
    assert _EMPTY_LEARNING in prompt


def test_thesis_agent_degraded_persists_run_only(conn, config) -> None:
    provider = FakeProvider(content="坏响应")
    run_id, run = run_thesis_review_agent(conn, config, provider, review_input=_thesis_input())
    assert run.status == STATUS_DEGRADED
    assert (
        conn.execute("SELECT count(*) FROM agent_output WHERE run_id = ?", [run_id]).fetchone()[0]
        == 0
    )


# ---------- 风控解读 ----------


def test_risk_interpreter_success_persists(conn, config) -> None:
    provider = FakeProvider(content=_RISK_JSON)
    run_id, run = run_risk_interpreter_agent(
        conn,
        config,
        provider,
        account_id="acc_swing",
        start=date(2026, 7, 1),
        end=date(2026, 9, 30),
        events=_risk_events(),
    )
    assert run.status == STATUS_SUCCESS
    assert provider.calls[0][1] == config.get("llm.model_cheap")

    row = conn.execute(
        "SELECT agent_name, prompt_version, input_snapshot FROM agent_run WHERE run_id = ?",
        [run_id],
    ).fetchone()
    assert row[0] == "risk_interpret"
    assert row[1] == "risk_interpret.v1"
    events = json.loads(row[2])["events"]
    assert events[0]["rule_key"] == "swing.max_positions"

    prompt = provider.calls[0][0][0].content
    assert "风控解读 Agent" in prompt
    assert "swing.max_positions" in prompt


def test_risk_interpreter_empty_events_fails_fast(conn, config) -> None:
    provider = FakeProvider(content=_RISK_JSON)
    with pytest.raises(ValueError, match="风控事件列表为空"):
        run_risk_interpreter_agent(
            conn,
            config,
            provider,
            account_id="acc_swing",
            start=date(2026, 7, 1),
            end=date(2026, 9, 30),
            events=[],
        )
    assert provider.calls == []  # 无 LLM 调用


def test_risk_interpreter_bad_period_fails_fast(conn, config) -> None:
    provider = FakeProvider(content=_RISK_JSON)
    with pytest.raises(ValueError, match="区间非法"):
        run_risk_interpreter_agent(
            conn,
            config,
            provider,
            account_id="acc_swing",
            start=date(2026, 9, 30),
            end=date(2026, 7, 1),
            events=_risk_events(),
        )
    assert provider.calls == []
