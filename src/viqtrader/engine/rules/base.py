"""P2 三阶段规则管线（设计文档第三节）。

执行顺序：Phase 1 MARKET（能不能交易）→ Phase 2 ACCOUNT（有没有资源）
→ Phase 3 RISK（该不该交易）。任一规则 passed=False（QuotaRule 除外）
立即终止校验；WARN 级通过但记入告警。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from viqtrader.engine.types import (
    OrderIntent,
    RuleCheckResult,
    RuleContext,
    RulePhase,
    ValidationResult,
)


class TradingRule(Protocol):
    """交易规则契约：所有规则实现此 Protocol。"""

    rule_id: str
    phase: RulePhase

    def check(self, intent: OrderIntent, *, ctx: RuleContext) -> RuleCheckResult: ...


_PHASE_ORDER: tuple[RulePhase, ...] = (RulePhase.MARKET, RulePhase.ACCOUNT, RulePhase.RISK)


class TradingRuleEngine:
    """按阶段顺序执行规则管线。"""

    def __init__(self, rules: Sequence[TradingRule]) -> None:
        unknown = [r.rule_id for r in rules if r.phase not in _PHASE_ORDER]
        if unknown:
            raise ValueError(f"规则 phase 非法: {unknown}")
        self._rules: dict[RulePhase, list[TradingRule]] = {p: [] for p in _PHASE_ORDER}
        for rule in rules:
            self._rules[rule.phase].append(rule)

    def validate(self, intent: OrderIntent, ctx: RuleContext) -> ValidationResult:
        """依次执行三阶段；任一硬拒绝即终止，返回已执行的全部结果。"""
        results: list[RuleCheckResult] = []
        for phase in _PHASE_ORDER:
            for rule in self._rules[phase]:
                result = rule.check(intent, ctx=ctx)
                results.append(result)
                if not result.passed and result.severity != "WARN":
                    reason = f"{phase}/{result.rule_id}: {result.message}"
                    return ValidationResult(
                        passed=False, results=tuple(results), rejection_reason=reason
                    )
        return ValidationResult(passed=True, results=tuple(results))
