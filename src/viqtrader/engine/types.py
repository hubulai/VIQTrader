"""P2 引擎层核心类型与契约（设计文档第二节、第九节）。

约定：
- 引擎内部金额全程 Decimal；持久化到 DuckDB 时转 DOUBLE（转换点统一在 store 写入处）
- OrderIntent 不可变（frozen）：策略层/Agent/人工只产出意图，不碰执行细节
- RuleCheckResult.severity=WARN 表示只告警不阻断（QuotaRule 软限制，已确认决策）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, overload


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"  # v1 网关不支持，TODO P3+
    STOP_LIMIT = "STOP_LIMIT"  # v1 网关不支持，TODO P3+


class TimeInForce(StrEnum):
    GTC = "GTC"
    IOC = "IOC"
    FOK = "FOK"


class IntentStatus(StrEnum):
    PENDING = "PENDING"
    REJECTED = "REJECTED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"


class RulePhase(StrEnum):
    MARKET = "MARKET"
    ACCOUNT = "ACCOUNT"
    RISK = "RISK"


class FillStatus(StrEnum):
    FILLED = "FILLED"
    NOT_FILLED = "NOT_FILLED"  # 未成交（挂单/无行情/涨跌停封死）
    UNSUPPORTED = "UNSUPPORTED"  # 订单类型网关不支持


@dataclass(frozen=True, slots=True)
class OrderIntent:
    """订单意图：不可变，规则引擎与账本引擎的唯一输入。"""

    intent_id: str
    account_id: str  # 归属子账本（acc_value / acc_swing）
    mode: str  # value / swing（冗余字段，便于过滤）
    code: str
    market: str  # SH / SZ / BJ / HK
    side: OrderSide
    order_type: OrderType
    qty: int  # 数量（股）
    price: Decimal | None  # 限价（MARKET 单为 None，原币）
    stop_price: Decimal | None = None
    time_in_force: TimeInForce = TimeInForce.GTC
    thesis_id: str | None = None  # 价值轨必填（校验在 Agent 层）
    strategy_id: str | None = None  # 波段轨必填（校验在 Agent 层）
    submitted_at: datetime = field(default_factory=datetime.now)


# ---------------------------------------------------------------------------
# 规则引擎上下文与结果
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MarketStatusView:
    """market_status 表的内存视图（规则引擎只读）。"""

    code: str
    trade_date: date
    board: str  # MAIN / CHINEXT / STAR / BSE / HK
    is_suspended: bool
    is_st: bool
    up_limit_price: Decimal | None
    down_limit_price: Decimal | None
    is_hk_connect: bool


@dataclass(frozen=True, slots=True)
class PositionView:
    """position 表的内存视图（规则引擎只读）。"""

    account_id: str
    mode: str
    code: str
    qty: int
    frozen_qty: int
    avg_cost: Decimal  # 原币
    avg_cost_cny: Decimal

    @property
    def available_qty(self) -> int:
        return self.qty - self.frozen_qty


@dataclass(frozen=True, slots=True)
class RuleCheckResult:
    """单条规则校验结果。severity=WARN 只告警不阻断（软限制）。"""

    passed: bool
    rule_id: str
    message: str = ""
    severity: str = "INFO"  # INFO / WARN


@dataclass(slots=True)
class RuleContext:
    """规则校验上下文（由执行服务按 Intent 构建）。

    design 契约字段之外扩展了 is_trading_day / current_price / account_mode /
    target_ratio / fx_rate，保持向后兼容（均有默认值）。
    """

    trade_date: date
    market_status: MarketStatusView | None
    position: PositionView | None
    track_cash: Decimal  # 本轨可用现金（CNY）
    track_market_value: Decimal  # 本轨持仓市值（CNY）
    total_portfolio_value: Decimal  # 双轨总资产（CNY）
    config: dict[str, Any]
    # ---- 扩展字段 ----
    is_trading_day: bool = True  # 交易日历判断（调用方注入）
    current_price: Decimal | None = None  # 标的当前价（原币，一般取当日收盘）
    account_mode: str = ""  # account.mode（ModeIsolationRule）
    target_ratio: Decimal = Decimal("0")  # 本轨目标配比（QuotaRule）
    fx_rate: Decimal = Decimal("1")  # 原币→CNY（A 股为 1，HK 用最近汇率）


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """三阶段校验整体结果：任一 passed=False 即整体不通过。"""

    passed: bool
    results: tuple[RuleCheckResult, ...] = ()
    rejection_reason: str = ""

    @property
    def warnings(self) -> tuple[RuleCheckResult, ...]:
        return tuple(r for r in self.results if r.severity == "WARN" and r.passed)


# ---------------------------------------------------------------------------
# 费用与成交
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FeeBreakdown:
    """费用明细（统一 CNY 口径；港股费用按汇率折算后给出）。"""

    commission: Decimal
    stamp_tax: Decimal
    transfer_fee: Decimal

    @property
    def total(self) -> Decimal:
        return self.commission + self.stamp_tax + self.transfer_fee


@dataclass(frozen=True, slots=True)
class FillResult:
    """网关撮合结果。"""

    status: FillStatus
    fill_price: Decimal | None = None  # 原币
    message: str = ""


@dataclass(frozen=True, slots=True)
class TradeLog:
    """execute_fill 的返回：与 trade_log 表对应 + 派生信息。"""

    trade_id: str
    account_id: str
    mode: str
    code: str
    side: str
    qty: int
    price: Decimal  # 原币
    commission: Decimal  # CNY
    stamp_tax: Decimal  # CNY
    transfer_fee: Decimal  # CNY
    fx_rate: Decimal
    amount_cny: Decimal  # 人民币结算额（现金流水口径）
    trade_date: date
    intent_id: str | None
    realized_pnl_cny: Decimal = Decimal("0")  # SELL 时的已实现盈亏（含费用，不含汇兑）


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class InsufficientCashAtFill(Exception):
    """FILLED 扣款前资金重检失败（兜底路径，见设计文档 5.2）。"""


@overload
def to_decimal(value: float | Decimal | int | str | None, default: Decimal) -> Decimal: ...


@overload
def to_decimal(
    value: float | Decimal | int | str | None, default: None = None
) -> Decimal | None: ...


def to_decimal(
    value: float | Decimal | int | str | None, default: Decimal | None = None
) -> Decimal | None:
    """float/Decimal → Decimal；None 时返回 default（float 经 str 转换避免二进制误差）。"""
    if value is None:
        return default
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))
