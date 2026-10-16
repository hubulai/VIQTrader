"""反方 Agent 输出契约（设计方案 4.1 ③，P4.2 样板之二）。

投委会交叉验证的另一侧：逐条攻击估值方假设、找财务雷点、验证护城河是否被高估。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from viqtrader.agents.contracts.base import (
    BaseContract,
    ContractError,
    as_list,
    as_str,
    as_str_list,
    nn,
)

_SEVERITIES = ("low", "medium", "high")


@dataclass(frozen=True, slots=True)
class Rebuttal:
    target: str
    attack: str
    severity: str


@dataclass(frozen=True, slots=True)
class DissentOutput:
    code: str
    rebuttals: tuple[Rebuttal, ...]
    fatal_issues: tuple[str, ...]
    verdict: str
    needs_data: tuple[str, ...]


class DissentContract(BaseContract[DissentOutput]):
    """校验反方 Agent 的 JSON 输出。"""

    name: str = "dissent"
    schema_hint: str = (
        '{"code": str, "rebuttals": [{"target": str, "attack": str, '
        '"severity": "low|medium|high"}], "fatal_issues": [str], "verdict": str, '
        '"needs_data": [str]}'
    )

    def parse(self, payload: dict[str, Any]) -> DissentOutput:
        errors: list[str] = []
        code = as_str(payload.get("code"), errors, "code")

        rebuttals: list[Rebuttal] = []
        raw_rebuttals = as_list(payload.get("rebuttals"), errors, "rebuttals")
        if raw_rebuttals is not None:
            if not raw_rebuttals:
                errors.append("rebuttals 不得为空（反方必须给出攻击点）")
            for index, item in enumerate(raw_rebuttals):
                path = f"rebuttals[{index}]"
                if not isinstance(item, dict):
                    errors.append(f"{path} 必须是对象")
                    continue
                target = as_str(item.get("target"), errors, f"{path}.target")
                attack = as_str(item.get("attack"), errors, f"{path}.attack")
                severity = as_str(item.get("severity"), errors, f"{path}.severity")
                if severity is not None and severity not in _SEVERITIES:
                    errors.append(f"{path}.severity 非法: {severity}（可选 {list(_SEVERITIES)}）")
                if target is not None and attack is not None and severity is not None:
                    rebuttals.append(Rebuttal(target, attack, severity))

        fatal_issues = as_str_list(payload.get("fatal_issues"), errors, "fatal_issues")
        verdict = as_str(payload.get("verdict"), errors, "verdict")
        needs_data = as_str_list(payload.get("needs_data"), errors, "needs_data")

        if errors:
            raise ContractError(errors)
        return DissentOutput(
            code=nn(code),
            rebuttals=tuple(rebuttals),
            fatal_issues=nn(fatal_issues),
            verdict=nn(verdict),
            needs_data=nn(needs_data),
        )


__all__ = ["DissentContract", "DissentOutput", "Rebuttal"]
