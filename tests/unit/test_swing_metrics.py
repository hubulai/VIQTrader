"""P3.4 波段轨回测指标单测：FIFO round-trip 手工核对。

覆盖：单笔胜负平保、加仓后部分平仓 FIFO 拆分（pnl/持有天数/超期）、
盈亏比/利润因子/期望/成本收益率、无样本全 None、年换手率三态、
卖超持仓 ValueError、多标的独立批次、乱序确定性、Decimal 成交（TradeLog 同构）。
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from viqtrader.engine.backtest.swing import compute_swing_metrics
from viqtrader.engine.types import TradeLog

D0 = date(2023, 1, 2)


def _t(d: date, code: str, side: str, qty: int, amount_cny: float) -> SimpleNamespace:
    """与 SwingTradeLike 同构的合成成交（amount_cny BUY 负 / SELL 正，含费）。"""
    return SimpleNamespace(trade_date=d, code=code, side=side, qty=qty, amount_cny=amount_cny)


# ---------------------------------------------------------------------------
# 单笔 round-trip
# ---------------------------------------------------------------------------


def test_single_win_roundtrip() -> None:
    """100 股成本 10000，10 日后 11000 平仓 → pnl=1000，收益率 10%，全胜。"""
    trades = [
        _t(D0, "600000", "BUY", 100, -10000.0),
        _t(D0 + timedelta(days=10), "600000", "SELL", 100, 11000.0),
    ]
    m = compute_swing_metrics(trades, max_holding_days=10)
    assert m.roundtrip_count == 1
    assert m.win_count == 1 and m.loss_count == 0 and m.breakeven_count == 0
    assert m.win_rate == 1.0 and m.loss_rate == 0.0
    assert m.avg_win_cny == 1000.0
    assert m.avg_loss_cny is None
    assert m.payoff_ratio is None  # 无亏损单：不可计算，不用 inf
    assert m.profit_factor is None
    assert m.expectancy_cny == 1000.0
    assert m.avg_roundtrip_return == pytest.approx(0.10)
    assert m.open_lot_count == 0
    assert (m.holding.mean_days, m.holding.median_days) == (10.0, 10.0)
    assert (m.holding.min_days, m.holding.max_days) == (10.0, 10.0)
    assert m.holding.breach_count == 0  # 恰好 10 天：严格大于才计超期


def test_breakeven_counts_as_non_win() -> None:
    """平保 1 笔：计入分母作非胜，win_rate=0；期望/收益率为 0。"""
    trades = [
        _t(D0, "600000", "BUY", 100, -10000.0),
        _t(D0 + timedelta(days=1), "600000", "SELL", 100, 10000.0),
    ]
    m = compute_swing_metrics(trades)
    assert m.roundtrip_count == 1
    assert m.win_count == 0 and m.loss_count == 0 and m.breakeven_count == 1
    assert m.win_rate == 0.0 and m.loss_rate == 0.0
    assert m.avg_win_cny is None and m.avg_loss_cny is None
    assert m.payoff_ratio is None and m.profit_factor is None
    assert m.expectancy_cny == 0.0
    assert m.avg_roundtrip_return == 0.0


# ---------------------------------------------------------------------------
# 加仓 + 部分平仓：FIFO 拆分
# ---------------------------------------------------------------------------


def test_fifo_partial_close() -> None:
    """两批买入 100@100 / 100@120，卖 150@110 → 拆分 +1000(20天) / -500(15天)。"""
    trades = [
        _t(D0, "600000", "BUY", 100, -10000.0),
        _t(D0 + timedelta(days=5), "600000", "BUY", 100, -12000.0),
        _t(D0 + timedelta(days=20), "600000", "SELL", 150, 16500.0),
    ]
    m = compute_swing_metrics(trades, max_holding_days=15)
    assert m.roundtrip_count == 2
    assert m.win_count == 1 and m.loss_count == 1 and m.breakeven_count == 0
    assert m.win_rate == 0.5 and m.loss_rate == 0.5
    assert m.avg_win_cny == 1000.0
    assert m.avg_loss_cny == 500.0  # 正数（绝对值口径）
    assert m.payoff_ratio == 2.0
    assert m.profit_factor == 2.0
    assert m.expectancy_cny == 250.0
    # (0.10 + -500/6000) / 2 = 0.008333...
    assert m.avg_roundtrip_return == pytest.approx((0.10 - 500.0 / 6000.0) / 2)
    assert m.open_lot_count == 1  # 第二批剩 50 股，1 个批次
    assert m.holding.mean_days == 17.5
    assert m.holding.median_days == 17.5
    assert m.holding.min_days == 15.0 and m.holding.max_days == 20.0
    assert m.holding.breach_count == 1  # 20 天超期，15 天不超（严格大于）

    # 同数据不配置持有期上限：breach_count=None 表示未统计（区别于 0）
    m2 = compute_swing_metrics(trades)
    assert m2.holding.breach_count is None


# ---------------------------------------------------------------------------
# 无样本 / 边界
# ---------------------------------------------------------------------------


def test_only_buys_all_none() -> None:
    """只有买入无平仓：统计类全 None，批次照计 2。"""
    trades = [
        _t(D0, "600000", "BUY", 100, -10000.0),
        _t(D0 + timedelta(days=2), "600000", "BUY", 50, -6000.0),
    ]
    m = compute_swing_metrics(trades, max_holding_days=20)
    assert m.roundtrip_count == 0
    assert m.win_rate is None and m.loss_rate is None
    assert m.avg_win_cny is None and m.payoff_ratio is None
    assert m.expectancy_cny is None and m.avg_roundtrip_return is None
    assert m.annual_turnover is None
    assert m.open_lot_count == 2
    assert m.holding.mean_days is None and m.holding.breach_count is None


def test_sell_exceeds_holding_raises() -> None:
    """SELL 超过 FIFO 持仓（缺建仓起点/数据损坏）必须 fail-fast，不静默配对。"""
    trades = [
        _t(D0, "600000", "BUY", 100, -10000.0),
        _t(D0 + timedelta(days=1), "600000", "SELL", 200, 22000.0),
    ]
    with pytest.raises(ValueError, match="FIFO"):
        compute_swing_metrics(trades)


# ---------------------------------------------------------------------------
# 年换手率
# ---------------------------------------------------------------------------


def test_annual_turnover() -> None:
    """3 个交易日均净值 100000，卖出 1000 → 1000/100000 x 252/3 = 0.84。"""
    d1, d2 = D0 + timedelta(days=1), D0 + timedelta(days=2)
    trades = [
        _t(D0, "600000", "BUY", 100, -5000.0),
        _t(d1, "600000", "SELL", 20, 1000.0),
    ]
    series = [(D0, 100000.0), (d1, 100000.0), (d2, 100000.0)]
    m = compute_swing_metrics(trades, equity_series=series)
    assert m.annual_turnover == pytest.approx(0.84)


def test_annual_turnover_none_states() -> None:
    """未注入净值 → None；均资产为 0 → None（不用默认值掩盖）。"""
    trades = [
        _t(D0, "600000", "BUY", 100, -10000.0),
        _t(D0 + timedelta(days=1), "600000", "SELL", 100, 10000.0),
    ]
    assert compute_swing_metrics(trades).annual_turnover is None
    d1 = D0 + timedelta(days=1)
    m = compute_swing_metrics(trades, equity_series=[(D0, 0.0), (d1, 0.0)])
    assert m.annual_turnover is None


# ---------------------------------------------------------------------------
# 多标的 / 乱序 / Decimal 兼容
# ---------------------------------------------------------------------------


def test_multi_code_independent_lots() -> None:
    """两只标的各自配对：+100(3天) / -200(5天)，持有期 mean=median=4。"""
    trades = [
        _t(D0, "600000", "BUY", 100, -10000.0),
        _t(D0, "000001", "BUY", 200, -20000.0),
        _t(D0 + timedelta(days=3), "600000", "SELL", 100, 10100.0),
        _t(D0 + timedelta(days=5), "000001", "SELL", 200, 19800.0),
    ]
    m = compute_swing_metrics(trades)
    assert m.roundtrip_count == 2
    assert m.win_count == 1 and m.loss_count == 1
    assert m.payoff_ratio == 0.5  # 100 / 200
    assert m.holding.mean_days == 4.0
    assert m.holding.median_days == 4.0
    assert m.holding.min_days == 3.0 and m.holding.max_days == 5.0
    assert m.open_lot_count == 0


def test_unsorted_input_deterministic() -> None:
    """乱序成交与正序结果一致（内部按日期稳定排序）。"""
    forward = [
        _t(D0, "600000", "BUY", 100, -10000.0),
        _t(D0 + timedelta(days=10), "600000", "SELL", 100, 11000.0),
    ]
    m_forward = compute_swing_metrics(forward)
    m_reverse = compute_swing_metrics(list(reversed(forward)))
    assert m_reverse == m_forward


def test_decimal_tradelog_compatible() -> None:
    """真实 TradeLog（amount_cny 为 Decimal）走同一口径：-10000/+10500 → +500。"""

    def _fill(side: str, qty: int, amount: str, d: date) -> TradeLog:
        return TradeLog(
            trade_id=f"t-{side}",
            account_id="acc_swing",
            mode="swing",
            code="600000",
            side=side,
            qty=qty,
            price=Decimal("100"),
            commission=Decimal("0"),
            stamp_tax=Decimal("0"),
            transfer_fee=Decimal("0"),
            fx_rate=Decimal("1"),
            amount_cny=Decimal(amount),
            trade_date=d,
            intent_id=None,
        )

    trades = [
        _fill("BUY", 100, "-10000", D0),
        _fill("SELL", 100, "10500", D0 + timedelta(days=7)),
    ]
    m = compute_swing_metrics(trades)
    assert m.roundtrip_count == 1
    assert m.win_count == 1
    assert m.avg_win_cny == pytest.approx(500.0)
    assert m.avg_roundtrip_return == pytest.approx(0.05)
