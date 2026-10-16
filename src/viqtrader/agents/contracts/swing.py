"""波段轨双 Agent 输出契约（P4.5，设计方案 4.2）。

- SwingPickContract：波段选股——信号类型枚举 + 形态描述 + 进出场参考位 + 风险；
  参考位必须落在给定 K 线窗口真实价格区间内（构造时传入区间，防 LLM 虚构价位）。
- SignalInterpretContract：信号解读——人话翻译 + 失效条件 + 风险 + 历史胜率佐证；
  胜率佐证无样本时必须明示「待积累」，禁止编造数字。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from viqtrader.agents.contracts.base import (
    BaseContract,
    ContractError,
    as_number,
    as_str,
    as_str_list,
    nn,
)

SIGNAL_TYPES = ("breakout", "pullback", "reversal", "range", "none")


def _as_optional_number(value: Any, errors: list[str], path: str) -> float | None:
    """可空数值：JSON null 放行，其余按数字校验。"""
    if value is None:
        return None
    return as_number(value, errors, path)


@dataclass(frozen=True, slots=True)
class SwingPickOutput:
    """波段选股 Agent 产物。"""

    code: str
    signal_type: str  # breakout / pullback / reversal / range / none
    pattern_desc: str
    entry_ref: float | None
    stop_ref: float | None
    target_ref: float | None
    risks: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SignalInterpretOutput:
    """信号解读 Agent 产物。"""

    interpretation: str
    invalidation_conditions: tuple[str, ...]
    key_risks: tuple[str, ...]
    win_rate_note: str


class SwingPickContract(BaseContract[SwingPickOutput]):
    """校验波段选股 Agent 的 JSON 输出。

    price_bounds=(窗口最低价, 窗口最高价)：任何给出的参考位必须在该区间内，
    从机制上禁止虚构 K 线中不存在的价位。
    """

    name: str = "swing_pick"
    schema_hint: str = (
        '{"code": str, "signal_type": "breakout|pullback|reversal|range|none", '
        '"pattern_desc": str, "entry_ref": num|null, "stop_ref": num|null, '
        '"target_ref": num|null, "risks": [str]}'
    )

    def __init__(self, *, code: str, price_bounds: tuple[float, float] | None = None) -> None:
        self._expected_code = code
        self._bounds = price_bounds

    def parse(self, payload: dict[str, Any]) -> SwingPickOutput:
        errors: list[str] = []
        code = as_str(payload.get("code"), errors, "code")
        if code is not None and code != self._expected_code:
            errors.append(f"code 必须为请求标的 {self._expected_code}，得到 {code}")

        signal_type = as_str(payload.get("signal_type"), errors, "signal_type")
        if signal_type is not None and signal_type not in SIGNAL_TYPES:
            errors.append(f"signal_type 非法: {signal_type}（可选 {list(SIGNAL_TYPES)}）")

        pattern = as_str(payload.get("pattern_desc"), errors, "pattern_desc")

        entry_ref = _as_optional_number(payload.get("entry_ref"), errors, "entry_ref")
        stop_ref = _as_optional_number(payload.get("stop_ref"), errors, "stop_ref")
        target_ref = _as_optional_number(payload.get("target_ref"), errors, "target_ref")

        if self._bounds is not None:
            low, high = self._bounds
            for name, value in (
                ("entry_ref", entry_ref),
                ("stop_ref", stop_ref),
                ("target_ref", target_ref),
            ):
                if value is not None and not low <= value <= high:
                    errors.append(
                        f"{name}={value} 超出给定 K 线真实价格区间 [{low}, {high}]，"
                        "参考位必须取自给出的 K 线价位"
                    )
        if entry_ref is not None and stop_ref is not None and not stop_ref < entry_ref:
            errors.append("多头波段 stop_ref 必须严格小于 entry_ref")
        if entry_ref is not None and target_ref is not None and not target_ref > entry_ref:
            errors.append("多头波段 target_ref 必须严格大于 entry_ref")

        risks = as_str_list(payload.get("risks"), errors, "risks")

        if errors:
            raise ContractError(errors)
        return SwingPickOutput(
            code=nn(code),
            signal_type=nn(signal_type),
            pattern_desc=nn(pattern),
            entry_ref=entry_ref,
            stop_ref=stop_ref,
            target_ref=target_ref,
            risks=nn(risks),
        )


class SignalInterpretContract(BaseContract[SignalInterpretOutput]):
    """校验信号解读 Agent 的 JSON 输出。"""

    name: str = "signal_interpret"
    schema_hint: str = (
        '{"interpretation": str, "invalidation_conditions": [str], '
        '"key_risks": [str], "win_rate_note": str}'
    )

    def parse(self, payload: dict[str, Any]) -> SignalInterpretOutput:
        errors: list[str] = []
        interpretation = as_str(payload.get("interpretation"), errors, "interpretation")
        invalidation = as_str_list(
            payload.get("invalidation_conditions"), errors, "invalidation_conditions"
        )
        if invalidation is not None and not invalidation:
            errors.append("invalidation_conditions 不得为空（必须给出信号失效条件）")
        key_risks = as_str_list(payload.get("key_risks"), errors, "key_risks")
        win_rate_note = as_str(payload.get("win_rate_note"), errors, "win_rate_note")

        if errors:
            raise ContractError(errors)
        return SignalInterpretOutput(
            interpretation=nn(interpretation),
            invalidation_conditions=nn(invalidation),
            key_risks=nn(key_risks),
            win_rate_note=nn(win_rate_note),
        )


__all__ = [
    "SIGNAL_TYPES",
    "SignalInterpretContract",
    "SignalInterpretOutput",
    "SwingPickContract",
    "SwingPickOutput",
]
