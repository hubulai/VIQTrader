"""P3.3 价值轨回测指标单测：合成确定性序列 + 手工核对。

覆盖：XIRR 已知解 / 无变号判 None、算术超额（含区间外点位过滤）、
年换手率、FIFO round-trip 持有期分布（含分位数插值与未平仓浮动天数）、
PeriodicSchedule 三模式边界与参数校验。
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from viqtrader.engine.backtest.result import BacktestResult, DailyEquity
from viqtrader.engine.backtest.value import (
    PeriodicSchedule,
    compute_value_metrics,
)
from viqtrader.engine.types import TradeLog

ACC = "acc_value"
D0 = date(2023, 1, 2)  # 周一；2023 非闰年，D0 → D0+365 恰好 1.0 年


# ---------------------------------------------------------------------------
# 构造辅助
# ---------------------------------------------------------------------------


def _trade(tid: str, code: str, side: str, qty: int, amount: float, d: date) -> TradeLog:
    """零费用合成成交：amount_cny 现金流水口径（BUY 负 / SELL 正）。"""
    return TradeLog(
        trade_id=tid,
        account_id=ACC,
        mode="value",
        code=code,
        side=side,
        qty=qty,
        price=Decimal("10"),
        commission=Decimal("0"),
        stamp_tax=Decimal("0"),
        transfer_fee=Decimal("0"),
        fx_rate=Decimal("1"),
        amount_cny=Decimal(str(amount)),
        trade_date=d,
        intent_id=None,
    )


def _equity(d: date, total: float) -> DailyEquity:
    return DailyEquity(
        trade_date=d,
        account_id=ACC,
        cash=Decimal(str(total)),
        market_value=Decimal("0"),
        total_value=Decimal(str(total)),
    )


def _result(
    start: date,
    end: date,
    equity: list[DailyEquity],
    trades: list[TradeLog],
) -> BacktestResult:
    return BacktestResult(start=start, end=end, accounts=[ACC], equity_curve=equity, trades=trades)


def _business_days(start: date, end: date) -> list[date]:
    """周一~周五近似交易日序列（测试用，不追求真实日历）。"""
    days: list[date] = []
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            days.append(cur)
        cur += timedelta(days=1)
    return days


# ---------------------------------------------------------------------------
# XIRR
# ---------------------------------------------------------------------------


def test_xirr_exact_known_case() -> None:
    """-10000 @T + 11000 @T+365d → XIRR 恰为 10%（2023 非闰年 365 天）。"""
    end = date(2024, 1, 2)
    assert (end - D0).days == 365  # 手算前提：恰好 1.0 年
    res = _result(
        D0,
        end,
        equity=[_equity(D0, 10000), _equity(end, 11000)],
        trades=[_trade("t1", "600000", "BUY", 1000, -10000, D0)],
    )
    m = compute_value_metrics(res, ACC)
    assert m.xirr is not None
    assert m.xirr == pytest.approx(0.10, abs=1e-6)


def test_xirr_none_without_sign_change() -> None:
    """无成交时只有终值正流入，现金流未变号 → None。"""
    end = date(2024, 1, 2)
    res = _result(D0, end, equity=[_equity(D0, 10000), _equity(end, 11000)], trades=[])
    m = compute_value_metrics(res, ACC)
    assert m.xirr is None


def test_xirr_none_without_series() -> None:
    """无净值曲线（无终值）且只有买入流出 → None。"""
    res = _result(
        D0,
        date(2024, 1, 2),
        equity=[],
        trades=[_trade("t1", "600000", "BUY", 1000, -10000, D0)],
    )
    m = compute_value_metrics(res, ACC)
    assert m.xirr is None


# ---------------------------------------------------------------------------
# 相对超额（算术口径）
# ---------------------------------------------------------------------------


def test_excess_arithmetic_and_range_filter() -> None:
    """组合 100000→110000 = 10%；基准区间内 4000→4200 = 5%；超额 = 5%。

    区间外的基准点位（起点前 9999）必须被过滤，否则手算口径对不上。
    """
    end = date(2024, 1, 2)
    res = _result(
        D0,
        end,
        equity=[_equity(D0, 100000), _equity(end, 110000)],
        trades=[],
    )
    bench = [
        (D0 - timedelta(days=10), 9999.0),  # 区间外，应被忽略
        (D0, 4000.0),
        (end, 4200.0),
    ]
    m = compute_value_metrics(res, ACC, benchmark_series=bench)
    assert m.generic_metrics["total_return"] == pytest.approx(0.10)
    assert m.benchmark_total_return == pytest.approx(0.05)
    assert m.excess_return_arithmetic == pytest.approx(0.05)


def test_excess_none_without_benchmark() -> None:
    end = date(2024, 1, 2)
    res = _result(D0, end, equity=[_equity(D0, 100000), _equity(end, 110000)], trades=[])
    m = compute_value_metrics(res, ACC)
    assert m.benchmark_total_return is None
    assert m.excess_return_arithmetic is None


# ---------------------------------------------------------------------------
# 年换手率
# ---------------------------------------------------------------------------


def test_annual_turnover() -> None:
    """卖出 1000 / 均资产 100000 x (252/3 个交易日) = 0.84。"""
    d1, d2 = D0 + timedelta(days=1), D0 + timedelta(days=2)
    res = _result(
        D0,
        d2,
        equity=[_equity(D0, 100000), _equity(d1, 100000), _equity(d2, 100000)],
        trades=[
            _trade("t1", "600000", "BUY", 100, -5000, D0),
            _trade("t2", "600000", "SELL", 100, 1000, d1),
        ],
    )
    m = compute_value_metrics(res, ACC)
    assert m.annual_turnover == pytest.approx(0.84)


def test_annual_turnover_none_without_series() -> None:
    res = _result(D0, date(2024, 1, 2), equity=[], trades=[])
    m = compute_value_metrics(res, ACC)
    assert m.annual_turnover is None


# ---------------------------------------------------------------------------
# FIFO 持有期分布
# ---------------------------------------------------------------------------


def test_fifo_holding_distribution() -> None:
    """BUY100@T、BUY100@T+10、SELL150@T+40 → 平仓 [40, 30] 天，余 50 股浮动。

    分布（升序 [30, 40]，线性插值）：count=2 mean=35 median=35
    p25=32.5 p75=37.5 min=30 max=40；未平仓 open_date=T+10 qty=50 浮动 30 天。
    """
    d_buy1, d_buy2, d_sell = D0, D0 + timedelta(days=10), D0 + timedelta(days=40)
    res = _result(
        D0,
        d_sell,
        equity=[_equity(D0, 100000), _equity(d_sell, 100000)],
        trades=[
            _trade("t1", "600000", "BUY", 100, -1000, d_buy1),
            _trade("t2", "600000", "BUY", 100, -1000, d_buy2),
            _trade("t3", "600000", "SELL", 150, 1500, d_sell),
        ],
    )
    m = compute_value_metrics(res, ACC)
    hp = m.holding_period
    assert hp.count == 2
    assert hp.mean_days == pytest.approx(35.0)
    assert hp.median_days == pytest.approx(35.0)
    assert hp.p25_days == pytest.approx(32.5)
    assert hp.p75_days == pytest.approx(37.5)
    assert hp.min_days == pytest.approx(30.0)
    assert hp.max_days == pytest.approx(40.0)

    assert len(m.open_positions) == 1
    op = m.open_positions[0]
    assert op.code == "600000"
    assert op.open_date == d_buy2.isoformat()
    assert op.qty == 50
    assert op.holding_days == 30


def test_holding_empty_trades() -> None:
    end = date(2024, 1, 2)
    res = _result(D0, end, equity=[_equity(D0, 100000), _equity(end, 100000)], trades=[])
    m = compute_value_metrics(res, ACC)
    assert m.holding_period.count == 0
    assert m.holding_period.mean_days is None
    assert m.open_positions == []


# ---------------------------------------------------------------------------
# PeriodicSchedule
# ---------------------------------------------------------------------------


def test_schedule_monthly() -> None:
    """2024 年 Q1 工作日序列：月末再平衡日 = 01-31 / 02-29 / 03-29。"""
    days = _business_days(date(2024, 1, 1), date(2024, 3, 31))
    sch = PeriodicSchedule.from_trading_days(days, mode="monthly")
    assert sch._days == {date(2024, 1, 31), date(2024, 2, 29), date(2024, 3, 29)}
    assert sch.is_rebalance_day(date(2024, 2, 29))
    assert not sch.is_rebalance_day(date(2024, 2, 28))


def test_schedule_quarterly() -> None:
    """2024 上半年：季度末再平衡日 = 03-29 / 06-28（6-30 为周日）。"""
    days = _business_days(date(2024, 1, 1), date(2024, 6, 30))
    sch = PeriodicSchedule.from_trading_days(days, mode="quarterly")
    assert sch._days == {date(2024, 3, 29), date(2024, 6, 28)}


def test_schedule_every_n_days() -> None:
    """10 个工作日每 3 日再平衡 → 第 3/6/9 个交易日（0-based 索引 2/5/8）。"""
    days = _business_days(date(2024, 1, 1), date(2024, 1, 14))
    assert len(days) == 10
    sch = PeriodicSchedule.from_trading_days(days, mode="every_n_days", n_days=3)
    assert sch._days == {days[2], days[5], days[8]}
    assert not sch.is_rebalance_day(days[0])


def test_schedule_invalid_params() -> None:
    days = _business_days(date(2024, 1, 1), date(2024, 1, 31))
    with pytest.raises(ValueError, match="n_days"):
        PeriodicSchedule.from_trading_days(days, mode="every_n_days")
    with pytest.raises(ValueError, match="未知再平衡模式"):
        PeriodicSchedule.from_trading_days(days, mode="weekly")
