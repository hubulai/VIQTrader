"""元复盘反方审查契约（P7）：专审统计有效性 / 过拟合 / 递归污染。

与估值反方（dissent.v2，审业务假设）职责不同：本契约只审提案的**方法论**——
- 统计有效性：平台年成交几十笔，样本量是否足以支持该提案结论；
- 过拟合：是否对噪声调参（个别失败案例推出普适规则）；
- 递归污染：evidence_refs 是否引用了其他 Agent 的观点而非原始事实。
不复核业务方向对错（终裁权在人）。
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


@dataclass(frozen=True, slots=True)
class ProposalVerdict:
    """对单条提案的审查结论（proposal_index 对应输入提案顺序，从 0 起）。"""

    proposal_index: int
    approved: bool
    verdict: str
    concerns: tuple[str, ...]
    questions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MetaReviewDissentOutput:
    """反方审查输出：总体意见 + 逐提案判定。"""

    overall: str
    verdicts: tuple[ProposalVerdict, ...]
    data_refs: tuple[str, ...]


class MetaReviewDissentContract(BaseContract[MetaReviewDissentOutput]):
    """校验元复盘反方 Agent 的 JSON 输出。"""

    name: str = "meta_review_dissent"
    schema_hint: str = (
        '{"overall": str, "verdicts": [{"proposal_index": int（从 0 起）, '
        '"approved": bool, "verdict": str, "concerns": [str], "questions": [str]}], '
        '"data_refs": [str, …非空…]} '
        "每条输入提案必须恰有一条对应 verdict；approved=false 时 concerns 不得为空。"
    )

    def parse(self, payload: dict[str, Any]) -> MetaReviewDissentOutput:
        errors: list[str] = []
        overall = as_str(payload.get("overall"), errors, "overall")

        verdicts: list[ProposalVerdict] = []
        raw_verdicts = as_list(payload.get("verdicts"), errors, "verdicts")
        if raw_verdicts is not None:
            for index, item in enumerate(raw_verdicts):
                path = f"verdicts[{index}]"
                if not isinstance(item, dict):
                    errors.append(f"{path} 必须是对象")
                    continue
                verdict = self._parse_verdict(item, errors, path)
                if verdict is not None:
                    verdicts.append(verdict)

        data_refs = as_str_list(payload.get("data_refs"), errors, "data_refs")
        if data_refs is not None and not data_refs:
            errors.append("data_refs 不得为空（无据推断红线）")

        if errors:
            raise ContractError(errors)
        return MetaReviewDissentOutput(
            overall=nn(overall),
            verdicts=tuple(verdicts),
            data_refs=nn(data_refs),
        )

    def _parse_verdict(
        self, item: dict[str, Any], errors: list[str], path: str
    ) -> ProposalVerdict | None:
        item_errors = len(errors)
        raw_index = item.get("proposal_index")
        if isinstance(raw_index, bool) or not isinstance(raw_index, int):
            errors.append(f"{path}.proposal_index 必须是整数")
            proposal_index = None
        elif raw_index < 0:
            errors.append(f"{path}.proposal_index 不得为负")
            proposal_index = None
        else:
            proposal_index = raw_index

        raw_approved = item.get("approved")
        if not isinstance(raw_approved, bool):
            errors.append(f"{path}.approved 必须是布尔值")
            approved = None
        else:
            approved = raw_approved

        verdict = as_str(item.get("verdict"), errors, f"{path}.verdict")
        concerns = as_str_list(item.get("concerns"), errors, f"{path}.concerns")
        questions = as_str_list(item.get("questions"), errors, f"{path}.questions")
        if approved is False and concerns is not None and not concerns:
            errors.append(f"{path}.concerns 不得为空（approved=false 必须说明顾虑）")

        if len(errors) > item_errors:
            return None
        return ProposalVerdict(
            proposal_index=nn(proposal_index),
            approved=nn(approved),
            verdict=nn(verdict),
            concerns=nn(concerns),
            questions=nn(questions),
        )


__all__ = ["MetaReviewDissentContract", "MetaReviewDissentOutput", "ProposalVerdict"]
