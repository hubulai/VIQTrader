"""波段轨确定性初筛（P4.5 零 LLM；P4.5.1 多策略化）。

全市场代码初筛 → 策略形态打分 → Top-N 候选，LLM 只解读少量候选。
- 策略经 strategies 注册表解析（strategy_id 缺省读 swing_track.active_strategy），
  形态规则与阈值由策略 params_schema 声明，YAML 策略节 + 调用覆盖三层合并；
- 通用闸门（与策略无关）留在 swing_track.agent：markets / screen_top_n /
  min_listing_bars / min_avg_amount_20d / lookback_bars；
- 硬过滤：ST（name 含 ST）、停牌（as-of 交易日无 bar）、次新（bar 不足）、
  流动性不足（近 20 日日均成交额 < min_avg_amount_20d）；
- as_of_date 支持历史重放（SQL 按该日期截断取数），为回测逐日重放的入口。

特征计算纯函数零 DB 依赖（strategies 包内），SQL 只负责取数。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

import duckdb

from viqtrader.agents.swing.strategies.base import ScreenBar, resolve_params
from viqtrader.agents.swing.strategies.registry import DEFAULT_STRATEGY_ID, get_strategy
from viqtrader.config.loader import Config

__all__ = ["ScreenBar", "ScreenCandidate", "screen_candidates"]


@dataclass(frozen=True, slots=True)
class ScreenCandidate:
    """通过初筛的候选：特征快照 + 供 prompt 使用的近期 K 线。"""

    code: str
    market: str
    name: str
    signal_date: date
    strategy_id: str
    score: int
    patterns: tuple[str, ...]
    features: dict[str, Any]
    bars: tuple[ScreenBar, ...]


def _market_placeholder(markets: list[str]) -> str:
    return ",".join("?" for _ in markets)


def screen_candidates(
    conn: duckdb.DuckDBPyConnection,
    config: Config,
    *,
    strategy_id: str | None = None,
    params_override: Mapping[str, Any] | None = None,
    as_of_date: date | None = None,
    top_n: int | None = None,
) -> list[ScreenCandidate]:
    """全市场初筛：取 as-of 日前 lookback_bars 个交易日，过滤打分后返回 Top-N。

    strategy_id 缺省读配置 swing_track.active_strategy；params_override 覆盖
    YAML 策略节（回测网格/CLI 用）；as_of_date 缺省为库中最新交易日。
    """
    agent = config.get("swing_track.agent", {}) or {}
    markets = list(agent.get("markets", ["SH", "SZ"]))
    lookback = int(agent.get("lookback_bars", 60))
    min_bars = int(agent.get("min_listing_bars", 60))
    min_amount = float(agent.get("min_avg_amount_20d", 1e8))
    top = int(top_n if top_n is not None else agent.get("screen_top_n", 5))

    sid = strategy_id or str(config.get("swing_track.active_strategy", DEFAULT_STRATEGY_ID))
    strategy = get_strategy(sid)
    params = resolve_params(
        strategy.params_schema(),
        config.get(f"swing_track.strategies.{sid}", {}) or {},
        params_override,
    )

    if as_of_date is None:
        latest_row = conn.execute("SELECT max(trade_date) FROM fact_daily_bar").fetchone()
        if latest_row is None or latest_row[0] is None:
            return []
        signal_date = latest_row[0]
    else:
        signal_date = as_of_date

    placeholders = _market_placeholder(markets)
    rows = conn.execute(
        f"""
        WITH ranked AS (
            SELECT f.code, f.trade_date, f.open, f.high, f.low, f.close,
                   f.volume, f.amount,
                   ROW_NUMBER() OVER (PARTITION BY f.code ORDER BY f.trade_date DESC) AS rn
            FROM fact_daily_bar f
            WHERE f.trade_date <= ?
              AND f.code IN (SELECT code FROM dim_stock WHERE market IN ({placeholders}))
        )
        SELECT s.code, s.market, s.name, r.trade_date, r.open, r.high, r.low,
               r.close, r.volume, r.amount, r.rn
        FROM ranked r
        JOIN dim_stock s ON s.code = r.code
        WHERE r.rn <= ? AND UPPER(s.name) NOT LIKE '%ST%'
        ORDER BY r.code, r.trade_date
        """,
        [signal_date, *markets, max(lookback, min_bars)],
    ).fetchall()

    by_code: dict[str, tuple[str, str, list[ScreenBar]]] = {}
    for code, market, name, td, op, hi, lo, cl, vol, amt, _rn in rows:
        entry = by_code.get(code)
        if entry is None:
            entry = (market, name, [])
            by_code[code] = entry
        if cl is not None:
            entry[2].append(
                ScreenBar(
                    trade_date=td,
                    open=float(op),
                    high=float(hi),
                    low=float(lo),
                    close=float(cl),
                    volume=float(vol or 0),
                    amount=float(amt or 0),
                )
            )

    candidates: list[ScreenCandidate] = []
    for code, (market, name, bars) in by_code.items():
        bars.sort(key=lambda b: b.trade_date)
        if len(bars) < min_bars or bars[-1].trade_date != signal_date:
            continue  # 次新 / 停牌（as-of 交易日无 bar）
        features = strategy.compute(bars, params)
        if features is None:
            continue
        if features["avg_amount_20d"] < min_amount:
            continue
        patterns = tuple(features["patterns"])
        if not patterns:
            continue
        candidates.append(
            ScreenCandidate(
                code=code,
                market=market,
                name=name,
                signal_date=signal_date,
                strategy_id=sid,
                score=int(features["score"]),
                patterns=patterns,
                features=features,
                bars=tuple(bars),
            )
        )

    candidates.sort(key=lambda c: (c.score, c.features["avg_amount_20d"]), reverse=True)
    return candidates[:top]
