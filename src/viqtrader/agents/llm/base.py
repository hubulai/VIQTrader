"""LLM 通道抽象：消息、响应、Provider Protocol（P4.1）。

- 所有 Agent 经统一 Protocol 调用 LLM；具体 provider（OpenAI 兼容 / 假 / 盒带）
  由编排层注入，测试用 FakeProvider / CassetteProvider 保证零网络、零成本。
- 金额一律 Decimal（精确到分），不上浮点误差。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal, Protocol, runtime_checkable

MessageRole = Literal["system", "user", "assistant"]
RunStatus = Literal["SUCCESS", "FAILED", "DEGRADED"]


class LLMError(Exception):
    """LLM 调用失败（网络 / 协议 / 解析）。"""


@dataclass(frozen=True, slots=True)
class Message:
    """一条对话消息。role 取 system / user / assistant。"""

    role: str
    content: str

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """provider 返回的一次补全结果。"""

    content: str
    model: str
    token_in: int = 0
    token_out: int = 0
    cost_cny: Decimal | None = None
    latency_ms: int = 0
    status: str = "SUCCESS"

    def to_dict(self) -> dict[str, Any]:
        """可 JSON 序列化（盒带落盘用）；cost 存字符串避免浮点失真。"""
        return {
            "content": self.content,
            "model": self.model,
            "token_in": self.token_in,
            "token_out": self.token_out,
            "cost_cny": None if self.cost_cny is None else str(self.cost_cny),
            "latency_ms": self.latency_ms,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LLMResponse:
        cost = data.get("cost_cny")
        return cls(
            content=str(data["content"]),
            model=str(data["model"]),
            token_in=int(data.get("token_in", 0)),
            token_out=int(data.get("token_out", 0)),
            cost_cny=None if cost is None else Decimal(str(cost)),
            latency_ms=int(data.get("latency_ms", 0)),
            status=str(data.get("status", "SUCCESS")),
        )


@runtime_checkable
class LLMProvider(Protocol):
    """LLM 补全通道。实现须保证：失败抛 LLMError，不返回半成品响应。"""

    def complete(self, messages: Sequence[Message], model: str, **params: Any) -> LLMResponse: ...
