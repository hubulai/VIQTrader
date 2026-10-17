"""P4.6 复盘/风控三 Agent 输出契约（设计方案 4.3 ⑥⑧）。

- AttributionReviewContract：组合归因解读——把 Brinson 定量结果翻译成人话，
  必须分别解读行业配置效应与个股选择效应，禁止编输入之外的数字。
- ThesisReviewContract：单持仓 thesis 复核——对照原始买入逻辑判断是否仍成立，
  立场与设计方案一致：价格下跌本身不是证伪，只有基本面/逻辑事实变化才算。
- RiskInterpretContract：风控解读——把风控引擎的规则事件翻译成可读提示与建议动作。

枚举口径与设计方案 JSON 示例对齐：thesis_status 使用 intact/partially_broken/falsified，
suggested_action 使用 hold/reduce/exit。
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

THESIS_STATUSES = ("intact", "partially_broken", "falsified")
THESIS_ACTIONS = ("hold", "reduce", "exit")
RISK_LEVELS = ("normal", "warning", "critical")


@dataclass(frozen=True, slots=True)
class AttributionReviewOutput:
    """组合归因解读 Agent 产物。"""

    summary: str  # 区间表现总览（2~4 句）
    allocation_comment: str  # 行业配置效应解读
    selection_comment: str  # 个股选择效应解读
    key_findings: tuple[str, ...]  # 关键发现（现金拖累/行业集中/新建仓等）
    data_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ThesisReviewOutput:
    """单持仓 thesis 复核 Agent 产物。"""

    position_id: str
    thesis_status: str  # intact / partially_broken / falsified
    changes: tuple[str, ...]  # 相对建仓假设发生的关键变化（须为事实陈述）
    suggested_action: str  # hold / reduce / exit
    reasoning: str
    data_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RiskItem:
    """单条风控事件的解读。"""

    rule_key: str
    interpretation: str
    suggested_action: str


@dataclass(frozen=True, slots=True)
class RiskInterpretOutput:
    """风控解读 Agent 产物。"""

    overall_risk: str  # normal / warning / critical
    items: tuple[RiskItem, ...]
    summary: str
    data_refs: tuple[str, ...]


class AttributionReviewContract(BaseContract[AttributionReviewOutput]):
    """校验组合归因解读 Agent 的 JSON 输出。"""

    name: str = "attribution_review"
    schema_hint: str = (
        '{"summary": str, "allocation_comment": str, "selection_comment": str, '
        '"key_findings": [str], "data_refs": [str]}'
    )

    def parse(self, payload: dict[str, Any]) -> AttributionReviewOutput:
        errors: list[str] = []
        summary = as_str(payload.get("summary"), errors, "summary")
        allocation_comment = as_str(payload.get("allocation_comment"), errors, "allocation_comment")
        selection_comment = as_str(payload.get("selection_comment"), errors, "selection_comment")
        key_findings = as_str_list(payload.get("key_findings"), errors, "key_findings")
        if key_findings is not None and not key_findings:
            errors.append("key_findings 不得为空（至少给出一条关键发现）")
        data_refs = as_str_list(payload.get("data_refs"), errors, "data_refs")
        if data_refs is not None and not data_refs:
            errors.append("data_refs 不得为空（结论必须引用输入数据）")

        if errors:
            raise ContractError(errors)
        return AttributionReviewOutput(
            summary=nn(summary),
            allocation_comment=nn(allocation_comment),
            selection_comment=nn(selection_comment),
            key_findings=nn(key_findings),
            data_refs=nn(data_refs),
        )


class ThesisReviewContract(BaseContract[ThesisReviewOutput]):
    """校验单持仓 thesis 复核 Agent 的 JSON 输出。

    expected_position_id 由调用方传入：输出必须回显同一持仓，防止 LLM 串仓。
    """

    name: str = "thesis_review"
    schema_hint: str = (
        '{"position_id": str, "thesis_status": "intact|partially_broken|falsified", '
        '"changes": [str], "suggested_action": "hold|reduce|exit", '
        '"reasoning": str, "data_refs": [str]}'
    )

    def __init__(self, *, expected_position_id: str) -> None:
        self._expected_position_id = expected_position_id

    def parse(self, payload: dict[str, Any]) -> ThesisReviewOutput:
        errors: list[str] = []
        position_id = as_str(payload.get("position_id"), errors, "position_id")
        if position_id is not None and position_id != self._expected_position_id:
            errors.append(
                f"position_id 必须为请求持仓 {self._expected_position_id}，得到 {position_id}"
            )

        thesis_status = as_str(payload.get("thesis_status"), errors, "thesis_status")
        if thesis_status is not None and thesis_status not in THESIS_STATUSES:
            errors.append(f"thesis_status 非法: {thesis_status}（可选 {list(THESIS_STATUSES)}）")

        changes = as_str_list(payload.get("changes"), errors, "changes")
        if changes is not None and not changes:
            errors.append("changes 不得为空（无变化时也需明确给出「无显著变化」）")

        suggested_action = as_str(payload.get("suggested_action"), errors, "suggested_action")
        if suggested_action is not None and suggested_action not in THESIS_ACTIONS:
            errors.append(
                f"suggested_action 非法: {suggested_action}（可选 {list(THESIS_ACTIONS)}）"
            )

        # 价值轨纪律：thesis 证伪 → 必须建议退出，不允许带逻辑裂痕继续持有
        if thesis_status == "falsified" and suggested_action not in (None, "exit"):
            errors.append("thesis_status=falsified 时 suggested_action 必须为 exit")

        reasoning = as_str(payload.get("reasoning"), errors, "reasoning")
        data_refs = as_str_list(payload.get("data_refs"), errors, "data_refs")
        if data_refs is not None and not data_refs:
            errors.append("data_refs 不得为空（结论必须引用输入的事实数据）")

        if errors:
            raise ContractError(errors)
        return ThesisReviewOutput(
            position_id=nn(position_id),
            thesis_status=nn(thesis_status),
            changes=nn(changes),
            suggested_action=nn(suggested_action),
            reasoning=nn(reasoning),
            data_refs=nn(data_refs),
        )


class RiskInterpretContract(BaseContract[RiskInterpretOutput]):
    """校验风控解读 Agent 的 JSON 输出。"""

    name: str = "risk_interpret"
    schema_hint: str = (
        '{"overall_risk": "normal|warning|critical", '
        '"items": [{"rule_key": str, "interpretation": str, "suggested_action": str}], '
        '"summary": str, "data_refs": [str]}'
    )

    def parse(self, payload: dict[str, Any]) -> RiskInterpretOutput:
        errors: list[str] = []
        overall_risk = as_str(payload.get("overall_risk"), errors, "overall_risk")
        if overall_risk is not None and overall_risk not in RISK_LEVELS:
            errors.append(f"overall_risk 非法: {overall_risk}（可选 {list(RISK_LEVELS)}）")

        items: list[RiskItem] = []
        raw_items = as_list(payload.get("items"), errors, "items")
        if raw_items is not None:
            if not raw_items:
                errors.append("items 不得为空（每条风控事件都必须解读）")
            for index, item in enumerate(raw_items):
                path = f"items[{index}]"
                if not isinstance(item, dict):
                    errors.append(f"{path} 必须是对象")
                    continue
                rule_key = as_str(item.get("rule_key"), errors, f"{path}.rule_key")
                interpretation = as_str(
                    item.get("interpretation"), errors, f"{path}.interpretation"
                )
                suggested_action = as_str(
                    item.get("suggested_action"), errors, f"{path}.suggested_action"
                )
                if rule_key is not None and interpretation is not None and suggested_action:
                    items.append(RiskItem(rule_key, interpretation, suggested_action))

        summary = as_str(payload.get("summary"), errors, "summary")
        data_refs = as_str_list(payload.get("data_refs"), errors, "data_refs")
        if data_refs is not None and not data_refs:
            errors.append("data_refs 不得为空（解读必须基于给出的风控事件）")

        if errors:
            raise ContractError(errors)
        return RiskInterpretOutput(
            overall_risk=nn(overall_risk),
            items=tuple(items),
            summary=nn(summary),
            data_refs=nn(data_refs),
        )


__all__ = [
    "RISK_LEVELS",
    "THESIS_ACTIONS",
    "THESIS_STATUSES",
    "AttributionReviewContract",
    "AttributionReviewOutput",
    "RiskInterpretContract",
    "RiskInterpretOutput",
    "RiskItem",
    "ThesisReviewContract",
    "ThesisReviewOutput",
]
