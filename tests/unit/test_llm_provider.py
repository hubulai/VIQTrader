"""P4.1 LLM 通道测试：响应解析、Decimal 计价、密钥读取（全离线，零网络）。"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from viqtrader.agents.llm.base import LLMError, Message
from viqtrader.agents.llm.fake import FakeProvider
from viqtrader.agents.llm.openai_compat import OpenAICompatProvider, api_key_from_env
from viqtrader.agents.llm.pricing import ModelPrice, Pricing
from viqtrader.config.loader import Config

# 自建单价表：单测不依赖可编辑的 default.yaml（pricing 现为空占位）
_PRICING = Pricing({"deepseek-chat": ModelPrice(Decimal("0.001"), Decimal("0.002"))})


def _canned_body(content: str = "你好", prompt: int = 12000, completion: int = 3000) -> bytes:
    return json.dumps(
        {
            "model": "deepseek-chat",
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
        }
    ).encode("utf-8")


def test_fake_provider_returns_and_records_calls() -> None:
    provider = FakeProvider(content="answer")
    resp = provider.complete([Message("user", "hi")], "m", temperature=0.0)
    assert resp.content == "answer"
    assert resp.status == "SUCCESS"
    assert len(provider.calls) == 1
    assert provider.calls[0][2] == {"temperature": 0.0}


def test_openai_compat_builds_request_and_parses_response(config: Config) -> None:
    captured: dict[str, object] = {}

    def fake_post(url: str, body: bytes, headers: dict[str, str], timeout: float) -> bytes:
        captured["url"] = url
        captured["body"] = json.loads(body.decode("utf-8"))
        captured["headers"] = headers
        return _canned_body()

    provider = OpenAICompatProvider(
        base_url="https://api.deepseek.com/v1/",
        api_key="sk-SECRET",
        pricing=_PRICING,
        http_post=fake_post,
    )
    resp = provider.complete(
        [Message("system", "s"), Message("user", "u")], "deepseek-chat", temperature=0.2
    )

    assert captured["url"] == "https://api.deepseek.com/v1/chat/completions"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["model"] == "deepseek-chat"
    assert body["messages"] == [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
    ]
    assert body["temperature"] == 0.2
    headers = captured["headers"]
    assert isinstance(headers, dict)
    assert headers["Authorization"] == "Bearer sk-SECRET"

    assert resp.content == "你好"
    assert resp.token_in == 12000
    assert resp.token_out == 3000
    # 12*0.001 + 3*0.002 = 0.018 元
    assert resp.cost_cny == Decimal("0.018000")


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        json.dumps({"model": "m"}).encode("utf-8"),
        json.dumps({"choices": [{"message": {"role": "assistant"}}]}).encode("utf-8"),
    ],
)
def test_openai_compat_rejects_malformed_response(payload: bytes) -> None:
    provider = OpenAICompatProvider(
        base_url="https://x/v1",
        api_key="k",
        pricing=_PRICING,
        http_post=lambda url, body, headers, timeout: payload,
    )
    with pytest.raises(LLMError):
        provider.complete([Message("user", "u")], "deepseek-chat")


def test_pricing_exact_decimal_and_unknown_model_is_none() -> None:
    # 小 token 量仍保留 6 位精度，不被舍入归零
    assert _PRICING.cost("deepseek-chat", 100, 50) == Decimal("0.000200")
    assert _PRICING.cost("未配置模型", 100, 50) is None
    # 空单价表（当前 default.yaml 状态）：一切模型成本留空
    assert Pricing({}).cost("deepseek-chat", 100, 50) is None


def test_fake_provider_computes_cost_with_pricing() -> None:
    provider = FakeProvider(content="x", token_in=1000, token_out=1000, pricing=_PRICING)
    response = provider.complete([Message("user", "u")], "deepseek-chat")
    assert response.cost_cny == Decimal("0.003000")


def test_api_key_from_env(monkeypatch: pytest.MonkeyPatch, config: Config) -> None:
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    with pytest.raises(LLMError):
        api_key_from_env(config)
    monkeypatch.setenv("LLM_API_KEY", "sk-from-env")
    assert api_key_from_env(config) == "sk-from-env"
