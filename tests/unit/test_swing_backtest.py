"""P4.5.1 波段信号回测测试（全内存库，零 LLM，不落库）。

覆盖：交易日区间过滤、ATR 手算对拍、双口径评估（target/stop/timeout/同日止损优先/
样本不足）、汇总统计（胜率/None 剔除/形态分组/出场原因计数）、参数网格展开、
CSV 冒烟，以及 run_backtest 单日端到端编排。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from viqtrader.agents.swing.backtest import (
    BacktestResult,
    ComboResult,
    SignalOutcome,
    TouchExitRule,
    compute_atr,
    evaluate_signals,
    expand_grid,
    run_backtest,
    summarize_outcomes,
    trading_days_between,
    write_csvs,
)
from viqtrader.agents.swing.screen import ScreenBar, ScreenCandidate

_SIGNAL_DATE = date(2024, 3, 1)


def _history_bars(n: int = 20, end: date = _SIGNAL_DATE) -> tuple[ScreenBar, ...]:
    """n 根横盘 K 线（high=11/low=9/close=10），每根真实波幅恒为 2.0。"""
    base = end - timedelta(days=n - 1)
    return tuple(
        ScreenBar(
            trade_date=base + timedelta(days=i),
            open=10.0,
            high=11.0,
            low=9.0,
            close=10.0,
            volume=1.0,
            amount=10.0,
        )
        for i in range(n)
    )


def _make_candidate(
    code: str = "600001",
    signal_date: date = _SIGNAL_DATE,
    entry: float = 10.0,
    patterns: tuple[str, ...] = ("breakout",),
    history: tuple[ScreenBar, ...] | None = None,
) -> ScreenCandidate:
    return ScreenCandidate(
        code=code,
        market="SH",
        name="测试股份",
        signal_date=signal_date,
        strategy_id="trend_momentum",
        score=3,
        patterns=patterns,
        features={"close": entry},
        bars=history if history is not None else _history_bars(end=signal_date),
    )


def _seed_forward(
    conn, code: str, bars: list[tuple[float, float, float, float]], signal_date: date = _SIGNAL_DATE
) -> None:
    """从 signal_date 次日起连续写入前向 K 线（open, high, low, close）。"""
    for i, (op, hi, lo, cl) in enumerate(bars, start=1):
        conn.execute(
            """
            INSERT INTO fact_daily_bar
                (code, trade_date, open, high, low, close, volume, amount, adj_factor)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1.0)
            """,
            [code, signal_date + timedelta(days=i), op, hi, lo, cl, 1_000_000, cl * 1_000_000],
        )


# ATR=2.0、entry=10 → stop=6.0 / target=16.0（默认规则 2x/3x ATR14）
_RISING_FORWARD = [  # 第 5 根 high 17.5 触及 target
    (10.2, 10.8, 9.8, 10.5),
    (10.6, 11.3, 10.3, 11.0),
    (11.1, 12.3, 10.9, 12.0),
    (12.1, 13.3, 11.9, 13.0),
    (13.1, 17.5, 12.9, 17.0),
]


# ---------------- 交易日与 ATR ----------------


def test_trading_days_between_filters_range(conn) -> None:
    _seed_forward(conn, "600001", [(10.0, 10.5, 9.5, 10.0)] * 5, date(2024, 1, 1))
    days = trading_days_between(conn, date(2024, 1, 3), date(2024, 1, 5))
    assert days == [date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 5)]
    assert trading_days_between(conn, date(2024, 2, 1), date(2024, 2, 5)) == []


def test_compute_atr_insufficient_samples() -> None:
    assert compute_atr(_history_bars(14), 14) is None  # 需 window+1 根


def test_compute_atr_constant_true_range() -> None:
    assert compute_atr(_history_bars(20), 14) == 2.0


def test_compute_atr_uses_prev_close_on_gap() -> None:
    bars = list(_history_bars(15))
    last = bars[-1]
    bars[-1] = ScreenBar(
        trade_date=last.trade_date,
        open=13.0,
        high=13.5,
        low=12.5,
        close=13.0,
        volume=1.0,
        amount=13.0,
    )
    # 末根 TR = max(1.0, |13.5-10|, |12.5-10|) = 3.5，其余 13 根 TR = 2.0
    assert compute_atr(bars, 14) == pytest.approx((13 * 2.0 + 3.5) / 14)


# ---------------- evaluate_signals 双口径 ----------------


def test_evaluate_target_hit(conn) -> None:
    _seed_forward(conn, "600001", _RISING_FORWARD)
    (outcome,) = evaluate_signals(conn, [_make_candidate()], (5,))
    assert outcome.horizon_returns == (0.7,)  # 17.0 / 10.0 - 1
    assert outcome.touch_reason == "target"
    assert outcome.touch_exit_price == 16.0
    assert outcome.touch_return == 0.6
    assert outcome.touch_days == 5
    assert outcome.touch_exit_date == _SIGNAL_DATE + timedelta(days=5)


def test_evaluate_stop_hit(conn) -> None:
    _seed_forward(conn, "600001", [(9.5, 9.8, 5.5, 7.0)])  # 首日 low 5.5 击穿 stop
    (outcome,) = evaluate_signals(conn, [_make_candidate()], (5,))
    assert outcome.horizon_returns == (None,)  # 前向数据不足 5 日
    assert outcome.touch_reason == "stop"
    assert outcome.touch_return == -0.4
    assert outcome.touch_days == 1


def test_evaluate_stop_priority_when_both_touched_same_day(conn) -> None:
    _seed_forward(conn, "600001", [(10.0, 17.0, 5.0, 12.0)])  # high 穿 target 且 low 穿 stop
    (outcome,) = evaluate_signals(conn, [_make_candidate()], (5,))
    assert outcome.touch_reason == "stop"  # 保守按止损先走
    assert outcome.touch_exit_price == 6.0
    assert outcome.touch_days == 1


def test_evaluate_timeout_when_neither_touched(conn) -> None:
    _seed_forward(conn, "600001", [(10.0, 10.5, 9.5, 10.0)] * 25)  # 25 根横盘
    (outcome,) = evaluate_signals(conn, [_make_candidate()], (5, 10, 20))
    assert outcome.horizon_returns == (0.0, 0.0, 0.0)
    assert outcome.touch_reason == "timeout"
    assert outcome.touch_days == 20  # max_holding_days 截断，而非第 25 根
    assert outcome.touch_exit_price == 10.0


def test_evaluate_touch_none_when_atr_insufficient(conn) -> None:
    _seed_forward(conn, "600001", _RISING_FORWARD)
    candidate = _make_candidate(history=_history_bars(10, end=_SIGNAL_DATE))  # < 15 根
    (outcome,) = evaluate_signals(conn, [candidate], (5,))
    assert outcome.horizon_returns == (0.7,)  # 固定窗口口径不受影响
    assert outcome.touch_reason is None
    assert outcome.touch_return is None
    assert outcome.touch_days is None


# ---------------- summarize_outcomes 汇总 ----------------


def _outcome(
    rets: tuple[float | None, ...],
    *,
    touch_return: float | None = None,
    touch_reason: str | None = None,
    touch_days: int | None = None,
    patterns: tuple[str, ...] = ("breakout",),
) -> SignalOutcome:
    return SignalOutcome(
        signal_date=_SIGNAL_DATE,
        code="600001",
        name="测试股份",
        market="SH",
        strategy_id="trend_momentum",
        patterns=patterns,
        score=3,
        entry=10.0,
        horizon_returns=rets,
        touch_exit_date=None,
        touch_exit_price=None,
        touch_reason=touch_reason,
        touch_return=touch_return,
        touch_days=touch_days,
    )


def test_summarize_outcomes_stats_and_breakdown() -> None:
    outcomes = [
        _outcome((0.1, None), touch_return=0.5, touch_reason="target", touch_days=3),
        _outcome((-0.05, 0.02), touch_return=-0.4, touch_reason="stop", touch_days=1),
        _outcome(
            (0.2, 0.03),
            touch_return=0.0,
            touch_reason="timeout",
            touch_days=20,
            patterns=("breakout", "pullback"),
        ),
    ]
    s = summarize_outcomes(outcomes, (5, 10), {"volume_ratio_min": 2.0})
    assert s.params == {"volume_ratio_min": 2.0}
    assert s.signal_count == 3

    w5, w10 = s.window_stats
    assert (w5.window, w5.samples) == (5, 3)
    assert w5.win_rate == 0.6667
    assert w5.mean_return == 0.083333
    assert w5.median_return == 0.1
    assert (w10.window, w10.samples) == (10, 2)  # None 样本已剔除
    assert w10.win_rate == 1.0
    assert w10.mean_return == 0.025

    assert s.touch_win_rate == 0.3333
    assert s.touch_mean == 0.033333
    assert s.touch_median == 0.0
    assert s.touch_avg_days == 8.0
    assert s.reason_counts == {"target": 1, "stop": 1, "timeout": 1}

    by_pattern = {p.pattern: p for p in s.pattern_breakdown}
    assert by_pattern["breakout"].count == 3
    assert by_pattern["breakout"].touch_win_rate == 0.3333
    assert by_pattern["pullback"].count == 1
    assert by_pattern["pullback"].touch_win_rate == 0.0


def test_summarize_outcomes_empty() -> None:
    s = summarize_outcomes([], (5, 10))
    assert s.signal_count == 0
    assert all(w.samples == 0 and w.win_rate is None for w in s.window_stats)
    assert s.touch_win_rate is None
    assert s.touch_avg_days is None
    assert s.reason_counts == {}
    assert s.pattern_breakdown == ()


# ---------------- 参数网格与 CSV ----------------


def test_expand_grid_empty_yields_baseline() -> None:
    assert expand_grid({}) == [{}]


def test_expand_grid_cartesian_product() -> None:
    combos = expand_grid({"a": ["1", "2"], "b": ["x", "y", "z"]})
    assert combos == [
        {"a": "1", "b": "x"},
        {"a": "1", "b": "y"},
        {"a": "1", "b": "z"},
        {"a": "2", "b": "x"},
        {"a": "2", "b": "y"},
        {"a": "2", "b": "z"},
    ]


def test_write_csvs_smoke(tmp_path) -> None:
    outcome = _outcome((0.1, None), touch_return=0.5, touch_reason="target", touch_days=3)
    combo = ComboResult(
        params={},
        outcomes=(outcome,),
        summary=summarize_outcomes([outcome], (5, 10)),
    )
    result = BacktestResult(
        strategy_id="trend_momentum",
        start=_SIGNAL_DATE,
        end=_SIGNAL_DATE,
        hold_windows=(5, 10),
        rule=TouchExitRule(),
        combos=(combo,),
    )
    detail_path, summary_path = write_csvs(result, tmp_path)
    assert detail_path.exists() and summary_path.exists()

    detail_lines = detail_path.read_text(encoding="utf-8-sig").splitlines()
    assert "ret_5d" in detail_lines[0] and "touch_reason" in detail_lines[0]
    assert "baseline" in detail_lines[1]  # 空参数组合标记为基线

    summary_lines = summary_path.read_text(encoding="utf-8-sig").splitlines()
    assert "combo_params" in summary_lines[0]
    assert "ret5_win" in summary_lines[0] and "ret10_median" in summary_lines[0]


# ---------------- run_backtest 端到端（单日） ----------------


def test_run_backtest_single_day_end_to_end(conn, config) -> None:
    conn.execute(
        "INSERT INTO dim_stock (code, market, name, industry, list_date, is_hk_connect) "
        "VALUES (?, ?, ?, NULL, '2000-01-01', false)",
        ["600001", "SH", "突破股份"],
    )
    base = date(2024, 1, 1)
    for i in range(60):  # 60 根历史：末日（2-29）放量突破
        close = 10.5 if i == 59 else 10.0
        volume = 20_000_000 if i == 59 else 10_000_000
        conn.execute(
            """
            INSERT INTO fact_daily_bar
                (code, trade_date, open, high, low, close, volume, amount, adj_factor)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1.0)
            """,
            [
                "600001",
                base + timedelta(days=i),
                close,
                close + 0.05,
                close - 0.05,
                close,
                volume,
                close * volume,
            ],
        )
    _seed_forward(conn, "600001", [(10.5, 10.6, 10.4, 10.5)] * 25, date(2024, 2, 29))

    result = run_backtest(
        conn,
        config,
        strategy_id=None,
        param_combos=[{}],
        start=date(2024, 2, 29),
        end=date(2024, 2, 29),
    )
    assert result.strategy_id == "trend_momentum"
    assert len(result.combos) == 1

    combo = result.combos[0]
    assert combo.params == {}
    assert combo.summary.signal_count == 1
    (outcome,) = combo.outcomes
    assert outcome.code == "600001"
    assert outcome.signal_date == date(2024, 2, 29)
    assert outcome.patterns == ("breakout",)
    assert outcome.entry == 10.5
    assert outcome.horizon_returns == (0.0, 0.0, 0.0)
    assert outcome.touch_reason == "timeout"
    assert outcome.touch_days == 20
