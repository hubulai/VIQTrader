"""深度估值 Agent 输出契约（设计方案 4.1 ②，P4.2 样板之一）。

强类型 dataclass 描述设计文档约定的输出 JSON；`parse` 逐字段校验，
不合格抛 ContractError（携带全部错误供重试提示）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from viqtrader.agents.contracts.base import (
    BaseContract,
    ContractError,
    as_list,
    as_number,
    as_str,
    as_str_list,
    nn,
)

_MOAT_RATINGS = ("none", "narrow", "strong", "wide")


@dataclass(frozen=True, slots=True)
class ValuationMethod:
    name: str
    value: float
    assumptions: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ValuationOutput:
    code: str
    intrinsic_value_range: tuple[float, float]
    current_price: float
    margin_of_safety: float
    methods: tuple[ValuationMethod, ...]
    moat_rating: str
    moat_evidence: tuple[str, ...]
    key_risks: tuple[str, ...]
    confidence: float
    variant_perception: str
    data_refs: tuple[str, ...]


class ValuationContract(BaseContract[ValuationOutput]):
    """校验深度估值 Agent 的 JSON 输出。"""

    name: str = "valuation"
    schema_hint: str = (
        '{"code": str, "intrinsic_value_range": [low, high], "current_price": num, '
        '"margin_of_safety": num, "methods": [{"name": str, "value": num, '
        '"assumptions": obj}], "moat_rating": "none|narrow|strong|wide", '
        '"moat_evidence": [str], "key_risks": [str], "confidence": num(0~1), '
        '"variant_perception": str, "data_refs": [str]'
    )

    def parse(self, payload: dict[str, Any]) -> ValuationOutput:
        errors: list[str] = []
        code = as_str(payload.get("code"), errors, "code")

        low: float | None = None
        high: float | None = None
        rng = as_list(payload.get("intrinsic_value_range"), errors, "intrinsic_value_range")
        if rng is not None:
            if len(rng) != 2:
                errors.append("intrinsic_value_range 必须是长度 2 的 [下界, 上界]")
            else:
                low = as_number(rng[0], errors, "intrinsic_value_range[0]")
                high = as_number(rng[1], errors, "intrinsic_value_range[1]")
                if low is not None and high is not None and low > high:
                    errors.append("intrinsic_value_range 下界不得大于上界")

        current_price = as_number(payload.get("current_price"), errors, "current_price")
        margin = as_number(payload.get("margin_of_safety"), errors, "margin_of_safety")

        methods: list[ValuationMethod] = []
        raw_methods = as_list(payload.get("methods"), errors, "methods")
        if raw_methods is not None:
            if not raw_methods:
                errors.append("methods 不得为空（须列全估值方法）")
            for index, item in enumerate(raw_methods):
                path = f"methods[{index}]"
                if not isinstance(item, dict):
                    errors.append(f"{path} 必须是对象")
                    continue
                name = as_str(item.get("name"), errors, f"{path}.name")
                value = as_number(item.get("value"), errors, f"{path}.value")
                assumptions = item.get("assumptions")
                if not isinstance(assumptions, dict):
                    errors.append(f"{path}.assumptions 必须是对象（须列全假设供反方攻击）")
                    assumptions = {}
                if name is not None and value is not None:
                    methods.append(ValuationMethod(name, value, assumptions))

        moat = as_str(payload.get("moat_rating"), errors, "moat_rating")
        if moat is not None and moat not in _MOAT_RATINGS:
            errors.append(f"moat_rating 非法: {moat}（可选 {list(_MOAT_RATINGS)}）")
        moat_evidence = as_str_list(payload.get("moat_evidence"), errors, "moat_evidence")
        key_risks = as_str_list(payload.get("key_risks"), errors, "key_risks")

        confidence = as_number(payload.get("confidence"), errors, "confidence")
        if confidence is not None and not 0.0 <= confidence <= 1.0:
            errors.append("confidence 必须在 [0, 1]")

        variant = as_str(payload.get("variant_perception"), errors, "variant_perception")
        data_refs = as_str_list(payload.get("data_refs"), errors, "data_refs")
        if data_refs is not None and not data_refs:
            errors.append("data_refs 不得为空（不允许无据推断）")

        if errors:
            raise ContractError(errors)
        return ValuationOutput(
            code=nn(code),
            intrinsic_value_range=(nn(low), nn(high)),
            current_price=nn(current_price),
            margin_of_safety=nn(margin),
            methods=tuple(methods),
            moat_rating=nn(moat),
            moat_evidence=nn(moat_evidence),
            key_risks=nn(key_risks),
            confidence=nn(confidence),
            variant_perception=nn(variant),
            data_refs=nn(data_refs),
        )


__all__ = ["ValuationContract", "ValuationMethod", "ValuationOutput"]
