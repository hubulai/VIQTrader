"""P4.6 复盘/风控三 Agent 契约校验测试。"""

from __future__ import annotations

import pytest

from viqtrader.agents.contracts.base import ContractError
from viqtrader.agents.contracts.review import (
    AttributionReviewContract,
    RiskInterpretContract,
    ThesisReviewContract,
)


def _attribution_payload(**overrides) -> dict:
    payload = {
        "summary": "区间组合收益 3.8%，基准 1.8%，超额 2.0%。",
        "allocation_comment": "超配食品饮料贡献主要配置效应。",
        "selection_comment": "食品饮料段内选股跑赢基准。",
        "key_findings": ["现金拖累 -0.3%", "食品饮料行业集中度偏高"],
        "data_refs": ["Brinson 归因结果 segments", "index_return 参考口径"],
    }
    payload.update(overrides)
    return payload


def _thesis_payload(**overrides) -> dict:
    payload = {
        "position_id": "pos_001",
        "thesis_status": "partially_broken",
        "changes": ["毛利率连续两季下滑 3pt"],
        "suggested_action": "reduce",
        "reasoning": "成本优势仍在，但成长逻辑已破，降仓不清仓。",
        "data_refs": ["区间事实：2026Q1 财报", "thesis 关键假设"],
    }
    payload.update(overrides)
    return payload


def _risk_payload(**overrides) -> dict:
    payload = {
        "overall_risk": "warning",
        "items": [
            {
                "rule_key": "swing.max_positions",
                "interpretation": "波段持仓数达到上限 5 只。",
                "suggested_action": "新开仓前先将持仓压缩至 4 只以内。",
            }
        ],
        "summary": "区间出现 1 条 WARN，无 CRITICAL 事件。",
        "data_refs": ["风控事件 swing.max_positions"],
    }
    payload.update(overrides)
    return payload


# ---------- 组合归因解读 ----------


def test_attribution_contract_valid() -> None:
    out = AttributionReviewContract().parse(_attribution_payload())
    assert out.summary.startswith("区间组合收益")
    assert len(out.key_findings) == 2
    assert out.data_refs[0] == "Brinson 归因结果 segments"


def test_attribution_contract_rejects_empty_findings() -> None:
    with pytest.raises(ContractError) as exc:
        AttributionReviewContract().parse(_attribution_payload(key_findings=[]))
    assert any("key_findings" in e for e in exc.value.errors)


def test_attribution_contract_rejects_empty_data_refs() -> None:
    with pytest.raises(ContractError) as exc:
        AttributionReviewContract().parse(_attribution_payload(data_refs=[]))
    assert any("data_refs" in e for e in exc.value.errors)


def test_attribution_contract_rejects_missing_comment() -> None:
    with pytest.raises(ContractError) as exc:
        AttributionReviewContract().parse(_attribution_payload(selection_comment=""))
    assert any("selection_comment" in e for e in exc.value.errors)


# ---------- 单持仓 thesis 复核 ----------


def test_thesis_contract_valid_partial() -> None:
    out = ThesisReviewContract(expected_position_id="pos_001").parse(_thesis_payload())
    assert out.thesis_status == "partially_broken"
    assert out.suggested_action == "reduce"


def test_thesis_contract_falsified_requires_exit() -> None:
    with pytest.raises(ContractError) as exc:
        ThesisReviewContract(expected_position_id="pos_001").parse(
            _thesis_payload(thesis_status="falsified", suggested_action="reduce")
        )
    assert any("falsified" in e and "exit" in e for e in exc.value.errors)


def test_thesis_contract_falsified_exit_valid() -> None:
    out = ThesisReviewContract(expected_position_id="pos_001").parse(
        _thesis_payload(thesis_status="falsified", suggested_action="exit")
    )
    assert out.thesis_status == "falsified"
    assert out.suggested_action == "exit"


def test_thesis_contract_rejects_unknown_status() -> None:
    with pytest.raises(ContractError) as exc:
        ThesisReviewContract(expected_position_id="pos_001").parse(
            _thesis_payload(thesis_status="moon")
        )
    assert any("thesis_status" in e for e in exc.value.errors)


def test_thesis_contract_rejects_unknown_action() -> None:
    with pytest.raises(ContractError) as exc:
        ThesisReviewContract(expected_position_id="pos_001").parse(
            _thesis_payload(suggested_action="buy")
        )
    assert any("suggested_action" in e for e in exc.value.errors)


def test_thesis_contract_rejects_position_mismatch() -> None:
    with pytest.raises(ContractError) as exc:
        ThesisReviewContract(expected_position_id="pos_001").parse(
            _thesis_payload(position_id="pos_999")
        )
    assert any("pos_001" in e for e in exc.value.errors)


def test_thesis_contract_rejects_empty_changes() -> None:
    with pytest.raises(ContractError) as exc:
        ThesisReviewContract(expected_position_id="pos_001").parse(_thesis_payload(changes=[]))
    assert any("changes" in e for e in exc.value.errors)


def test_thesis_contract_rejects_empty_data_refs() -> None:
    with pytest.raises(ContractError) as exc:
        ThesisReviewContract(expected_position_id="pos_001").parse(_thesis_payload(data_refs=[]))
    assert any("data_refs" in e for e in exc.value.errors)


# ---------- 风控解读 ----------


def test_risk_contract_valid() -> None:
    out = RiskInterpretContract().parse(_risk_payload())
    assert out.overall_risk == "warning"
    assert out.items[0].rule_key == "swing.max_positions"


def test_risk_contract_rejects_bad_level() -> None:
    with pytest.raises(ContractError) as exc:
        RiskInterpretContract().parse(_risk_payload(overall_risk="doom"))
    assert any("overall_risk" in e for e in exc.value.errors)


def test_risk_contract_rejects_empty_items() -> None:
    with pytest.raises(ContractError) as exc:
        RiskInterpretContract().parse(_risk_payload(items=[]))
    assert any("items" in e for e in exc.value.errors)


def test_risk_contract_rejects_non_object_item() -> None:
    with pytest.raises(ContractError) as exc:
        RiskInterpretContract().parse(_risk_payload(items=["坏条目"]))
    assert any("items[0]" in e for e in exc.value.errors)


def test_risk_contract_rejects_empty_data_refs() -> None:
    with pytest.raises(ContractError) as exc:
        RiskInterpretContract().parse(_risk_payload(data_refs=[]))
    assert any("data_refs" in e for e in exc.value.errors)
