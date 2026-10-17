"""波段初筛信号回测（P4.5.1，零 LLM，不落库）。

定位：验证「初筛策略 + 参数组合」在历史区间上的信号质量，产出 CSV + 控制台汇总，
不写任何库表（不污染 swing_signal 实盘建议层）。

双口径表现：
1. 固定持有 N 日收益（hold_windows，默认 5/10/20 交易日）：信号日 T 收盘进场，
   T+N 收盘出场，数据不足 N 日记 None 并在统计时剔除；
2. 触位出场：回测零 LLM 无参考位，用 ATR 推算 stop/target 替代——
   stop = entry - stop_atr_mult x ATR(atr_window)，target = entry + target_atr_mult x ATR；
   信号日后逐根 K 线检查，同日触及两侧保守按止损先走；max_holding_days 内均未触及
   则末日收盘出场（timeout）。

参数网格：expand_grid 做笛卡尔积展开，每个组合独立重放 + 评估 + 汇总。
as-of 逐日重放经 screen_candidates(as_of_date=T)，前视安全由 SQL 日期截断保证。
"""

from __future__ import annotations

import csv
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from itertools import product
from pathlib import Path
from statistics import mean, median
from typing import Any

import duckdb

from viqtrader.agents.swing.screen import ScreenBar, ScreenCandidate, screen_candidates
from viqtrader.config.loader import Config

__all__ = [
    "DEFAULT_HOLD_WINDOWS",
    "BacktestResult",
    "ComboResult",
    "ComboSummary",
    "PatternStats",
    "SignalOutcome",
    "TouchExitRule",
    "WindowStats",
    "compute_atr",
    "evaluate_signals",
    "expand_grid",
    "replay_signals",
    "run_backtest",
    "summarize_outcomes",
    "trading_days_between",
    "write_csvs",
]

DEFAULT_HOLD_WINDOWS = (5, 10, 20)


@dataclass(frozen=True, slots=True)
class TouchExitRule:
    """触位出场规则（ATR 推算 stop/target 替代 LLM 参考位）。"""

    atr_window: int = 14
    stop_atr_mult: float = 2.0
    target_atr_mult: float = 3.0
    max_holding_days: int = 20


_DEFAULT_RULE = TouchExitRule()


@dataclass(frozen=True, slots=True)
class SignalOutcome:
    """单条信号的双口径回测结果（entry = 信号日收盘价）。"""

    signal_date: date
    code: str
    name: str
    market: str
    strategy_id: str
    patterns: tuple[str, ...]
    score: int
    entry: float
    horizon_returns: tuple[float | None, ...]  # 与 hold_windows 一一对齐，数据不足为 None
    touch_exit_date: date | None
    touch_exit_price: float | None
    touch_reason: str | None  # stop / target / timeout；ATR 不可用为 None
    touch_return: float | None
    touch_days: int | None  # 进场到出场的交易日数


@dataclass(frozen=True, slots=True)
class WindowStats:
    window: int
    samples: int  # 有效样本数（数据不足的信号已剔除）
    win_rate: float | None
    mean_return: float | None
    median_return: float | None


@dataclass(frozen=True, slots=True)
class PatternStats:
    pattern: str
    count: int  # 含该形态的信号数（多形态信号分别计入）
    touch_win_rate: float | None
    touch_mean: float | None


@dataclass(frozen=True, slots=True)
class ComboSummary:
    """单参数组合汇总。"""

    params: dict[str, Any]  # 该组合的参数覆盖（空 dict = 纯 YAML 基线）
    signal_count: int
    window_stats: tuple[WindowStats, ...]
    touch_win_rate: float | None
    touch_mean: float | None
    touch_median: float | None
    touch_avg_days: float | None
    reason_counts: dict[str, int]  # stop/target/timeout 各自笔数
    pattern_breakdown: tuple[PatternStats, ...]


@dataclass(frozen=True, slots=True)
class ComboResult:
    params: dict[str, Any]
    outcomes: tuple[SignalOutcome, ...]
    summary: ComboSummary


@dataclass(frozen=True, slots=True)
class BacktestResult:
    strategy_id: str
    start: date
    end: date
    hold_windows: tuple[int, ...]
    rule: TouchExitRule
    combos: tuple[ComboResult, ...]


def trading_days_between(conn: duckdb.DuckDBPyConnection, start: date, end: date) -> list[date]:
    """[start, end] 内库中实际存在日 K 的交易日（升序）。"""
    rows = conn.execute(
        "SELECT DISTINCT trade_date FROM fact_daily_bar "
        "WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date",
        [start, end],
    ).fetchall()
    return [r[0] for r in rows]


def replay_signals(
    conn: duckdb.DuckDBPyConnection,
    config: Config,
    *,
    strategy_id: str | None,
    params_override: Mapping[str, Any] | None,
    days: Sequence[date],
    top_n: int | None = None,
) -> list[ScreenCandidate]:
    """逐交易日 as-of 重放初筛（零 LLM），返回全部信号候选（按日期+排序）。"""
    signals: list[ScreenCandidate] = []
    for day in days:
        signals.extend(
            screen_candidates(
                conn,
                config,
                strategy_id=strategy_id,
                params_override=params_override,
                as_of_date=day,
                top_n=top_n,
            )
        )
    return signals


def compute_atr(bars: Sequence[ScreenBar], window: int) -> float | None:
    """以末尾 window 根 K 线的真实波幅均值估算 ATR；样本不足返回 None。"""
    if len(bars) < window + 1:
        return None
    trs: list[float] = []
    for i in range(len(bars) - window, len(bars)):
        cur, prev = bars[i], bars[i - 1]
        trs.append(max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close)))
    return mean(trs)


def evaluate_signals(
    conn: duckdb.DuckDBPyConnection,
    candidates: Sequence[ScreenCandidate],
    hold_windows: tuple[int, ...] = DEFAULT_HOLD_WINDOWS,
    rule: TouchExitRule = _DEFAULT_RULE,
) -> list[SignalOutcome]:
    """对每条信号取信号日后的前向 K 线，计算双口径结果。"""
    need = max(max(hold_windows), rule.max_holding_days)
    outcomes: list[SignalOutcome] = []
    for c in candidates:
        rows = conn.execute(
            """
            SELECT trade_date, open, high, low, close
            FROM fact_daily_bar
            WHERE code = ? AND trade_date > ? AND close IS NOT NULL
            ORDER BY trade_date
            LIMIT ?
            """,
            [c.code, c.signal_date, need],
        ).fetchall()
        forward = [
            ScreenBar(
                trade_date=td,
                open=float(op),
                high=float(hi),
                low=float(lo),
                close=float(cl),
                volume=0.0,
                amount=0.0,
            )
            for td, op, hi, lo, cl in rows
        ]
        entry = float(c.features["close"])

        horizon_returns: list[float | None] = [
            round(forward[n - 1].close / entry - 1, 6) if len(forward) >= n else None
            for n in hold_windows
        ]

        atr = compute_atr(c.bars, rule.atr_window)
        touch_exit_date: date | None = None
        touch_exit_price: float | None = None
        touch_reason: str | None = None
        touch_return: float | None = None
        touch_days: int | None = None
        if atr is not None and atr > 0 and forward:
            stop = entry - rule.stop_atr_mult * atr
            target = entry + rule.target_atr_mult * atr
            exit_price = forward[-1].close
            exit_idx = len(forward) - 1
            reason = "timeout"
            for i, bar in enumerate(forward[: rule.max_holding_days]):
                hit_stop = bar.low <= stop
                hit_target = bar.high >= target
                if hit_stop:  # 同日触及两侧保守按止损先走
                    exit_price, exit_idx, reason = stop, i, "stop"
                    break
                if hit_target:
                    exit_price, exit_idx, reason = target, i, "target"
                    break
            else:
                if len(forward) > rule.max_holding_days:
                    exit_idx = rule.max_holding_days - 1
                    exit_price = forward[exit_idx].close
            touch_exit_date = forward[exit_idx].trade_date
            touch_exit_price = round(exit_price, 4)
            touch_reason = reason
            touch_return = round(exit_price / entry - 1, 6)
            touch_days = exit_idx + 1

        outcomes.append(
            SignalOutcome(
                signal_date=c.signal_date,
                code=c.code,
                name=c.name,
                market=c.market,
                strategy_id=c.strategy_id,
                patterns=c.patterns,
                score=c.score,
                entry=entry,
                horizon_returns=tuple(horizon_returns),
                touch_exit_date=touch_exit_date,
                touch_exit_price=touch_exit_price,
                touch_reason=touch_reason,
                touch_return=touch_return,
                touch_days=touch_days,
            )
        )
    return outcomes


def _win_rate(returns: list[float]) -> float:
    return sum(1 for r in returns if r > 0) / len(returns)


def summarize_outcomes(
    outcomes: Sequence[SignalOutcome],
    hold_windows: tuple[int, ...] = DEFAULT_HOLD_WINDOWS,
    params: Mapping[str, Any] | None = None,
) -> ComboSummary:
    """对单组合信号结果做汇总（胜率=收益>0 占比，None 样本剔除）。"""
    window_stats: list[WindowStats] = []
    for idx, n in enumerate(hold_windows):
        rets = [ret for o in outcomes if (ret := o.horizon_returns[idx]) is not None]
        window_stats.append(
            WindowStats(
                window=n,
                samples=len(rets),
                win_rate=round(_win_rate(rets), 4) if rets else None,
                mean_return=round(mean(rets), 6) if rets else None,
                median_return=round(median(rets), 6) if rets else None,
            )
        )

    touch = [o for o in outcomes if o.touch_return is not None]
    touch_rets = [o.touch_return for o in touch if o.touch_return is not None]
    reason_counts: dict[str, int] = {}
    for o in touch:
        if o.touch_reason is not None:
            reason_counts[o.touch_reason] = reason_counts.get(o.touch_reason, 0) + 1

    by_pattern: dict[str, list[SignalOutcome]] = {}
    for o in outcomes:
        for p in o.patterns:
            by_pattern.setdefault(p, []).append(o)
    pattern_breakdown = tuple(
        PatternStats(
            pattern=p,
            count=len(group),
            touch_win_rate=(
                round(_win_rate([g.touch_return for g in group if g.touch_return is not None]), 4)
                if any(g.touch_return is not None for g in group)
                else None
            ),
            touch_mean=(
                round(mean([g.touch_return for g in group if g.touch_return is not None]), 6)
                if any(g.touch_return is not None for g in group)
                else None
            ),
        )
        for p, group in sorted(by_pattern.items())
    )

    return ComboSummary(
        params=dict(params or {}),
        signal_count=len(outcomes),
        window_stats=tuple(window_stats),
        touch_win_rate=round(_win_rate(touch_rets), 4) if touch_rets else None,
        touch_mean=round(mean(touch_rets), 6) if touch_rets else None,
        touch_median=round(median(touch_rets), 6) if touch_rets else None,
        touch_avg_days=(
            round(mean([o.touch_days for o in touch if o.touch_days is not None]), 2)
            if any(o.touch_days is not None for o in touch)
            else None
        ),
        reason_counts=reason_counts,
        pattern_breakdown=pattern_breakdown,
    )


def expand_grid(grid: Mapping[str, list[str]]) -> list[dict[str, str]]:
    """参数网格笛卡尔积展开；空网格返回单元素空组合（纯基线）。"""
    if not grid:
        return [{}]
    keys = list(grid)
    return [dict(zip(keys, values, strict=True)) for values in product(*grid.values())]


def run_backtest(
    conn: duckdb.DuckDBPyConnection,
    config: Config,
    *,
    strategy_id: str | None,
    param_combos: Sequence[Mapping[str, Any]],
    start: date,
    end: date,
    hold_windows: tuple[int, ...] = DEFAULT_HOLD_WINDOWS,
    rule: TouchExitRule = _DEFAULT_RULE,
    top_n: int | None = None,
) -> BacktestResult:
    """逐参数组合：as-of 重放 → 双口径评估 → 汇总。"""
    days = trading_days_between(conn, start, end)
    sid = strategy_id or str(config.get("swing_track.active_strategy", "trend_momentum"))
    combos: list[ComboResult] = []
    for params in param_combos:
        candidates = replay_signals(
            conn,
            config,
            strategy_id=sid,
            params_override=params,
            days=days,
            top_n=top_n,
        )
        outcomes = evaluate_signals(conn, candidates, hold_windows, rule)
        combos.append(
            ComboResult(
                params=dict(params),
                outcomes=tuple(outcomes),
                summary=summarize_outcomes(outcomes, hold_windows, params),
            )
        )
    return BacktestResult(
        strategy_id=sid,
        start=start,
        end=end,
        hold_windows=hold_windows,
        rule=rule,
        combos=tuple(combos),
    )


def _pct(value: float | None) -> str:
    return "" if value is None else f"{value:.4f}"


def write_csvs(result: BacktestResult, out_dir: Path) -> tuple[Path, Path]:
    """写明细与汇总两份 CSV，返回 (detail_path, summary_path)。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = f"{result.strategy_id}_{result.start}_{result.end}"
    detail_path = out_dir / f"swing_backtest_detail_{tag}.csv"
    summary_path = out_dir / f"swing_backtest_summary_{tag}.csv"

    with detail_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "combo_params",
                "signal_date",
                "code",
                "name",
                "market",
                "patterns",
                "score",
                "entry",
                *[f"ret_{n}d" for n in result.hold_windows],
                "touch_exit_date",
                "touch_exit_price",
                "touch_reason",
                "touch_return",
                "touch_days",
            ]
        )
        for combo in result.combos:
            combo_tag = json.dumps(combo.params, ensure_ascii=False) if combo.params else "baseline"
            for o in combo.outcomes:
                writer.writerow(
                    [
                        combo_tag,
                        o.signal_date,
                        o.code,
                        o.name,
                        o.market,
                        "|".join(o.patterns),
                        o.score,
                        o.entry,
                        *[_pct(r) for r in o.horizon_returns],
                        o.touch_exit_date or "",
                        o.touch_exit_price or "",
                        o.touch_reason or "",
                        _pct(o.touch_return),
                        o.touch_days or "",
                    ]
                )

    with summary_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "combo_params",
                "signal_count",
                *[
                    f"ret{n}_{m}"
                    for n in result.hold_windows
                    for m in ("samples", "win", "mean", "median")
                ],
                "touch_win",
                "touch_mean",
                "touch_median",
                "touch_avg_days",
                "stop_count",
                "target_count",
                "timeout_count",
                "pattern_breakdown",
            ]
        )
        for combo in result.combos:
            s = combo.summary
            combo_tag = json.dumps(s.params, ensure_ascii=False) if s.params else "baseline"
            cells: list[str] = []
            for w in s.window_stats:
                cells.extend(
                    [
                        str(w.samples),
                        _pct(w.win_rate),
                        _pct(w.mean_return),
                        _pct(w.median_return),
                    ]
                )
            writer.writerow(
                [
                    combo_tag,
                    s.signal_count,
                    *cells,
                    _pct(s.touch_win_rate),
                    _pct(s.touch_mean),
                    _pct(s.touch_median),
                    s.touch_avg_days or "",
                    s.reason_counts.get("stop", 0),
                    s.reason_counts.get("target", 0),
                    s.reason_counts.get("timeout", 0),
                    json.dumps(
                        [
                            {
                                "pattern": p.pattern,
                                "count": p.count,
                                "touch_win_rate": p.touch_win_rate,
                                "touch_mean": p.touch_mean,
                            }
                            for p in s.pattern_breakdown
                        ],
                        ensure_ascii=False,
                    ),
                ]
            )
    return detail_path, summary_path
