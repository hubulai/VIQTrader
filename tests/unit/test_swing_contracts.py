"""P4.5 波段双 Agent 契约校验测试。"""

from __future__ import annotations

import pytest

from viqtrader.agents.contracts.base import ContractError
from viqtrader.agents.contracts.swing import (
    SignalInterpretContract,
    SwingPickContract,
)

_BOUNDS = (9.5, 10.5)


def _pick_payload(**overrides) -> dict:
    payload = {
        "code": "600001",
        "signal_type": "breakout",
        "pattern_desc": "收盘创 20 日新高且放量",
        "entry_ref": 10.2,
        "stop_ref": 9.8,
        "target_ref": 10.4,
        "risks": ["假突破", "量能背离"],
    }
    payload.update(overrides)
    return payload


def test_pick_contract_valid() -> None:
    out = SwingPickContract(code="600001", price_bounds=_BOUNDS).parse(_pick_payload())
    assert out.signal_type == "breakout"
    assert out.entry_ref == 10.2
    assert out.risks == ("假突破", "量能背离")


def test_pick_contract_rejects_unknown_signal_type() -> None:
    with pytest.raises(ContractError) as exc:
        SwingPickContract(code="600001", price_bounds=_BOUNDS).parse(
            _pick_payload(signal_type="moon")
        )
    assert any("signal_type" in e for e in exc.value.errors)


def test_pick_contract_rejects_ref_outside_kline_bounds() -> None:
    with pytest.raises(ContractError) as exc:
        SwingPickContract(code="600001", price_bounds=_BOUNDS).parse(_pick_payload(target_ref=12.0))
    assert any("真实价格区间" in e for e in exc.value.errors)


def test_pick_contract_rejects_bad_long_structure() -> None:
    contract = SwingPickContract(code="600001", price_bounds=_BOUNDS)
    with pytest.raises(ContractError) as exc:
        contract.parse(_pick_payload(stop_ref=10.3))  # stop >= entry
    assert any("stop_ref" in e for e in exc.value.errors)
    with pytest.raises(ContractError) as exc:
        contract.parse(_pick_payload(target_ref=10.0))  # target <= entry
    assert any("target_ref" in e for e in exc.value.errors)


def test_pick_contract_rejects_code_mismatch() -> None:
    with pytest.raises(ContractError) as exc:
        SwingPickContract(code="600001", price_bounds=_BOUNDS).parse(_pick_payload(code="000002"))
    assert any("600001" in e for e in exc.value.errors)


def test_pick_contract_none_allows_null_refs() -> None:
    out = SwingPickContract(code="600001", price_bounds=_BOUNDS).parse(
        _pick_payload(
            signal_type="none",
            pattern_desc="形态不明确",
            entry_ref=None,
            stop_ref=None,
            target_ref=None,
        )
    )
    assert out.signal_type == "none"
    assert out.entry_ref is None


def test_interpret_contract_valid() -> None:
    out = SignalInterpretContract().parse(
        {
            "interpretation": "这是放量突破形态，属右侧机会。",
            "invalidation_conditions": ["收盘跌回突破位下方", "跌破 9.8"],
            "key_risks": ["假突破"],
            "win_rate_note": "暂无历史样本，胜率待积累",
        }
    )
    assert out.invalidation_conditions == ("收盘跌回突破位下方", "跌破 9.8")
    assert "待积累" in out.win_rate_note


def test_interpret_contract_rejects_empty_invalidation() -> None:
    with pytest.raises(ContractError) as exc:
        SignalInterpretContract().parse(
            {
                "interpretation": "x",
                "invalidation_conditions": [],
                "key_risks": [],
                "win_rate_note": "暂无历史样本，胜率待积累",
            }
        )
    assert any("invalidation_conditions" in e for e in exc.value.errors)


def test_interpret_contract_rejects_missing_win_rate_note() -> None:
    with pytest.raises(ContractError):
        SignalInterpretContract().parse(
            {
                "interpretation": "x",
                "invalidation_conditions": ["跌破 9.8"],
                "key_risks": [],
            }
        )
