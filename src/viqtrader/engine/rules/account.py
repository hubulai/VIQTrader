"""P2 Phase 2 账户规则（设计文档 3.3）：有没有资源。

CashSufficiencyRule / PositionLimitRule / ModeIsolationRule / QuotaRule（软限制）
"""

from __future__ import annotations

from decimal import Decimal

from viqtrader.engine.fees import estimate_fees
from viqtrader.engine.types import (
    OrderIntent,
    OrderSide,
    OrderType,
    RuleCheckResult,
    RuleContext,
    RulePhase,
)


def _ok(rule_id: str, message: str = "") -> RuleCheckResult:
    return RuleCheckResult(passed=True, rule_id=rule_id, message=message)


def _deny(rule_id: str, message: str) -> RuleCheckResult:
    return RuleCheckResult(passed=False, rule_id=rule_id, message=message)


# ---------------------------------------------------------------------------
# CashSufficiencyRule（可用资金充足，含费用预估）
# ---------------------------------------------------------------------------


class CashSufficiencyRule:
    rule_id = "CASH_SUFFICIENCY"
    phase = RulePhase.ACCOUNT

    def check(self, intent: OrderIntent, *, ctx: RuleContext) -> RuleCheckResult:
        if intent.side != OrderSide.BUY:
            return _ok(self.rule_id)
        price = self._reference_price(intent, ctx)
        if price is None:
            return _deny(self.rule_id, "无法确定参考价（无限价且无当前价）")
        fees = estimate_fees(ctx.config, intent.market, intent.side, intent.qty, price, ctx.fx_rate)
        # 原币成交额 x 汇率 = CNY 口径
        needed = price * intent.qty * ctx.fx_rate + fees.total
        if needed > ctx.track_cash:
            return _deny(
                self.rule_id,
                f"所需资金 {needed} 超过本轨可用现金 {ctx.track_cash}",
            )
        return _ok(self.rule_id)

    @staticmethod
    def _reference_price(intent: OrderIntent, ctx: RuleContext) -> Decimal | None:
        if intent.order_type == OrderType.LIMIT and intent.price is not None:
            return intent.price
        return ctx.current_price  # MARKET 单用当前价估算


# ---------------------------------------------------------------------------
# PositionLimitRule（卖出量 ≤ 可用持仓）
# ---------------------------------------------------------------------------


class PositionLimitRule:
    rule_id = "POSITION_LIMIT"
    phase = RulePhase.ACCOUNT

    def check(self, intent: OrderIntent, *, ctx: RuleContext) -> RuleCheckResult:
        if intent.side != OrderSide.SELL:
            return _ok(self.rule_id)
        pos = ctx.position
        if pos is None:
            return _deny(self.rule_id, f"{intent.code} 无持仓，不可卖出")
        if intent.qty > pos.available_qty:
            return _deny(
                self.rule_id,
                f"可用持仓 {pos.available_qty}（总 {pos.qty} - 冻结 {pos.frozen_qty}）"
                f"< 意图卖出 {intent.qty}",
            )
        return _ok(self.rule_id)


# ---------------------------------------------------------------------------
# ModeIsolationRule（mode 与 account 匹配）
# ---------------------------------------------------------------------------


class ModeIsolationRule:
    rule_id = "MODE_ISOLATION"
    phase = RulePhase.ACCOUNT

    def check(self, intent: OrderIntent, *, ctx: RuleContext) -> RuleCheckResult:
        if intent.mode != ctx.account_mode:
            return _deny(
                self.rule_id,
                f"意图 mode={intent.mode} 与账本 mode={ctx.account_mode} 不一致（双轨隔离红线）",
            )
        return _ok(self.rule_id)


# ---------------------------------------------------------------------------
# QuotaRule（双轨配额——已确认软限制：超限只告警不阻断）
# ---------------------------------------------------------------------------


class QuotaRule:
    """买入不改变单轨总价值（现金等额转持仓），直接以当前占比对照目标区间。

    超限 → passed=True + severity=WARN（写入 risk_event 走告警路由，拍板权在用户）。
    """

    rule_id = "QUOTA"
    phase = RulePhase.ACCOUNT

    def check(self, intent: OrderIntent, *, ctx: RuleContext) -> RuleCheckResult:
        if intent.side != OrderSide.BUY:
            return _ok(self.rule_id)
        total = ctx.total_portfolio_value
        if total <= 0:
            return _ok(self.rule_id, "总资产为 0，跳过配额检查")
        track_value = ctx.track_cash + ctx.track_market_value
        ratio = track_value / total
        tolerance = Decimal(str((ctx.config.get("trading", {}) or {}).get("drift_tolerance", 0.05)))
        ceiling = ctx.target_ratio + tolerance
        if ratio > ceiling:
            return RuleCheckResult(
                passed=True,
                rule_id=self.rule_id,
                message=(
                    f"本轨占比 {ratio:.4f} 超过目标 {ctx.target_ratio} + 容忍 {tolerance}"
                    f"（软限制，仅告警）"
                ),
                severity="WARN",
            )
        return _ok(self.rule_id)
