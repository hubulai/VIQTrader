"""P4.3 Prompt 版本化测试：manifest 映射、版本回溯、占位渲染不与 JSON 花括号冲突。"""

from __future__ import annotations

import pytest

from viqtrader.agents.prompts.registry import PromptError, PromptRegistry


def test_registry_lists_and_reports_version() -> None:
    registry = PromptRegistry()
    assert registry.names() == ["dissent", "valuation"]
    assert registry.version("valuation") == "valuation.v2"
    assert registry.version("dissent") == "dissent.v2"


def test_render_substitutes_without_json_brace_conflict() -> None:
    registry = PromptRegistry()
    rendered = registry.render(
        "valuation",
        code="600519",
        market="SH",
        name="贵州茅台",
        financials="{...财务...}",
        valuation="{...估值...}",
        dividends="{...分红...}",
        disclosures="{...公告...}",
        peers="{...同业...}",
        consensus="{...券预期...}",
        learning_context="## 学习上下文\n（暂无历史案例与评分卡记录）",
        schema_hint='{"code": "600519"}',
    )
    assert "600519" in rendered
    assert '{"code": "600519"}' in rendered  # JSON 花括号原样保留
    assert "$code" not in rendered


def test_unknown_prompt_raises() -> None:
    registry = PromptRegistry()
    with pytest.raises(PromptError):
        registry.text("nonexistent")
    with pytest.raises(PromptError):
        registry.meta("nonexistent")
