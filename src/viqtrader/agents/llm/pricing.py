"""模型计价：从 config 读每模型 in/out 单价，算 cost_cny（Decimal）。

未在 `llm.pricing` 中配置的模型返回 None——成本留空而不猜，避免审计失真。

精度说明：单次调用成本常为「分的百分之一以下」（如 3K token ≈ 0.003 元），
按分舍入会一律归零，失去成本核算意义；故保留 6 位小数（元），全程 Decimal 无浮点误差。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from viqtrader.config.loader import Config

_PRECISION = Decimal("0.000001")
_THOUSAND = Decimal(1000)


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """单模型单价：CNY / 1K token。"""

    input_per_1k: Decimal
    output_per_1k: Decimal


class Pricing:
    """模型单价表 + 成本计算。"""

    def __init__(self, prices: Mapping[str, ModelPrice]) -> None:
        self._prices = dict(prices)

    @classmethod
    def from_config(cls, config: Config) -> Pricing:
        raw = config.get("llm.pricing", {}) or {}
        prices: dict[str, ModelPrice] = {}
        if isinstance(raw, dict):
            for model, spec in raw.items():
                if not isinstance(spec, dict):
                    continue
                prices[str(model)] = ModelPrice(
                    input_per_1k=Decimal(str(spec.get("input_per_1k", 0))),
                    output_per_1k=Decimal(str(spec.get("output_per_1k", 0))),
                )
        return cls(prices)

    def cost(self, model: str, token_in: int, token_out: int) -> Decimal | None:
        price = self._prices.get(model)
        if price is None:
            return None
        amount = (
            Decimal(int(token_in)) / _THOUSAND * price.input_per_1k
            + Decimal(int(token_out)) / _THOUSAND * price.output_per_1k
        )
        return amount.quantize(_PRECISION, rounding=ROUND_HALF_UP)


__all__ = ["ModelPrice", "Pricing"]
