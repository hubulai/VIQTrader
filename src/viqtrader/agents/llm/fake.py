"""FakeProvider：测试 / 离线用假 provider，零网络、零成本。

返回预设或脚本化内容，并记录每次调用（便于断言调用次数、capture 入参）。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import Any

from viqtrader.agents.llm.base import LLMResponse, Message
from viqtrader.agents.llm.pricing import Pricing

Responder = Callable[[Sequence[Message], str, dict[str, Any]], str]


class FakeProvider:
    """按 responder 产出内容；未给 responder 时恒返回固定 content。"""

    def __init__(
        self,
        responder: Responder | None = None,
        *,
        content: str = "",
        model: str = "fake-model",
        token_in: int = 0,
        token_out: int = 0,
        latency_ms: int = 0,
        pricing: Pricing | None = None,
    ) -> None:
        self._responder: Responder = responder or _fixed(content)
        self._model = model
        self._token_in = token_in
        self._token_out = token_out
        self._latency_ms = latency_ms
        self._pricing = pricing
        self.calls: list[tuple[tuple[Message, ...], str, dict[str, Any]]] = []

    def complete(self, messages: Sequence[Message], model: str, **params: Any) -> LLMResponse:
        snapshot = tuple(messages)
        self.calls.append((snapshot, model, dict(params)))
        cost: Decimal | None = None
        if self._pricing is not None:
            cost = self._pricing.cost(model, self._token_in, self._token_out)
        return LLMResponse(
            content=self._responder(snapshot, model, dict(params)),
            model=model or self._model,
            token_in=self._token_in,
            token_out=self._token_out,
            cost_cny=cost,
            latency_ms=self._latency_ms,
            status="SUCCESS",
        )


def _fixed(content: str) -> Responder:
    def _respond(messages: Sequence[Message], model: str, params: dict[str, Any]) -> str:
        return content

    return _respond


__all__ = ["FakeProvider", "Responder"]
