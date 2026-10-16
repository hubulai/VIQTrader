"""波段轨确定性初筛（P4.5，零 LLM）。

全市场代码初筛 → 三形态并集打分 → Top-N 候选，LLM 只解读少量候选。
形态规则（参数全部外置 swing_track.agent）：
- breakout 放量突破：收盘 = breakout_window 内最高收盘 且 量比 ≥ volume_ratio_min
- pullback 回踩企稳：收盘距 MA20 ±pullback_band 内 且 MA20 较 5 日前上行
- reversal 底部金叉：近 5 日 MA5 上穿 MA20 且收盘处于 60 日区间下 40%
- 趋势确认（加分项）：MA5 > MA20 > MA60 多头排列
硬过滤：ST（name 含 ST）、停牌（最近交易日无 bar）、次新（bar 不足）、
流动性不足（近 20 日日均成交额 < min_avg_amount_20d）。

特征计算纯函数零 DB 依赖，直接可单测；SQL 只负责取数。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import duckdb

from viqtrader.config.loader import Config

__all__ = ["ScreenBar", "ScreenCandidate", "compute_features", "screen_candidates"]


@dataclass(frozen=True, slots=True)
class ScreenBar:
    """初筛用日线条目（日期升序）。"""

    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float


@dataclass(frozen=True, slots=True)
class ScreenCandidate:
    """通过初筛的候选：特征快照 + 供 prompt 使用的近期 K 线。"""

    code: str
    market: str
    name: str
    signal_date: date
    score: int
    patterns: tuple[str, ...]
    features: dict[str, Any]
    bars: tuple[ScreenBar, ...]


def _ma(closes: list[float], end: int, window: int) -> float | None:
    """closes[end-window:end] 简单均值；样本不足返回 None。end 为开区间右端。"""
    if end < window:
        return None
    seg = closes[end - window : end]
    return sum(seg) / window


def compute_features(
    bars: list[ScreenBar],
    *,
    ma_short: int,
    ma_mid: int,
    ma_long: int,
    breakout_window: int,
    volume_ratio_min: float,
    pullback_band: float,
) -> dict[str, Any] | None:
    """对单只标的的升序 bars 计算形态特征；样本不足返回 None。"""
    n = len(bars)
    need = max(ma_long, breakout_window + 1, 20)
    if n < need:
        return None

    closes = [b.close for b in bars]
    volumes = [b.volume for b in bars]
    last = bars[-1]

    ma_s = _ma(closes, n, ma_short)
    ma_m = _ma(closes, n, ma_mid)
    ma_l = _ma(closes, n, ma_long)
    ma_m_prev = _ma(closes, n - 5, ma_mid)
    if ma_s is None or ma_m is None or ma_l is None:
        return None

    window_closes = closes[-(breakout_window + 1) : -1]  # 不含当日
    prior_high = max(window_closes)
    prev5_vol = volumes[-6:-1]
    avg_vol5 = sum(prev5_vol) / len(prev5_vol) if prev5_vol else 0.0
    volume_ratio = last.volume / avg_vol5 if avg_vol5 > 0 else None

    breakout = last.close >= prior_high and (
        volume_ratio is not None and volume_ratio >= volume_ratio_min
    )

    distance = abs(last.close - ma_m) / ma_m if ma_m else None
    ma_mid_rising = ma_m_prev is not None and ma_m > ma_m_prev
    pullback = distance is not None and distance <= pullback_band and ma_mid_rising

    window_low = min(b.low for b in bars[-ma_long:])
    window_high = max(b.high for b in bars[-ma_long:])
    span = window_high - window_low
    in_lower_band = span > 0 and (last.close - window_low) / span <= 0.4
    # 近 5 日内发生 MA_short 上穿 MA_mid（前一日在下、当日在上）
    golden_cross = False
    for end in range(n - 4, n + 1):
        s_now, m_now = _ma(closes, end, ma_short), _ma(closes, end, ma_mid)
        s_prev, m_prev = _ma(closes, end - 1, ma_short), _ma(closes, end - 1, ma_mid)
        if (
            s_now is not None
            and m_now is not None
            and s_prev is not None
            and m_prev is not None
            and s_prev <= m_prev
            and s_now > m_now
        ):
            golden_cross = True
            break
    reversal = golden_cross and in_lower_band

    bullish = ma_s > ma_m > ma_l

    patterns: list[str] = []
    score = 0
    if breakout:
        patterns.append("breakout")
        score += 3
    if pullback:
        patterns.append("pullback")
        score += 2
    if reversal:
        patterns.append("reversal")
        score += 1
    if bullish:
        score += 1  # 趋势确认加分（不单独成信号）

    avg_amount_20d = sum(b.amount for b in bars[-20:]) / min(20, n)
    return {
        "close": last.close,
        f"ma{ma_short}": round(ma_s, 4),
        f"ma{ma_mid}": round(ma_m, 4),
        f"ma{ma_long}": round(ma_l, 4),
        "prior_high": round(prior_high, 4),
        "volume_ratio": round(volume_ratio, 2) if volume_ratio is not None else None,
        "distance_to_ma_mid_pct": round(distance, 4) if distance is not None else None,
        "ma_mid_rising": ma_mid_rising,
        "bullish_alignment": bullish,
        "in_lower_60d_band": in_lower_band,
        "avg_amount_20d": round(avg_amount_20d, 2),
        "patterns": patterns,
        "score": score,
    }


def _market_placeholder(markets: list[str]) -> str:
    return ",".join("?" for _ in markets)


def screen_candidates(
    conn: duckdb.DuckDBPyConnection,
    config: Config,
    *,
    top_n: int | None = None,
) -> list[ScreenCandidate]:
    """全市场初筛：取最近 lookback_bars 个交易日，过滤打分后返回 Top-N（分值降序）。"""
    agent = config.get("swing_track.agent", {}) or {}
    markets = list(agent.get("markets", ["SH", "SZ"]))
    lookback = int(agent.get("lookback_bars", 60))
    min_bars = int(agent.get("min_listing_bars", 60))
    min_amount = float(agent.get("min_avg_amount_20d", 1e8))
    top = int(top_n if top_n is not None else agent.get("screen_top_n", 5))

    ma_short = int(agent.get("ma_short", 5))
    ma_mid = int(agent.get("ma_mid", 20))
    ma_long = int(agent.get("ma_long", 60))
    breakout_window = int(agent.get("breakout_window", 20))
    volume_ratio_min = float(agent.get("volume_ratio_min", 1.5))
    pullback_band = float(agent.get("pullback_band", 0.03))

    latest_row = conn.execute("SELECT max(trade_date) FROM fact_daily_bar").fetchone()
    if latest_row is None or latest_row[0] is None:
        return []
    latest_date = latest_row[0]

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
        [latest_date, *markets, max(lookback, min_bars)],
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
        if len(bars) < min_bars or bars[-1].trade_date != latest_date:
            continue  # 次新 / 停牌（最近交易日无 bar）
        features = compute_features(
            bars,
            ma_short=ma_short,
            ma_mid=ma_mid,
            ma_long=ma_long,
            breakout_window=breakout_window,
            volume_ratio_min=volume_ratio_min,
            pullback_band=pullback_band,
        )
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
                signal_date=latest_date,
                score=int(features["score"]),
                patterns=patterns,
                features=features,
                bars=tuple(bars),
            )
        )

    candidates.sort(key=lambda c: (c.score, c.features["avg_amount_20d"]), reverse=True)
    return candidates[:top]
