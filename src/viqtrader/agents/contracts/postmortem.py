"""案例尸检 Agent 输出契约（P5.3，设计方案第十一节案例库）。

LLM 只产出观点性字段（标题/失败模式/尸检结论/标签）；
code/industry/thesis_type 等事实字段由装配方从库中带入，不经 LLM，防编造。
成败案例都收：成功案例 failure_mode 为 null。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from viqtrader.agents.contracts.base import (
    BaseContract,
    ContractError,
    as_str,
    as_str_list,
    nn,
)


@dataclass(frozen=True, slots=True)
class PostmortemOutput:
    title: str  # 案例标题（一句话概括教训/经验）
    failure_mode: str | None  # 失败模式标签；成功案例为 None
    postmortem: str  # 尸检结论（因果复盘：什么判断对/错、证据是什么）
    tags: tuple[str, ...]  # 模式标签（供案例检索）
    data_refs: tuple[str, ...]


class PostmortemContract(BaseContract[PostmortemOutput]):
    """校验案例尸检 Agent 的 JSON 输出。"""

    name: str = "postmortem"
    schema_hint: str = (
        '{"title": str, "failure_mode": str|null, "postmortem": str, '
        '"tags": [str], "data_refs": [str]}'
    )

    def parse(self, payload: dict[str, Any]) -> PostmortemOutput:
        errors: list[str] = []
        title = as_str(payload.get("title"), errors, "title")

        raw_mode = payload.get("failure_mode")
        failure_mode: str | None = None
        if raw_mode is not None:
            failure_mode = as_str(raw_mode, errors, "failure_mode")

        postmortem = as_str(payload.get("postmortem"), errors, "postmortem")
        tags = as_str_list(payload.get("tags"), errors, "tags")
        data_refs = as_str_list(payload.get("data_refs"), errors, "data_refs")
        if data_refs is not None and not data_refs:
            errors.append("data_refs 不得为空（不允许无据推断）")

        if errors:
            raise ContractError(errors)
        return PostmortemOutput(
            title=nn(title),
            failure_mode=failure_mode,
            postmortem=nn(postmortem),
            tags=nn(tags),
            data_refs=nn(data_refs),
        )


__all__ = ["PostmortemContract", "PostmortemOutput"]
