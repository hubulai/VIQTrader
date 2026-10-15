"""P2 Phase 1 市场规则（设计文档 3.2）：能不能交易。

T1Rule / TradingDayRule / SuspensionRule / PriceLimitRule / LotSizeRule / HKConnectRule
"""

from __future__ import annotations

from viqtrader.engine.types import (
    MarketStatusView,
    OrderIntent,
    OrderSide,
    OrderType,
    RuleCheckResult,
    RuleContext,
    RulePhase,
)

_A_SHARE_MARKETS = frozenset({"SH", "SZ", "BJ"})


def _ok(rule_id: str, message: str = "") -> RuleCheckResult:
    return RuleCheckResult(passed=True, rule_id=rule_id, message=message)


def _deny(rule_id: str, message: str) -> RuleCheckResult:
    return RuleCheckResult(passed=False, rule_id=rule_id, message=message)


# ---------------------------------------------------------------------------
# T1Rule（A 股 T+1：当日买入不可卖；港股通 T+0 跳过）
# ---------------------------------------------------------------------------


class T1Rule:
    rule_id = "T1"
    phase = RulePhase.MARKET

    def check(self, intent: OrderIntent, *, ctx: RuleContext) -> RuleCheckResult:
        if intent.market not in _A_SHARE_MARKETS:
            return _ok(self.rule_id, "非 A 股跳过 T+1")
        if intent.side != OrderSide.SELL:
            return _ok(self.rule_id)
        pos = ctx.position
        if pos is None:
            return _ok(self.rule_id, "无持仓，交由 PositionLimitRule 拒绝")
        if intent.qty > pos.available_qty:
            return _deny(
                self.rule_id,
                f"T+1 当日买入冻结 {pos.frozen_qty} 股，"
                f"可卖 {pos.available_qty} < 意图卖出 {intent.qty}",
            )
        return _ok(self.rule_id)


# ---------------------------------------------------------------------------
# TradingDayRule（非交易日拒绝）
# ---------------------------------------------------------------------------


class TradingDayRule:
    rule_id = "TRADING_DAY"
    phase = RulePhase.MARKET

    def check(self, intent: OrderIntent, *, ctx: RuleContext) -> RuleCheckResult:
        if not ctx.is_trading_day:
            return _deny(self.rule_id, f"{ctx.trade_date} 非交易日")
        return _ok(self.rule_id)


# ---------------------------------------------------------------------------
# SuspensionRule（停牌不可交易）
# ---------------------------------------------------------------------------


class SuspensionRule:
    rule_id = "SUSPENSION"
    phase = RulePhase.MARKET

    def check(self, intent: OrderIntent, *, ctx: RuleContext) -> RuleCheckResult:
        ms = ctx.market_status
        if ms is None:
            return _deny(
                self.rule_id,
                f"{intent.code} 在 {ctx.trade_date} 无市场状态快照（视为停牌/数据缺失）",
            )
        if ms.is_suspended:
            return _deny(self.rule_id, f"{intent.code} 在 {ctx.trade_date} 停牌")
        return _ok(self.rule_id)


# ---------------------------------------------------------------------------
# PriceLimitRule（涨跌停校验）
# ---------------------------------------------------------------------------


class PriceLimitRule:
    rule_id = "PRICE_LIMIT"
    phase = RulePhase.MARKET

    def check(self, intent: OrderIntent, *, ctx: RuleContext) -> RuleCheckResult:
        ms: MarketStatusView | None = ctx.market_status
        if ms is None or ms.up_limit_price is None or ms.down_limit_price is None:
            return _ok(self.rule_id, "无涨跌停价（HK 或前收盘缺失），跳过")

        if intent.order_type == OrderType.LIMIT:
            price = intent.price
            if price is None:
                return _deny(self.rule_id, "LIMIT 单缺少限价")
            if price > ms.up_limit_price or price < ms.down_limit_price:
                return _deny(
                    self.rule_id,
                    f"限价 {price} 超出涨跌停区间 [{ms.down_limit_price}, {ms.up_limit_price}]",
                )
            return _ok(self.rule_id)

        if intent.order_type == OrderType.MARKET:
            cur = ctx.current_price
            if cur is None:
                return _ok(self.rule_id, "MARKET 单无当前价，跳过涨跌停判断")
            if intent.side == OrderSide.BUY and cur >= ms.up_limit_price:
                return _deny(self.rule_id, f"当前价 {cur} 已达涨停 {ms.up_limit_price}，不可买入")
            if intent.side == OrderSide.SELL and cur <= ms.down_limit_price:
                return _deny(self.rule_id, f"当前价 {cur} 已达跌停 {ms.down_limit_price}，不可卖出")
            return _ok(self.rule_id)

        # STOP / STOP_LIMIT 由策略层触发后转 LIMIT/MARKET，此处不校验
        return _ok(self.rule_id, "STOP 类订单不在此校验")


# ---------------------------------------------------------------------------
# LotSizeRule（手数校验）
# ---------------------------------------------------------------------------


class LotSizeRule:
    """手数规则：主板/北交所整百；科创板 ≥200 股后按 1 股递增；港股通 v1 简化 lot=1。"""

    rule_id = "LOT_SIZE"
    phase = RulePhase.MARKET

    def check(self, intent: OrderIntent, *, ctx: RuleContext) -> RuleCheckResult:
        if intent.qty <= 0:
            return _deny(self.rule_id, f"数量必须为正，当前 {intent.qty}")
        lot_cfg = (ctx.config.get("trading", {}) or {}).get("lot_size", {}) or {}
        board = ctx.market_status.board if ctx.market_status else None

        if intent.market == "HK":
            return _ok(self.rule_id, "港股通 v1 手数简化为 1")
        if board == "STAR":
            if intent.qty < int(lot_cfg.get("star_min", 200)):
                return _deny(self.rule_id, f"科创板最小申报 200 股，当前 {intent.qty}")
            return _ok(self.rule_id)
        # 主板 / 创业板 / 北交所：整百
        if intent.qty % int(lot_cfg.get("main", 100)) != 0:
            return _deny(self.rule_id, f"数量 {intent.qty} 不是整百手数")
        return _ok(self.rule_id)


# ---------------------------------------------------------------------------
# HKConnectRule（港股通专项：调出名单后只能卖不能买）
# ---------------------------------------------------------------------------


class HKConnectRule:
    rule_id = "HK_CONNECT"
    phase = RulePhase.MARKET

    def check(self, intent: OrderIntent, *, ctx: RuleContext) -> RuleCheckResult:
        if intent.market != "HK":
            return _ok(self.rule_id)
        ms = ctx.market_status
        if ms is None:
            return _ok(self.rule_id, "无市场状态，交由 SuspensionRule 拒绝")
        if intent.side == OrderSide.BUY and not ms.is_hk_connect:
            return _deny(self.rule_id, f"{intent.code} 不在港股通名单（调出后只能卖不能买）")
        return _ok(self.rule_id)
