"""OpenAI 兼容协议 provider（DeepSeek / Qwen / GLM / Kimi 等通用，P4.1）。

- 仅用标准库 urllib，零新增依赖。
- 认证头只在 HTTP 请求里出现，绝不进入 request 快照 / 盒带（防泄密）。
- HTTP 传输可注入（http_post），测试用假传输即可离线验证请求构造与响应解析。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from typing import Any, cast

from viqtrader.agents.llm.base import LLMError, LLMResponse, Message
from viqtrader.agents.llm.pricing import Pricing
from viqtrader.config.loader import Config

HttpPost = Callable[[str, bytes, dict[str, str], float], bytes]


def api_key_from_env(config: Config) -> str:
    """从 `llm.api_key_env` 指定的环境变量取密钥（.env 由 Config.load 载入）。"""
    env_name = str(config.get("llm.api_key_env", "LLM_API_KEY"))
    key = os.environ.get(env_name, "")
    if not key:
        raise LLMError(f"缺少 LLM API key：请设置环境变量 {env_name}（放 .env，不入库）")
    return key


def _default_http_post(url: str, body: bytes, headers: dict[str, str], timeout: float) -> bytes:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return bytes(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise LLMError(f"LLM HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise LLMError(f"LLM 网络错误: {exc.reason}") from exc


class OpenAICompatProvider:
    """POST {base_url}/chat/completions（OpenAI 兼容协议）。"""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        pricing: Pricing | None = None,
        timeout: float = 120.0,
        http_post: HttpPost | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._pricing = pricing
        self._timeout = timeout
        self._http_post = http_post or _default_http_post

    @classmethod
    def from_config(
        cls, config: Config, *, http_post: HttpPost | None = None
    ) -> OpenAICompatProvider:
        """按 config 组装；密钥经 api_key_from_env 从环境变量读取。"""
        return cls(
            base_url=str(config.get("llm.base_url", "")),
            api_key=api_key_from_env(config),
            pricing=Pricing.from_config(config),
            timeout=float(config.get("llm.timeout_seconds", 120)),
            http_post=http_post,
        )

    def complete(self, messages: Sequence[Message], model: str, **params: Any) -> LLMResponse:
        body: dict[str, Any] = {
            "model": model,
            "messages": [m.to_dict() for m in messages],
            **params,
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }
        started = time.monotonic()
        raw = self._http_post(
            f"{self._base_url}/chat/completions",
            json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers,
            self._timeout,
        )
        latency_ms = int((time.monotonic() - started) * 1000)
        return self._parse(raw, model, latency_ms)

    def _parse(self, raw: bytes, model: str, latency_ms: int) -> LLMResponse:
        try:
            payload = cast("dict[str, Any]", json.loads(raw.decode("utf-8")))
        except (ValueError, UnicodeDecodeError) as exc:
            raise LLMError(f"LLM 响应非法 JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise LLMError("LLM 响应顶层必须是对象")

        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LLMError("LLM 响应缺少 choices")
        first = choices[0]
        message = first.get("message") if isinstance(first, dict) else None
        if not isinstance(message, dict):
            raise LLMError("LLM 响应缺少 choices[0].message")
        content = message.get("content")
        if not isinstance(content, str):
            raise LLMError("LLM 响应缺少 message.content")

        usage = payload.get("usage")
        token_in = 0
        token_out = 0
        if isinstance(usage, dict):
            token_in = int(usage.get("prompt_tokens") or 0)
            token_out = int(usage.get("completion_tokens") or 0)

        cost = self._pricing.cost(model, token_in, token_out) if self._pricing else None
        return LLMResponse(
            content=content,
            model=str(payload.get("model") or model),
            token_in=token_in,
            token_out=token_out,
            cost_cny=cost,
            latency_ms=latency_ms,
            status="SUCCESS",
        )


__all__ = ["HttpPost", "OpenAICompatProvider", "api_key_from_env"]
