"""P3.4 波段轨回测专属指标（纯函数零 DB / 零 LLM，引擎零改动）。

统计单元：FIFO round-trip（2026-10-16 裁决）。
- BUY 批次入队，单位成本 = |amount_cny| / qty（amount_cny 已含买入费用，CNY 口径）；
- SELL 按 FIFO 与历史 BUY 批次配对，卖出单位收入 = amount_cny / qty（已含卖出费用）；
- round-trip 净盈亏 = 卖出摊得现金流 - 匹配批次成本（含费净额，可逐笔对账闭合）；
- 平保单（pnl == 0）计入胜率分母作非胜（2026-10-16 裁决），breakeven_count 单列透明；
- 未平仓批次不参与胜率/盈亏统计，open_lot_count 单列。

架构约束：lifecycle 层位于 engine 上层（import-linter 分层契约），本模块不得 import
lifecycle；策略归因（trade_log JOIN order_intent 取 strategy_id）由上层
lifecycle.equity.strategy_trades 完成，本模块用 Protocol 结构类型接收同构成交。

不可计算语义（禁止用默认值掩盖）：
- 无已平仓 round-trip：胜率/均值/期望/收益类指标全部 None；
- 无亏损单：payoff_ratio / profit_factor 返回 None（不用 inf）；
- 未注入 equity_series：annual_turnover 返回 None；
- max_holding_days=None：breach_count=None 表示"未统计"，区别于统计值 0。
"""

from __future__ import annotations

import statistics
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol

TRADING_DAYS_PER_YEAR = 252


class SwingTradeLike(Protocol):
    """单条策略归属成交的结构类型（与 lifecycle.equity.StrategyTrade / TradeLog 同构）。"""

    trade_date: date
    code: str
    side: str  # BUY / SELL
    qty: int
    amount_cny: float | Decimal  # CNY 现金流（BUY 负 / SELL 正，均已含费）


@dataclass(frozen=True, slots=True)
class RoundTrip:
    """一笔已平仓的 FIFO 往返（金额均 CNY 含费净额）。"""

    code: str
    open_date: date
    close_date: date
    qty: int
    cost_cny: float  # 匹配批次成本（|买入 amount_cny| 按 qty 摊销）
    proceeds_cny: float  # 卖出摊得现金流（卖出 amount_cny 按 qty 摊销）
    pnl_cny: float  # proceeds - cost
    holding_days: int  # 自然日


@dataclass(frozen=True, slots=True)
class SwingHoldingStats:
    """已平仓 round-trip 持有天数分布（自然日）；无样本时统计值全 None。"""

    mean_days: float | None
    median_days: float | None
    max_days: float | None
    min_days: float | None
    breach_count: int | None  # holding_days > max_holding_days 的笔数；未配置上限为 None


@dataclass(frozen=True, slots=True)
class SwingBacktestMetrics:
    """波段轨交易统计（dataclasses.asdict 可 JSON 序列化）。

    通用收益指标（total_return/max_drawdown/sharpe）不在此重复实现，
    调用方复用 lifecycle.kpi.compute_kpi_snapshot（同源 252/0 利率口径）。
    """

    roundtrip_count: int
    win_count: int  # pnl > 0
    loss_count: int  # pnl < 0
    breakeven_count: int  # pnl == 0
    win_rate: float | None  # win_count / roundtrip_count（平保计入分母作非胜）
    loss_rate: float | None  # loss_count / roundtrip_count
    avg_win_cny: float | None  # 盈利单平均盈亏（正数）
    avg_loss_cny: float | None  # 亏损单平均亏损额（正数，绝对值口径）
    payoff_ratio: float | None  # avg_win / avg_loss；无亏损单 None
    profit_factor: float | None  # 总盈利 / |总亏损|；总亏损为 0 时 None
    expectancy_cny: float | None  # 全部 round-trip 的平均净盈亏（含平保）
    avg_roundtrip_return: float | None  # 平均（pnl / cost）成本收益率
    annual_turnover: float | None
    open_lot_count: int  # 未平仓 FIFO 批次总数
    holding: SwingHoldingStats


def compute_swing_metrics(
    trades: Sequence[SwingTradeLike],
    *,
    equity_series: Sequence[tuple[date, float]] | None = None,
    max_holding_days: int | None = None,
) -> SwingBacktestMetrics:
    """计算波段轨交易统计（纯函数，不碰数据库/网络）。

    Args:
        trades: 单策略归属成交（乱序自动按日期稳定排序；同日保持输入先后）。
            必须含完整建仓起点：SELL 超过 FIFO 持仓时 ValueError（防区间截断致口径漂移）。
        equity_series: 策略逐日净值（调用方用 lifecycle.equity.replay_strategy_equity
            重放），作为年换手率分母；None 时年换手率为 None。
        max_holding_days: 持有期上限（自然日，取 config swing_track.risk.max_holding_days
            注入）；holding_days 严格大于该值计超期；None 时不统计 breach_count。
    """
    ordered = sorted(trades, key=lambda t: t.trade_date)
    roundtrips, open_lot_count = _fifo_roundtrips(ordered)
    return _summarize(
        roundtrips,
        open_lot_count=open_lot_count,
        equity_series=equity_series,
        max_holding_days=max_holding_days,
        sell_total_cny=sum(float(t.amount_cny) for t in ordered if t.side == "SELL"),
    )


# ---------------------------------------------------------------------------
# FIFO 配对
# ---------------------------------------------------------------------------


def _fifo_roundtrips(
    trades: Sequence[SwingTradeLike],
) -> tuple[list[RoundTrip], int]:
    """FIFO 撮合：返回 (已平仓 round-trip 列表, 未平仓批次总数)。

    批次队列元素 = (买入日, 剩余数量, 单位成本)；SELL 超出持仓抛 ValueError。
    """
    lots: dict[str, deque[tuple[date, int, float]]] = {}
    closed: list[RoundTrip] = []
    for t in trades:
        qty = t.qty
        amount = float(t.amount_cny)
        queue = lots.setdefault(t.code, deque())
        if t.side == "BUY":
            if qty <= 0:
                raise ValueError(f"BUY 数量必须为正: {t.code} {t.trade_date} qty={qty}")
            queue.append((t.trade_date, qty, abs(amount) / qty))
            continue
        if qty <= 0:
            raise ValueError(f"SELL 数量必须为正: {t.code} {t.trade_date} qty={qty}")
        sell_per_share = amount / qty  # 单位卖出收入（已扣卖出费用）
        remaining = qty
        while remaining > 0:
            if not queue:
                raise ValueError(
                    f"SELL 超过 FIFO 持仓（成交序列缺建仓起点或数据损坏）: {t.code} {t.trade_date}"
                )
            lot_date, lot_qty, lot_cost_pps = queue[0]
            matched = min(lot_qty, remaining)
            cost = lot_cost_pps * matched
            proceeds = sell_per_share * matched
            closed.append(
                RoundTrip(
                    code=t.code,
                    open_date=lot_date,
                    close_date=t.trade_date,
                    qty=matched,
                    cost_cny=cost,
                    proceeds_cny=proceeds,
                    pnl_cny=proceeds - cost,
                    holding_days=(t.trade_date - lot_date).days,
                )
            )
            remaining -= matched
            if matched == lot_qty:
                queue.popleft()
            else:
                queue[0] = (lot_date, lot_qty - matched, lot_cost_pps)
    open_lot_count = sum(len(q) for q in lots.values())
    return closed, open_lot_count


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------


def _summarize(
    roundtrips: Sequence[RoundTrip],
    *,
    open_lot_count: int,
    equity_series: Sequence[tuple[date, float]] | None,
    max_holding_days: int | None,
    sell_total_cny: float,
) -> SwingBacktestMetrics:
    n = len(roundtrips)
    wins = [rt.pnl_cny for rt in roundtrips if rt.pnl_cny > 0]
    losses = [rt.pnl_cny for rt in roundtrips if rt.pnl_cny < 0]
    breakeven = n - len(wins) - len(losses)

    if n == 0:
        return SwingBacktestMetrics(
            roundtrip_count=0,
            win_count=0,
            loss_count=0,
            breakeven_count=0,
            win_rate=None,
            loss_rate=None,
            avg_win_cny=None,
            avg_loss_cny=None,
            payoff_ratio=None,
            profit_factor=None,
            expectancy_cny=None,
            avg_roundtrip_return=None,
            annual_turnover=_annual_turnover(sell_total_cny, equity_series),
            open_lot_count=open_lot_count,
            holding=SwingHoldingStats(None, None, None, None, None),
        )

    avg_win = statistics.fmean(wins) if wins else None
    avg_loss = abs(statistics.fmean(losses)) if losses else None
    total_loss = abs(sum(losses))
    days = [rt.holding_days for rt in roundtrips]
    breach: int | None = None
    if max_holding_days is not None:
        breach = sum(1 for d in days if d > max_holding_days)

    return SwingBacktestMetrics(
        roundtrip_count=n,
        win_count=len(wins),
        loss_count=len(losses),
        breakeven_count=breakeven,
        win_rate=len(wins) / n,
        loss_rate=len(losses) / n,
        avg_win_cny=avg_win,
        avg_loss_cny=avg_loss,
        payoff_ratio=(avg_win / avg_loss if avg_win is not None and avg_loss is not None else None),
        profit_factor=(sum(wins) / total_loss if total_loss > 0 else None),
        expectancy_cny=statistics.fmean(rt.pnl_cny for rt in roundtrips),
        avg_roundtrip_return=(
            statistics.fmean(rt.pnl_cny / rt.cost_cny for rt in roundtrips if rt.cost_cny > 0)
            if any(rt.cost_cny > 0 for rt in roundtrips)
            else None
        ),
        annual_turnover=_annual_turnover(sell_total_cny, equity_series),
        open_lot_count=open_lot_count,
        holding=SwingHoldingStats(
            mean_days=statistics.fmean(days),
            median_days=float(statistics.median(days)),
            max_days=float(max(days)),
            min_days=float(min(days)),
            breach_count=breach,
        ),
    )


def _annual_turnover(
    sell_total_cny: float, equity_series: Sequence[tuple[date, float]] | None
) -> float | None:
    """年卖出额 / 平均净值，252 交易日年化；未注入净值或均资产 <= 0 时 None。"""
    if not equity_series:
        return None
    values = [v for _, v in equity_series]
    avg_assets = statistics.fmean(values)
    if avg_assets <= 0:
        return None
    return sell_total_cny / avg_assets * (TRADING_DAYS_PER_YEAR / len(values))
