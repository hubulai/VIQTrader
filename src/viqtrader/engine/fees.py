"""P2 费用模型（设计文档 3.3 / 7.5，参数来自 config fees 节）。

- A 股：佣金双边（最低 5 元）、印花税卖出单边、过户费双边
- 港股通：佣金双边（最低 HKD）、印花税双边、交易费双边；原币计算后按汇率折 CNY
- 输出统一 CNY 口径（与 cash_ledger / trade_log 一致），费用四舍五入到分
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from viqtrader.engine.types import FeeBreakdown, OrderSide, to_decimal

_D0 = Decimal("0")
_CENT = Decimal("0.01")


def estimate_fees(
    config: dict[str, Any],
    market: str,
    side: OrderSide,
    qty: int,
    price: Decimal,
    fx_rate: Decimal,
) -> FeeBreakdown:
    """估算成交费用（CNY）。price 为原币价；fx_rate 为原币→CNY（A 股传 1）。

    Args:
        config: 全量配置 dict（取 fees 节）。
        market: SH / SZ / BJ / HK。
        side: BUY / SELL。
        qty: 数量（股）。
        price: 原币价格。
        fx_rate: 汇率（原币→CNY）。
    """
    fees_cfg = (config.get("fees", {}) or {}).get("hk_connect" if market == "HK" else "a_share", {})
    turnover = price * qty  # 原币成交额

    if market == "HK":
        commission = _max_min(
            turnover * to_decimal(fees_cfg.get("commission_rate"), _D0),
            to_decimal(fees_cfg.get("commission_min_hkd"), _D0),
        )
        stamp = turnover * to_decimal(fees_cfg.get("stamp_tax_bilateral"), _D0)
        trading_fee = turnover * to_decimal(fees_cfg.get("trading_fee"), _D0)
        # 港股费用原币 HKD 计算，统一折 CNY 输出
        return FeeBreakdown(
            commission=_round_fee(commission * fx_rate),
            stamp_tax=_round_fee(stamp * fx_rate),
            transfer_fee=_round_fee(trading_fee * fx_rate),
        )

    # A 股
    commission = _max_min(
        turnover * to_decimal(fees_cfg.get("commission_rate"), _D0),
        to_decimal(fees_cfg.get("commission_min_cny"), _D0),
    )
    stamp = _D0
    if side == OrderSide.SELL:
        stamp = turnover * to_decimal(fees_cfg.get("stamp_tax_sell"), _D0)
    transfer = turnover * to_decimal(fees_cfg.get("transfer_fee"), _D0)
    return FeeBreakdown(
        commission=_round_fee(commission),
        stamp_tax=_round_fee(stamp),
        transfer_fee=_round_fee(transfer),
    )


def _max_min(value: Decimal, minimum: Decimal) -> Decimal:
    return value if value > minimum else minimum


def _round_fee(value: Decimal) -> Decimal:
    """费用四舍五入到分。"""
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)
