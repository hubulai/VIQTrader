"""元复盘 Agent 输出契约（P7）：横切学习资产的唯一出口，只提议不执行。

红线（设计评审拍板，契约层强制可校验部分）：
- 每条提案必须携带非空 evidence_refs（无据不提）；
- kind 仅四类：param_change / thesis_retire / screen_rule / prompt_change；
- confidence 仅 high / medium / low——样本量陷阱（平台年成交几十笔）下
  样本不足必须降级，由 prompt 与反方审查兜底，契约保证枚举合法；
- param_change 的 payload 必含 config_path / current / proposed；
  prompt_change 只写 direction（方向），不代笔全文。
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

KINDS = ("param_change", "thesis_retire", "screen_rule", "prompt_change")
CONFIDENCES = ("high", "medium", "low")

# 各 kind 的 payload 必填键（current/proposed 可为任意 JSON 值，其余必须字符串）
_KIND_REQUIRED_KEYS: dict[str, tuple[str, ...]] = {
    "param_change": ("config_path", "current", "proposed"),
    "thesis_retire": ("target",),
    "screen_rule": ("target", "direction"),
    "prompt_change": ("target", "direction"),
}
_KIND_STR_KEYS: dict[str, tuple[str, ...]] = {
    "param_change": ("config_path",),
    "thesis_retire": ("target",),
    "screen_rule": ("target", "direction"),
    "prompt_change": ("target", "direction"),
}


@dataclass(frozen=True, slots=True)
class Proposal:
    """单条学习提案：四类之一 + 理由 + 证据 + 置信度。"""

    kind: str
    title: str
    payload: dict[str, Any]
    rationale: str
    evidence_refs: tuple[str, ...]
    confidence: str


@dataclass(frozen=True, slots=True)
class MetaReviewOutput:
    """元复盘输出：窗口总结 + 提案列表（可为空）+ 样本量声明。"""

    window_summary: str
    proposals: tuple[Proposal, ...]
    sample_caveat: str | None
    data_refs: tuple[str, ...]


class MetaReviewContract(BaseContract[MetaReviewOutput]):
    """校验元复盘 Agent 的 JSON 输出。"""

    name: str = "meta_review"
    schema_hint: str = (
        '{"window_summary": str, "sample_caveat": str | null, '
        '"proposals": [{"kind": "param_change|thesis_retire|screen_rule|prompt_change", '
        '"title": str, "payload": {…按 kind 见下…}, "rationale": str, '
        '"evidence_refs": [str, …非空…], "confidence": "high|medium|low"}], '
        '"data_refs": [str, …非空…]} '
        'payload 必填键：param_change={"config_path": str, "current": 任意, "proposed": 任意}；'
        'thesis_retire={"target": str（code 或 thesis_id）}；'
        'screen_rule={"target": str（规则标识）, "direction": str}；'
        'prompt_change={"target": str（agent 名）, "direction": str（只写调整方向，不代笔全文）}。'
        "无足够证据时 proposals 允许为空数组。"
    )

    def parse(self, payload: dict[str, Any]) -> MetaReviewOutput:
        errors: list[str] = []
        window_summary = as_str(payload.get("window_summary"), errors, "window_summary")

        sample_caveat = payload.get("sample_caveat")
        if sample_caveat is not None and not isinstance(sample_caveat, str):
            errors.append("sample_caveat 必须是字符串或 null")
            sample_caveat = None

        proposals: list[Proposal] = []
        raw_proposals = as_list(payload.get("proposals"), errors, "proposals")
        if raw_proposals is not None:
            for index, item in enumerate(raw_proposals):
                path = f"proposals[{index}]"
                if not isinstance(item, dict):
                    errors.append(f"{path} 必须是对象")
                    continue
                proposal = self._parse_proposal(item, errors, path)
                if proposal is not None:
                    proposals.append(proposal)

        data_refs = as_str_list(payload.get("data_refs"), errors, "data_refs")
        if data_refs is not None and not data_refs:
            errors.append("data_refs 不得为空（无据推断红线）")

        if errors:
            raise ContractError(errors)
        return MetaReviewOutput(
            window_summary=nn(window_summary),
            proposals=tuple(proposals),
            sample_caveat=sample_caveat,
            data_refs=nn(data_refs),
        )

    def _parse_proposal(
        self, item: dict[str, Any], errors: list[str], path: str
    ) -> Proposal | None:
        item_errors = len(errors)
        kind = as_str(item.get("kind"), errors, f"{path}.kind")
        if kind is not None and kind not in KINDS:
            errors.append(f"{path}.kind 非法: {kind}（可选 {list(KINDS)}）")
            kind = None
        title = as_str(item.get("title"), errors, f"{path}.title")
        rationale = as_str(item.get("rationale"), errors, f"{path}.rationale")
        confidence = as_str(item.get("confidence"), errors, f"{path}.confidence")
        if confidence is not None and confidence not in CONFIDENCES:
            errors.append(f"{path}.confidence 非法: {confidence}（可选 {list(CONFIDENCES)}）")
            confidence = None
        evidence_refs = as_str_list(item.get("evidence_refs"), errors, f"{path}.evidence_refs")
        if evidence_refs is not None and not evidence_refs:
            errors.append(f"{path}.evidence_refs 不得为空（每条提案必须带证据）")
            evidence_refs = None

        raw_payload = item.get("payload")
        payload_dict: dict[str, Any] | None = None
        if not isinstance(raw_payload, dict):
            errors.append(f"{path}.payload 必须是对象")
        else:
            payload_dict = raw_payload
            if kind is not None:
                for key in _KIND_REQUIRED_KEYS[kind]:
                    if key not in raw_payload:
                        errors.append(f"{path}.payload.{key} 缺失（kind={kind} 必填）")
                for key in _KIND_STR_KEYS[kind]:
                    if key in raw_payload and not isinstance(raw_payload[key], str):
                        errors.append(f"{path}.payload.{key} 必须是字符串")

        if len(errors) > item_errors:
            return None
        return Proposal(
            kind=nn(kind),
            title=nn(title),
            payload=nn(payload_dict),
            rationale=nn(rationale),
            evidence_refs=nn(evidence_refs),
            confidence=nn(confidence),
        )


__all__ = [
    "CONFIDENCES",
    "KINDS",
    "MetaReviewContract",
    "MetaReviewOutput",
    "Proposal",
]
