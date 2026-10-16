"""研报解析 Agent 输出契约（P4.8，设计方案 4.6 研报库 + P1.8 入库管线的消费侧）。

把券商研报全文抽取为结构化 JSON：摘要 / 核心假设 / 盈利预测（→ analyst_forecast）/
评级目标价 / 风险点。所有产出均为**外部观点**，平台内与自身数据严格区分。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from viqtrader.agents.contracts.base import (
    BaseContract,
    ContractError,
    as_list,
    as_number,
    as_str,
    as_str_list,
    nn,
)


def _optional_number(value: Any, errors: list[str], path: str) -> float | None:
    """可空数值：None → None；其余按 number 校验。"""
    if value is None:
        return None
    return as_number(value, errors, path)


def _optional_str(value: Any, errors: list[str], path: str) -> str | None:
    """可空字符串：None → None；其余按非空字符串校验。"""
    if value is None:
        return None
    return as_str(value, errors, path)


@dataclass(frozen=True, slots=True)
class ForecastItem:
    """单财年盈利预测（数值字段可空，但 fiscal_year 必填）。"""

    fiscal_year: str
    net_profit: float | None
    eps: float | None
    target_price: float | None


@dataclass(frozen=True, slots=True)
class DocAnalysisOutput:
    """研报结构化解析结果。"""

    summary: str
    core_assumptions: tuple[str, ...]
    forecasts: tuple[ForecastItem, ...]
    rating: str | None
    target_price: float | None
    risks: tuple[str, ...]


class DocAnalysisContract(BaseContract[DocAnalysisOutput]):
    """校验研报解析 Agent 的 JSON 输出。"""

    name: str = "doc_analysis"
    schema_hint: str = (
        '{"summary": str, "core_assumptions": [str], '
        '"forecasts": [{"fiscal_year": str, "net_profit": float|null, '
        '"eps": float|null, "target_price": float|null}], '
        '"rating": str|null, "target_price": float|null, "risks": [str]}'
    )

    def parse(self, payload: dict[str, Any]) -> DocAnalysisOutput:
        errors: list[str] = []
        summary = as_str(payload.get("summary"), errors, "summary")
        core_assumptions = as_str_list(payload.get("core_assumptions"), errors, "core_assumptions")

        forecasts: list[ForecastItem] = []
        raw_forecasts = as_list(payload.get("forecasts"), errors, "forecasts")
        if raw_forecasts is not None:
            for index, item in enumerate(raw_forecasts):
                path = f"forecasts[{index}]"
                if not isinstance(item, dict):
                    errors.append(f"{path} 必须是对象")
                    continue
                fiscal_year = as_str(item.get("fiscal_year"), errors, f"{path}.fiscal_year")
                net_profit = _optional_number(item.get("net_profit"), errors, f"{path}.net_profit")
                eps = _optional_number(item.get("eps"), errors, f"{path}.eps")
                target = _optional_number(item.get("target_price"), errors, f"{path}.target_price")
                if fiscal_year is not None:
                    forecasts.append(ForecastItem(fiscal_year, net_profit, eps, target))

        rating = _optional_str(payload.get("rating"), errors, "rating")
        target_price = _optional_number(payload.get("target_price"), errors, "target_price")
        risks = as_str_list(payload.get("risks"), errors, "risks")

        if errors:
            raise ContractError(errors)
        return DocAnalysisOutput(
            summary=nn(summary),
            core_assumptions=nn(core_assumptions),
            forecasts=tuple(forecasts),
            rating=rating,
            target_price=target_price,
            risks=nn(risks),
        )


__all__ = ["DocAnalysisContract", "DocAnalysisOutput", "ForecastItem"]
