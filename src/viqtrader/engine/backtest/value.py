"""P3.3 价值轨回测专属指标（与 result.py 通用指标互补，零 LLM 纯函数）。

口径说明：
- XIRR：按 trade_log 实际现金流（BUY 为负 / SELL 为正，amount_cny 账本口径）
  + 期末市值终值，二分法解 NPV=0 得年化内部收益率；现金流未变号时返回 None；
- 相对超额（算术口径）= 组合累计收益 - 基准累计收益；基准序列由调用方注入
  （默认沪深 300，见 config value_track.benchmark_code），缺省则超额为 None；
- 持有期分布：FIFO 拆 round-trip，已平仓给 count/mean/median/p25/p75/max/min；
  未平仓持仓按最早未平批次浮动天数单列；
- 年换手率 = 卖出总额 / 平均资产，按 252 交易日年化（供风格漂移告警，不阻断）。

PeriodicSchedule：策略在 on_bar 问"今天是否再平衡日"的纯函数工具，
支持每月末 / 每季度末 / 每 N 交易日三种模式，引擎零改动。
"""

from __future__ import annotations

import itertools
import math
import statistics
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from viqtrader.engine.backtest.result import BacktestResult
from viqtrader.engine.types import TradeLog

TRADING_DAYS_PER_YEAR = 252
_DAYS_PER_YEAR = 365.0


# ---------------------------------------------------------------------------
# 结果结构（dataclasses.asdict 可直接 JSON 序列化；日期一律 ISO 字符串）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HoldingPeriodStats:
    """已平仓 round-trip 持有天数分布（自然日）；count=0 时统计值全 None。"""

    count: int
    mean_days: float | None
    median_days: float | None
    p25_days: float | None
    p75_days: float | None
    max_days: float | None
    min_days: float | None


@dataclass(frozen=True, slots=True)
class OpenPositionHolding:
    """未平仓持仓的浮动持有天数（open_date 为最早未平批次的买入日，ISO 字符串）。"""

    code: str
    open_date: str
    qty: int
    holding_days: int


@dataclass(frozen=True, slots=True)
class ValueBacktestMetrics:
    """价值轨专属指标汇总；generic_metrics 复用 result.metrics() 不重复实现。"""

    account_id: str
    generic_metrics: dict[str, float | int | None]
    xirr: float | None
    benchmark_total_return: float | None
    excess_return_arithmetic: float | None
    annual_turnover: float | None
    holding_period: HoldingPeriodStats
    open_positions: list[OpenPositionHolding] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def compute_value_metrics(
    result: BacktestResult,
    account_id: str,
    *,
    benchmark_series: list[tuple[date, float]] | None = None,
) -> ValueBacktestMetrics:
    """计算价值轨专属指标（纯函数，不碰数据库/网络）。

    Args:
        result: 回测结果（净值曲线 + 成交记录）。
        account_id: 目标子账本（通常 acc_value）。
        benchmark_series: 基准逐日点位 [(date, 点位)]，自动截取 [start, end] 区间；
            None 时超额相关指标为 None。
    """
    generic = result.metrics(account_id)
    acc_trades = sorted(
        (t for t in result.trades if t.account_id == account_id),
        key=lambda t: (t.trade_date, t.trade_id),
    )
    series = result.series(account_id)

    xirr = _compute_xirr(acc_trades, series)
    bench_ret, excess = _compute_excess(generic["total_return"], benchmark_series, result)
    turnover = _compute_annual_turnover(acc_trades, series)
    holding = _compute_holding_stats(acc_trades)
    open_positions = _compute_open_positions(acc_trades, result.end)

    return ValueBacktestMetrics(
        account_id=account_id,
        generic_metrics=generic,
        xirr=xirr,
        benchmark_total_return=bench_ret,
        excess_return_arithmetic=excess,
        annual_turnover=turnover,
        holding_period=holding,
        open_positions=open_positions,
    )


# ---------------------------------------------------------------------------
# XIRR
# ---------------------------------------------------------------------------


def _compute_xirr(trades: list[TradeLog], series: Sequence[tuple[date, Decimal]]) -> float | None:
    """按实际日期现金流解年化 IRR；需要至少一负一正两笔现金流。"""
    flows: list[tuple[date, float]] = [
        (t.trade_date, float(t.amount_cny)) for t in trades if float(t.amount_cny) != 0.0
    ]
    if series:
        end_date, end_value = series[-1]
        if float(end_value) > 0:  # 期末市值作为终值流入
            flows.append((end_date, float(end_value)))
    if len(flows) < 2:
        return None
    has_neg = any(cf < 0 for _, cf in flows)
    has_pos = any(cf > 0 for _, cf in flows)
    if not (has_neg and has_pos):
        return None
    return _solve_xirr(flows)


def _xnpv(rate: float, flows: list[tuple[date, float]]) -> float:
    d0 = flows[0][0]
    return math.fsum(cf / (1.0 + rate) ** ((d - d0).days / _DAYS_PER_YEAR) for d, cf in flows)


def _solve_xirr(flows: list[tuple[date, float]]) -> float | None:
    """二分法解 NPV=0；区间 [-0.9999, 10] 内无变号则判无解。"""
    flows = sorted(flows)
    lo, hi = -0.9999, 10.0
    f_lo, f_hi = _xnpv(lo, flows), _xnpv(hi, flows)
    if f_lo == 0.0:
        return lo
    if f_lo * f_hi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2.0
        f_mid = _xnpv(mid, flows)
        if abs(f_mid) < 1e-10:
            return mid
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2.0


# ---------------------------------------------------------------------------
# 相对超额（算术口径）
# ---------------------------------------------------------------------------


def _compute_excess(
    portfolio_total_return: float | int | None,
    benchmark_series: list[tuple[date, float]] | None,
    result: BacktestResult,
) -> tuple[float | None, float | None]:
    """返回 (基准累计收益, 算术超额)；基准不足 2 个点或组合收益缺失时为 None。"""
    if not benchmark_series:
        return None, None
    pts = sorted((d, v) for d, v in benchmark_series if result.start <= d <= result.end)
    if len(pts) < 2 or pts[0][1] <= 0:
        return None, None
    bench_ret = pts[-1][1] / pts[0][1] - 1.0
    if portfolio_total_return is None:
        return bench_ret, None
    excess = float(portfolio_total_return) - bench_ret
    return bench_ret, excess


# ---------------------------------------------------------------------------
# 年换手率
# ---------------------------------------------------------------------------


def _compute_annual_turnover(
    trades: list[TradeLog], series: Sequence[tuple[date, Decimal]]
) -> float | None:
    """年卖出额 / 平均资产，252 交易日年化；无净值或均资产为 0 时 None。"""
    if not series:
        return None
    values = [float(v) for _, v in series]
    avg_assets = statistics.fmean(values)
    if avg_assets <= 0:
        return None
    sell_total = sum(float(t.amount_cny) for t in trades if t.side == "SELL")
    return sell_total / avg_assets * (TRADING_DAYS_PER_YEAR / len(values))


# ---------------------------------------------------------------------------
# 持有期分布（FIFO round-trip）
# ---------------------------------------------------------------------------


def _fifo_lots(
    trades: list[TradeLog],
) -> tuple[list[float], dict[str, deque[tuple[date, int]]]]:
    """FIFO 撮合：返回 (已平仓持有天数列表, 各标的剩余批次队列)。"""
    lots: dict[str, deque[tuple[date, int]]] = {}
    closed_days: list[float] = []
    for t in trades:
        queue = lots.setdefault(t.code, deque())
        if t.side == "BUY":
            queue.append((t.trade_date, t.qty))
            continue
        remaining = t.qty
        while remaining > 0 and queue:
            lot_date, lot_qty = queue[0]
            matched = min(lot_qty, remaining)
            closed_days.append(float((t.trade_date - lot_date).days))
            remaining -= matched
            if matched == lot_qty:
                queue.popleft()
            else:
                queue[0] = (lot_date, lot_qty - matched)
    return closed_days, lots


def _compute_holding_stats(trades: list[TradeLog]) -> HoldingPeriodStats:
    closed_days, _ = _fifo_lots(trades)
    if not closed_days:
        return HoldingPeriodStats(0, None, None, None, None, None, None)
    ordered = sorted(closed_days)
    return HoldingPeriodStats(
        count=len(ordered),
        mean_days=statistics.fmean(ordered),
        median_days=_percentile(ordered, 0.5),
        p25_days=_percentile(ordered, 0.25),
        p75_days=_percentile(ordered, 0.75),
        max_days=ordered[-1],
        min_days=ordered[0],
    )


def _compute_open_positions(trades: list[TradeLog], end: date) -> list[OpenPositionHolding]:
    _, lots = _fifo_lots(trades)
    out: list[OpenPositionHolding] = []
    for code in sorted(lots):
        queue = lots[code]
        if not queue:
            continue
        first_date = queue[0][0]
        out.append(
            OpenPositionHolding(
                code=code,
                open_date=first_date.isoformat(),
                qty=sum(qty for _, qty in queue),
                holding_days=(end - first_date).days,
            )
        )
    return out


def _percentile(sorted_vals: list[float], p: float) -> float:
    """线性插值分位数（与 numpy 默认 linear 方法一致），输入须已升序。"""
    k = (len(sorted_vals) - 1) * p
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


# ---------------------------------------------------------------------------
# PeriodicSchedule：再平衡日判断（策略侧纯函数工具）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PeriodicSchedule:
    """再平衡日历：由交易日序列预计算再平衡日集合，查询 O(1)。"""

    _days: frozenset[date]

    @classmethod
    def from_trading_days(
        cls,
        trading_days: list[date],
        *,
        mode: str = "monthly",
        n_days: int | None = None,
    ) -> PeriodicSchedule:
        """按模式预计算再平衡日。

        Args:
            trading_days: 交易日序列（任意顺序，自动去重排序）。
            mode: "monthly"（每月末）/ "quarterly"（每季度末）/
                "every_n_days"（每 N 个交易日）。
            n_days: every_n_days 模式的 N（≥1）；其余模式忽略。

        Raises:
            ValueError: mode 未知或 every_n_days 缺 N / N < 1。
        """
        days = sorted(set(trading_days))
        if mode == "monthly":
            marks = _period_ends(days, key=lambda d: (d.year, d.month))
        elif mode == "quarterly":
            marks = _period_ends(days, key=lambda d: (d.year, (d.month - 1) // 3))
        elif mode == "every_n_days":
            if n_days is None or n_days < 1:
                raise ValueError("every_n_days 模式需要 n_days >= 1")
            marks = {days[i] for i in range(n_days - 1, len(days), n_days)}
        else:
            raise ValueError(f"未知再平衡模式: {mode}")
        return cls(frozenset(marks))

    def is_rebalance_day(self, d: date) -> bool:
        """当日是否再平衡日。"""
        return d in self._days


def _period_ends(days: list[date], key: Callable[[date], tuple[int, int]]) -> set[date]:
    """按 key 分组（输入已排序），取每组最后一个交易日。"""
    out: set[date] = set()
    for _, group in itertools.groupby(days, key):
        *_, last = group
        out.add(last)
    return out
