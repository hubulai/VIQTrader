"""LLM 盒带（cassette）：录制真实响应 → 离线重放，零网络、零成本、可复现。

- fingerprint = sha256(规范化 {model, messages, 关键参数})，**不含 api_key / 请求头**。
- 录制模式落盘；重放模式按 fingerprint 命中。未命中 fail-fast 报错——
  杜绝测试偷偷发真实请求。
- 文件布局：<cassette_dir>/<agent>/<case>.json，同一 case 多次调用按 fingerprint 归入 entries。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal, cast

from viqtrader.agents.llm.base import LLMError, LLMProvider, LLMResponse, Message

Mode = Literal["record", "replay", "auto"]


class CassetteMissError(LLMError):
    """重放模式未命中指纹：用例与录制不一致，必须暴露而非静默重发。"""


def fingerprint(messages: Sequence[Message], model: str, params: dict[str, Any]) -> str:
    """请求指纹：仅覆盖 model / messages / 显式参数，排除 None 与凭据。"""
    payload = {
        "model": model,
        "messages": [m.to_dict() for m in messages],
        "params": {k: params[k] for k in sorted(params) if params[k] is not None},
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class CassetteProvider:
    """包装真实 provider：录制落盘 / 重放查表。"""

    def __init__(
        self,
        inner: LLMProvider,
        *,
        cassette_dir: Path,
        agent: str,
        case: str,
        mode: Mode = "auto",
    ) -> None:
        self._inner = inner
        self._agent = agent
        self._case = case
        self._mode = mode
        self._path = Path(cassette_dir) / agent / f"{case}.json"
        self._doc = self._load()

    @property
    def path(self) -> Path:
        return self._path

    def complete(self, messages: Sequence[Message], model: str, **params: Any) -> LLMResponse:
        fp = fingerprint(messages, model, params)
        entries: dict[str, Any] = self._doc["entries"]
        if fp in entries:
            recorded = entries[fp]
            return LLMResponse.from_dict(cast("dict[str, Any]", recorded["response"]))
        if self._mode == "replay":
            raise CassetteMissError(
                f"盒带未命中：agent={self._agent} case={self._case} fp={fp[:12]}；请先录制"
            )

        response = self._inner.complete(messages, model, **params)
        entries[fp] = {
            "request": {
                "model": model,
                "messages": [m.to_dict() for m in messages],
                "params": {k: params[k] for k in sorted(params) if params[k] is not None},
            },
            "response": response.to_dict(),
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._doc, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return response

    def _load(self) -> dict[str, Any]:
        if self._path.exists():
            return cast("dict[str, Any]", json.loads(self._path.read_text(encoding="utf-8")))
        if self._mode == "replay":
            raise CassetteMissError(f"盒带文件缺失，无法重放: {self._path}")
        return {"agent": self._agent, "case": self._case, "entries": {}}


__all__ = ["CassetteMissError", "CassetteProvider", "Mode", "fingerprint"]
