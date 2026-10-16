"""Agent 输出契约通用机制（P4.2）。

- 每个 Agent 一份 dataclass 契约：`parse` 强类型解析 + 校验，失败抛 ContractError。
- 通用运行器 `run_contract`：不合格 → 带错误提示重试一次 → 仍不合格标 DEGRADED
  （由上层决定不写 agent_output）。
- LLM 输出常裹 markdown 代码块与前后废话，`parse_json_response` 负责抽取首个 JSON 对象。
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast, runtime_checkable

from viqtrader.agents.llm.base import LLMProvider, LLMResponse, Message
from viqtrader.agents.llm.runs import STATUS_DEGRADED, STATUS_FAILED, STATUS_SUCCESS

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class ContractError(Exception):
    """契约校验失败，携带全部错误信息（供重试提示）。"""

    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = tuple(errors)
        super().__init__("; ".join(self.errors))


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """校验结果：ok / errors / 规范化原始 JSON（落 agent_output）/ 强类型实例。"""

    ok: bool
    errors: tuple[str, ...] = ()
    payload: dict[str, Any] | None = None
    parsed: Any | None = None


@runtime_checkable
class AgentContract(Protocol):
    """Agent 输出契约：名字 + 供 prompt 用的 schema 提示 + 校验入口。"""

    name: str
    schema_hint: str

    def validate(self, payload: Any) -> ValidationResult: ...


class BaseContract[T]:
    """dataclass 契约骨架：子类只实现 `parse`（强类型解析），校验入口统一在此。"""

    name: str = ""
    schema_hint: str = ""

    def parse(self, payload: dict[str, Any]) -> T:
        raise NotImplementedError

    def validate(self, payload: Any) -> ValidationResult:
        if not isinstance(payload, dict):
            return ValidationResult(False, ("响应顶层必须是 JSON 对象",))
        try:
            parsed = self.parse(payload)
        except ContractError as exc:
            return ValidationResult(False, tuple(exc.errors))
        return ValidationResult(True, (), payload, parsed)


def parse_json_response(text: str) -> dict[str, Any]:
    """从 LLM 文本抽取 JSON 对象（容忍代码块与前后缀）。"""
    candidate = text.strip()
    fenced = _FENCE_RE.search(candidate)
    if fenced:
        candidate = fenced.group(1).strip()
    try:
        data = json.loads(candidate)
    except ValueError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start == -1 or end <= start:
            raise ContractError(["响应中未找到 JSON 对象"]) from None
        try:
            data = json.loads(candidate[start : end + 1])
        except ValueError as exc:
            raise ContractError([f"JSON 解析失败: {exc}"]) from exc
    if not isinstance(data, dict):
        raise ContractError(["响应顶层必须是 JSON 对象"])
    return cast("dict[str, Any]", data)


@dataclass(frozen=True, slots=True)
class ContractRun:
    """一次契约化调用的结果。"""

    status: str
    attempts: int
    response: LLMResponse | None
    validation: ValidationResult


_RETRY_TEMPLATE = (
    "你上一条输出不满足契约要求，问题如下：\n{errors}\n请仅返回满足契约的 JSON，不要额外解释。"
)


def run_contract(
    provider: LLMProvider,
    contract: AgentContract,
    messages: Sequence[Message],
    model: str,
    *,
    params: dict[str, Any] | None = None,
    max_retries: int = 1,
) -> ContractRun:
    """调用 LLM 并对输出做契约校验；不合格带错误提示重试（默认 1 次）。"""
    convo = list(messages)
    kwargs: dict[str, Any] = dict(params) if params else {}
    attempts = 0
    last: ValidationResult | None = None
    last_response: LLMResponse | None = None

    while True:
        attempts += 1
        # LLM 边界：调用失败统一收敛为 FAILED，不向上抛
        try:
            response = provider.complete(convo, model, **kwargs)
        except Exception as exc:
            return ContractRun(
                STATUS_FAILED, attempts, None, ValidationResult(False, (f"LLM 调用失败: {exc}",))
            )
        last_response = response
        try:
            result = contract.validate(parse_json_response(response.content))
        except ContractError as exc:
            result = ValidationResult(False, tuple(exc.errors))
        last = result
        if result.ok:
            return ContractRun(STATUS_SUCCESS, attempts, response, result)
        if attempts > max_retries:
            break
        convo = [
            *convo,
            Message("assistant", response.content),
            Message(
                "user",
                _RETRY_TEMPLATE.format(errors="\n".join(f"- {e}" for e in result.errors)),
            ),
        ]

    if last is None or last_response is None:
        return ContractRun(
            STATUS_FAILED, attempts, last_response, ValidationResult(False, ("未获得有效响应",))
        )
    return ContractRun(STATUS_DEGRADED, attempts, last_response, last)


def as_str(value: Any, errors: list[str], path: str, *, allow_empty: bool = False) -> str | None:
    if not isinstance(value, str):
        errors.append(f"{path} 必须是字符串")
        return None
    if not value and not allow_empty:
        errors.append(f"{path} 不得为空")
        return None
    return value


def as_number(value: Any, errors: list[str], path: str) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        errors.append(f"{path} 必须是数字")
        return None
    return float(value)


def as_list(value: Any, errors: list[str], path: str) -> list[Any] | None:
    if not isinstance(value, list):
        errors.append(f"{path} 必须是数组")
        return None
    return value


def as_str_list(value: Any, errors: list[str], path: str) -> tuple[str, ...] | None:
    items = as_list(value, errors, path)
    if items is None:
        return None
    out: list[str] = []
    for index, item in enumerate(items):
        item_str = as_str(item, errors, f"{path}[{index}]")
        if item_str is not None:
            out.append(item_str)
    return tuple(out)


def nn[T](value: T | None) -> T:
    """校验通过后取非空值；触发即说明校验逻辑有漏（测试兜底）。"""
    if value is None:
        raise ContractError(["内部校验错误：必填字段为空"])
    return value


__all__ = [
    "AgentContract",
    "BaseContract",
    "ContractError",
    "ContractRun",
    "ValidationResult",
    "as_list",
    "as_number",
    "as_str",
    "as_str_list",
    "nn",
    "parse_json_response",
    "run_contract",
]
