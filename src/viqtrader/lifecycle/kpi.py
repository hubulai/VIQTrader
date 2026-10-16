"""P3.6 生命周期 KPI 计算：纯函数，输入日净值序列输出 KPI 快照。

与 BacktestResult.metrics 同源口径（年化按 252 日、Sharpe 无风险利率 0），
差异在于生命周期判定需要"滚动窗口"口径（滚动 30 交易日 Sharpe、近 30 交易日回撤）
与"持续低迷天数"（自然日）等阶段化指标。

设计文档 5.3 判定用字段（全部 float，可 JSON 序列化）：
- total_return / max_drawdown / sharpe：进入当前阶段以来的全窗口指标（PAPER 晋级判定）
- rolling_30d_sharpe：最近 30 个交易日窗口的 Sharpe（试用→主力、主力保持、低 Sharpe 退役判定）
- rolling_30d_max_drawdown：最近 30 个交易日窗口内的最大回撤（主力保持判定）
- daily_return_latest：最后一个交易日的单日收益（单日亏损退役判定）
- consecutive_low_sharpe_calendar_days：截至 as_of，每日滚动 30 日 Sharpe 连续 < retire_sharpe
  的自然日天数（低 Sharpe 持续 14 天退役判定；样本不足 31 个交易日时恒 0）
"""

from __future__ import annotations

import math
import statistics
from datetime import date, timedelta
from typing import Any

ROLLING_WINDOW = 30  # 滚动 Sharpe 窗口（交易日），与设计 5.3 "30 日 Sharpe" 对齐


def daily_returns(series: list[tuple[date, float]]) -> list[tuple[date, float]]:
    """逐日收益率序列（首日无前值不产生样本）。"""
    out: list[tuple[date, float]] = []
    for i in range(1, len(series)):
        prev = series[i - 1][1]
        if prev > 0:
            out.append((series[i][0], series[i][1] / prev - 1.0))
    return out


def sharpe_of(returns: list[float]) -> float | None:
    """年化 Sharpe（252 交易日，无风险利率 0）；样本 <2 或零波动返回 None。"""
    if len(returns) < 2:
        return None
    sd = statistics.stdev(returns)
    if sd <= 0:
        return None
    return statistics.mean(returns) / sd * math.sqrt(252.0)


def max_drawdown_of(values: list[float]) -> float | None:
    """最大回撤（负值口径，如 -0.12）；样本 <2 返回 None。"""
    if len(values) < 2:
        return None
    peak = values[0]
    mdd = 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, v / peak - 1.0)
    return mdd


def rolling_sharpe(series: list[tuple[date, float]], window: int = ROLLING_WINDOW) -> float | None:
    """最近 window 个交易日的年化 Sharpe；样本不足返回 None。"""
    if len(series) <= window:
        return None
    tail = series[-(window + 1) :]
    rets = [r for _, r in daily_returns(tail)]
    return sharpe_of(rets)


def rolling_max_drawdown(
    series: list[tuple[date, float]], window: int = ROLLING_WINDOW
) -> float | None:
    """最近 window 个交易日窗口内的最大回撤；样本不足返回 None。"""
    if len(series) <= window:
        return None
    tail = [v for _, v in series[-(window + 1) :]]
    return max_drawdown_of(tail)


def consecutive_low_sharpe_calendar_days(
    series: list[tuple[date, float]], threshold: float, window: int = ROLLING_WINDOW
) -> int:
    """截至序列末日，每日滚动 Sharpe 连续低于 threshold 的自然日天数。

    判定日 d 的滚动 Sharpe 用 ≤ d 的最近 window+1 个净值点计算；
    自末日起向前逐自然日回扫，首次遇到"不满足低 Sharpe"的日期终止。
    样本不足（不足 window+1 个点）的日期视为不满足，恒计 0。
    """
    if len(series) <= window:
        return 0
    count = 0
    cursor = series[-1][0]
    while True:
        prefix = [(d, v) for d, v in series if d <= cursor]
        if len(prefix) <= window:
            break
        rs = rolling_sharpe(prefix, window)
        if rs is None or rs >= threshold:
            break
        count += 1
        cursor -= timedelta(days=1)
    return count


def compute_kpi_snapshot(
    series: list[tuple[date, float]],
    *,
    retire_sharpe: float,
) -> dict[str, Any]:
    """全量 KPI 快照（lifecycle 配置项 retire_sharpe 注入，其余口径固定）。

    series 为空或单点时全部指标为 None（trading_days 如实返回）。
    """
    n = len(series)
    snap: dict[str, Any] = {
        "trading_days": n,
        "total_return": None,
        "max_drawdown": None,
        "sharpe": None,
        "rolling_30d_sharpe": None,
        "rolling_30d_max_drawdown": None,
        "daily_return_latest": None,
        "consecutive_low_sharpe_calendar_days": 0,
    }
    if n < 2:
        return snap

    values = [v for _, v in series]
    first, last = values[0], values[-1]
    snap["total_return"] = last / first - 1.0 if first > 0 else 0.0
    snap["max_drawdown"] = max_drawdown_of(values)
    snap["sharpe"] = sharpe_of([r for _, r in daily_returns(series)])
    snap["rolling_30d_sharpe"] = rolling_sharpe(series)
    snap["rolling_30d_max_drawdown"] = rolling_max_drawdown(series)

    rets = daily_returns(series)
    if rets:
        snap["daily_return_latest"] = rets[-1][1]
    snap["consecutive_low_sharpe_calendar_days"] = consecutive_low_sharpe_calendar_days(
        series, retire_sharpe
    )
    return snap


__all__ = [
    "ROLLING_WINDOW",
    "compute_kpi_snapshot",
    "consecutive_low_sharpe_calendar_days",
    "daily_returns",
    "max_drawdown_of",
    "rolling_max_drawdown",
    "rolling_sharpe",
    "sharpe_of",
]
