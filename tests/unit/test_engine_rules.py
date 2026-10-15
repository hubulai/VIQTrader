"""P2 三阶段规则管线测试（Phase 1 市场 / Phase 2 账户）。"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

import pytest

from viqtrader.engine.rules.account import (
    CashSufficiencyRule,
    ModeIsolationRule,
    PositionLimitRule,
    QuotaRule,
)
from viqtrader.engine.rules.base import TradingRuleEngine
from viqtrader.engine.rules.market import (
    HKConnectRule,
    LotSizeRule,
    PriceLimitRule,
    SuspensionRule,
    T1Rule,
    TradingDayRule,
)
from viqtrader.engine.types import (
    MarketStatusView,
    OrderIntent,
    OrderSide,
    OrderType,
    PositionView,
    RuleCheckResult,
    RuleContext,
    RulePhase,
)


def make_intent(
    *,
    side: OrderSide = OrderSide.BUY,
    market: str = "SH",
    order_type: OrderType = OrderType.LIMIT,
    qty: int = 100,
    price: Decimal | None = Decimal("10.00"),
    mode: str = "value",
    code: str = "600519",
) -> OrderIntent:
    return OrderIntent(
        intent_id=str(uuid.uuid4()),
        account_id="acc_value",
        mode=mode,
        code=code,
        market=market,
        side=side,
        order_type=order_type,
        qty=qty,
        price=price,
        submitted_at=datetime(2026, 1, 6, 9, 30),
    )


def make_ms(
    *,
    code: str = "600519",
    board: str = "MAIN",
    suspended: bool = False,
    up: Decimal | None = Decimal("11.00"),
    down: Decimal | None = Decimal("9.00"),
    hk_connect: bool = True,
) -> MarketStatusView:
    return MarketStatusView(
        code=code,
        trade_date=date(2026, 1, 6),
        board=board,
        is_suspended=suspended,
        is_st=False,
        up_limit_price=up,
        down_limit_price=down,
        is_hk_connect=hk_connect,
    )


def make_pos(
    *,
    qty: int = 0,
    frozen: int = 0,
    code: str = "600519",
) -> PositionView:
    return PositionView(
        account_id="acc_value",
        mode="value",
        code=code,
        qty=qty,
        frozen_qty=frozen,
        avg_cost=Decimal("10"),
        avg_cost_cny=Decimal("10"),
    )


def make_ctx(
    *,
    ms: MarketStatusView | None = None,
    pos: PositionView | None = None,
    cash: Decimal = Decimal("100000"),
    market_value: Decimal = Decimal("0"),
    total: Decimal = Decimal("100000"),
    is_trading_day: bool = True,
    current_price: Decimal | None = Decimal("10.00"),
    account_mode: str = "value",
    target_ratio: Decimal = Decimal("0.8"),
) -> RuleContext:
    return RuleContext(
        trade_date=date(2026, 1, 6),
        market_status=ms,
        position=pos,
        track_cash=cash,
        track_market_value=market_value,
        total_portfolio_value=total,
        config={},
        is_trading_day=is_trading_day,
        current_price=current_price,
        account_mode=account_mode,
        target_ratio=target_ratio,
    )


# ---------------------------------------------------------------------------
# Phase 1
# ---------------------------------------------------------------------------


class TestT1Rule:
    def test_a_share_sell_blocked_by_frozen(self) -> None:
        rule = T1Rule()
        intent = make_intent(side=OrderSide.SELL, qty=100)
        ctx = make_ctx(pos=make_pos(qty=200, frozen=200))
        result = rule.check(intent, ctx=ctx)
        assert result.passed is False

    def test_a_share_sell_allowed_within_available(self) -> None:
        rule = T1Rule()
        intent = make_intent(side=OrderSide.SELL, qty=100)
        ctx = make_ctx(pos=make_pos(qty=200, frozen=100))
        assert rule.check(intent, ctx=ctx).passed is True

    def test_buy_not_checked(self) -> None:
        rule = T1Rule()
        assert rule.check(make_intent(), ctx=make_ctx()).passed is True

    def test_hk_skipped(self) -> None:
        rule = T1Rule()
        intent = make_intent(market="HK", code="00700", side=OrderSide.SELL, qty=100)
        ctx = make_ctx(pos=make_pos(qty=100, frozen=100, code="00700"))
        assert rule.check(intent, ctx=ctx).passed is True


class TestTradingDayRule:
    def test_non_trading_day_rejected(self) -> None:
        rule = TradingDayRule()
        result = rule.check(make_intent(), ctx=make_ctx(is_trading_day=False))
        assert result.passed is False

    def test_trading_day_ok(self) -> None:
        assert TradingDayRule().check(make_intent(), ctx=make_ctx()).passed is True


class TestSuspensionRule:
    def test_no_market_status_rejected(self) -> None:
        result = SuspensionRule().check(make_intent(), ctx=make_ctx(ms=None))
        assert result.passed is False

    def test_suspended_rejected(self) -> None:
        result = SuspensionRule().check(make_intent(), ctx=make_ctx(ms=make_ms(suspended=True)))
        assert result.passed is False

    def test_normal_ok(self) -> None:
        assert SuspensionRule().check(make_intent(), ctx=make_ctx(ms=make_ms())).passed is True


class TestPriceLimitRule:
    def test_limit_price_out_of_range(self) -> None:
        intent = make_intent(price=Decimal("12.00"))
        result = PriceLimitRule().check(intent, ctx=make_ctx(ms=make_ms()))
        assert result.passed is False

    def test_limit_price_in_range(self) -> None:
        intent = make_intent(price=Decimal("10.50"))
        assert PriceLimitRule().check(intent, ctx=make_ctx(ms=make_ms())).passed is True

    def test_market_buy_at_up_limit_blocked(self) -> None:
        intent = make_intent(order_type=OrderType.MARKET, price=None)
        ctx = make_ctx(ms=make_ms(), current_price=Decimal("11.00"))  # 恰达涨停
        result = PriceLimitRule().check(intent, ctx=ctx)
        assert result.passed is False

    def test_market_sell_at_down_limit_blocked(self) -> None:
        intent = make_intent(order_type=OrderType.MARKET, side=OrderSide.SELL, price=None)
        ctx = make_ctx(ms=make_ms(), current_price=Decimal("9.00"))
        assert PriceLimitRule().check(intent, ctx=ctx).passed is False

    def test_hk_no_limits_skipped(self) -> None:
        intent = make_intent(market="HK", code="00700", price=Decimal("999"))
        ctx = make_ctx(ms=make_ms(code="00700", board="HK", up=None, down=None))
        assert PriceLimitRule().check(intent, ctx=ctx).passed is True


class TestLotSizeRule:
    def test_lot_100_ok(self) -> None:
        assert LotSizeRule().check(make_intent(qty=100), ctx=make_ctx(ms=make_ms())).passed is True

    def test_lot_150_rejected(self) -> None:
        result = LotSizeRule().check(make_intent(qty=150), ctx=make_ctx(ms=make_ms()))
        assert result.passed is False

    def test_star_min_200(self) -> None:
        ctx = make_ctx(ms=make_ms(board="STAR"))
        assert LotSizeRule().check(make_intent(qty=100), ctx=ctx).passed is False
        assert LotSizeRule().check(make_intent(qty=200), ctx=ctx).passed is True
        assert LotSizeRule().check(make_intent(qty=201), ctx=ctx).passed is True

    def test_zero_qty_rejected(self) -> None:
        assert LotSizeRule().check(make_intent(qty=0), ctx=make_ctx(ms=make_ms())).passed is False

    def test_hk_no_lot_constraint(self) -> None:
        intent = make_intent(market="HK", code="00700", qty=7)
        ctx = make_ctx(ms=make_ms(code="00700", board="HK"))
        assert LotSizeRule().check(intent, ctx=ctx).passed is True


class TestHKConnectRule:
    def test_buy_not_in_list_rejected(self) -> None:
        intent = make_intent(market="HK", code="00700")
        ctx = make_ctx(ms=make_ms(code="00700", board="HK", hk_connect=False))
        result = HKConnectRule().check(intent, ctx=ctx)
        assert result.passed is False

    def test_sell_allowed_when_out_of_list(self) -> None:
        intent = make_intent(market="HK", code="00700", side=OrderSide.SELL)
        ctx = make_ctx(ms=make_ms(code="00700", board="HK", hk_connect=False))
        assert HKConnectRule().check(intent, ctx=ctx).passed is True

    def test_a_share_skipped(self) -> None:
        assert HKConnectRule().check(make_intent(), ctx=make_ctx(ms=make_ms())).passed is True


# ---------------------------------------------------------------------------
# Phase 2
# ---------------------------------------------------------------------------


class TestCashSufficiencyRule:
    def test_insufficient_cash_rejected(self) -> None:
        rule = CashSufficiencyRule()
        intent = make_intent(qty=100, price=Decimal("10.00"))
        ctx = make_ctx(cash=Decimal("500"))  # 需要 1000 + 费用
        result = rule.check(intent, ctx=ctx)
        assert result.passed is False

    def test_sufficient_cash_ok(self) -> None:
        rule = CashSufficiencyRule()
        intent = make_intent(qty=100, price=Decimal("10.00"))
        ctx = make_ctx(cash=Decimal("100000"))
        assert rule.check(intent, ctx=ctx).passed is True

    def test_market_order_uses_current_price(self) -> None:
        rule = CashSufficiencyRule()
        intent = make_intent(order_type=OrderType.MARKET, price=None, qty=100)
        ctx = make_ctx(cash=Decimal("500"), current_price=Decimal("10.00"))
        assert rule.check(intent, ctx=ctx).passed is False

    def test_sell_not_checked(self) -> None:
        rule = CashSufficiencyRule()
        intent = make_intent(side=OrderSide.SELL, qty=100)
        ctx = make_ctx(cash=Decimal("0"))
        assert rule.check(intent, ctx=ctx).passed is True


class TestPositionLimitRule:
    def test_sell_without_position_rejected(self) -> None:
        rule = PositionLimitRule()
        intent = make_intent(side=OrderSide.SELL, qty=100)
        result = rule.check(intent, ctx=make_ctx(pos=None))
        assert result.passed is False

    def test_sell_beyond_available_rejected(self) -> None:
        rule = PositionLimitRule()
        intent = make_intent(side=OrderSide.SELL, qty=200)
        ctx = make_ctx(pos=make_pos(qty=200, frozen=100))
        assert rule.check(intent, ctx=ctx).passed is False

    def test_sell_within_available_ok(self) -> None:
        rule = PositionLimitRule()
        intent = make_intent(side=OrderSide.SELL, qty=100)
        ctx = make_ctx(pos=make_pos(qty=200, frozen=100))
        assert rule.check(intent, ctx=ctx).passed is True


class TestModeIsolationRule:
    def test_mode_mismatch_rejected(self) -> None:
        rule = ModeIsolationRule()
        intent = make_intent(mode="swing")
        result = rule.check(intent, ctx=make_ctx(account_mode="value"))
        assert result.passed is False

    def test_mode_match_ok(self) -> None:
        rule = ModeIsolationRule()
        intent = make_intent(mode="value")
        assert rule.check(intent, ctx=make_ctx(account_mode="value")).passed is True


class TestQuotaRule:
    """已确认：双轨配额为软限制——超限 WARN 不阻断。"""

    def test_over_quota_warn_but_pass(self) -> None:
        rule = QuotaRule()
        # 本轨 90000/100000 = 0.9 > 0.8 + 0.05
        ctx = make_ctx(cash=Decimal("90000"), total=Decimal("100000"))
        result = rule.check(make_intent(), ctx=ctx)
        assert result.passed is True
        assert result.severity == "WARN"

    def test_within_tolerance_info(self) -> None:
        rule = QuotaRule()
        ctx = make_ctx(cash=Decimal("84000"), total=Decimal("100000"))  # 0.84 = 0.8 + 0.05 边界内
        result = rule.check(make_intent(), ctx=ctx)
        assert result.passed is True
        assert result.severity == "INFO"

    def test_sell_not_checked(self) -> None:
        rule = QuotaRule()
        intent = make_intent(side=OrderSide.SELL)
        ctx = make_ctx(cash=Decimal("90000"), total=Decimal("100000"))
        result = rule.check(intent, ctx=ctx)
        assert result.passed is True
        assert result.severity == "INFO"


# ---------------------------------------------------------------------------
# 管线
# ---------------------------------------------------------------------------


class TestTradingRuleEngine:
    def test_pipeline_stops_on_first_hard_rejection(self) -> None:
        class Probe(CashSufficiencyRule):
            called = False

            def check(self, intent, *, ctx):
                Probe.called = True
                return super().check(intent, ctx=ctx)

        engine = TradingRuleEngine([SuspensionRule(), Probe()])
        intent = make_intent(qty=100)
        ctx = make_ctx(ms=make_ms(suspended=True), cash=Decimal("1"))
        result = engine.validate(intent, ctx)
        assert result.passed is False
        assert "SUSPENSION" in result.rejection_reason
        assert Probe.called is False  # Phase1 拒绝后 Phase2 不执行

    def test_warn_does_not_block(self) -> None:
        engine = TradingRuleEngine(
            [T1Rule(), CashSufficiencyRule(), ModeIsolationRule(), QuotaRule()]
        )
        intent = make_intent()
        ctx = make_ctx(cash=Decimal("90000"), total=Decimal("100000"))  # Quota WARN
        result = engine.validate(intent, ctx)
        assert result.passed is True
        assert len(result.warnings) == 1
        assert result.warnings[0].rule_id == "QUOTA"

    def test_all_pass_clean(self) -> None:
        engine = TradingRuleEngine(
            [
                T1Rule(),
                TradingDayRule(),
                SuspensionRule(),
                PriceLimitRule(),
                LotSizeRule(),
                HKConnectRule(),
                CashSufficiencyRule(),
                PositionLimitRule(),
                ModeIsolationRule(),
                QuotaRule(),
            ]
        )
        result = engine.validate(make_intent(), ctx=make_ctx(ms=make_ms()))
        assert result.passed is True
        assert len(result.results) == 10

    def test_invalid_phase_rejected(self) -> None:
        class BadRule:
            rule_id = "BAD"
            phase = "WRONG"

            def check(self, intent, *, ctx):
                return RuleCheckResult(passed=True, rule_id="BAD")

        with pytest.raises(ValueError, match="phase"):
            TradingRuleEngine([BadRule()])  # type: ignore[list-item]

    def test_phase_order_enforced(self) -> None:
        """Phase 顺序：MARKET → ACCOUNT → RISK，与规则传入顺序无关。"""
        engine = TradingRuleEngine([QuotaRule(), SuspensionRule()])
        intent = make_intent()
        ctx = make_ctx(ms=make_ms(suspended=True))
        result = engine.validate(intent, ctx)
        assert result.results[0].rule_id == "SUSPENSION"
        assert RulePhase.MARKET == "MARKET"
