"""LLM 通道（P4.1）：消息/响应/Provider Protocol + 实现。

- base：Message / LLMResponse / LLMProvider / LLMError
- pricing：模型单价 → cost_cny（Decimal 精确到分）
- openai_compat：真实 provider（标准库 urllib，OpenAI 兼容协议）
- fake：FakeProvider（测试零网络）
- cassette：CassetteProvider（录制/重放，测试零网络）
- runs：agent_run 审计落库（config_hash + code_version）
"""

from __future__ import annotations

from viqtrader.agents.llm.base import LLMError, LLMProvider, LLMResponse, Message
from viqtrader.agents.llm.cassette import CassetteMissError, CassetteProvider, fingerprint
from viqtrader.agents.llm.fake import FakeProvider
from viqtrader.agents.llm.openai_compat import OpenAICompatProvider, api_key_from_env
from viqtrader.agents.llm.pricing import ModelPrice, Pricing
from viqtrader.agents.llm.runs import (
    AgentRunRecord,
    build_run_record,
    new_run_id,
    write_agent_run,
)

__all__ = [
    "AgentRunRecord",
    "CassetteMissError",
    "CassetteProvider",
    "FakeProvider",
    "LLMError",
    "LLMProvider",
    "LLMResponse",
    "Message",
    "ModelPrice",
    "OpenAICompatProvider",
    "Pricing",
    "api_key_from_env",
    "build_run_record",
    "fingerprint",
    "new_run_id",
    "write_agent_run",
]
