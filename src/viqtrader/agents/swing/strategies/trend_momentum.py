"""趋势动量策略（trend_momentum）：P4.5 三形态并集初筛的策略化封装（P4.5.1）。

形态规则（全部阈值经 params_schema 外置，默认值即原 P4.5 基线）：
- breakout 放量突破：收盘 = breakout_window 内最高收盘 且 量比 ≥ volume_ratio_min（+3 分）
- pullback 回踩企稳：收盘距 MA20 ±pullback_band 内 且 MA20 较 5 日前上行（+2 分）
- reversal 底部金叉：近 5 日 MA5 上穿 MA20 且收盘处于 60 日区间下 lower_band_pct（+1 分）
- 趋势确认（加分项）：MA5 > MA20 > MA60 多头排列（+1 分，不单独成信号）

compute_features 保持零依赖纯函数，策略类仅为薄封装；与原 P4.5 逻辑逐行等价，
唯一变化是原硬编码 0.4 下区间比例外置为 lower_band_pct。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from viqtrader.agents.swing.strategies.base import ParamSpec, ScreenBar

__all__ = ["TrendMomentumStrategy", "compute_features"]

_SCHEMA = (
    ParamSpec("ma_short", int, 5, "短期均线周期（日）", minimum=2, maximum=60),
    ParamSpec("ma_mid", int, 20, "中期均线周期（日），回踩/金叉锚点", minimum=5, maximum=120),
    ParamSpec("ma_long", int, 60, "长期均线周期（日），区间与多头锚点", minimum=20, maximum=250),
    ParamSpec("breakout_window", int, 20, "突破窗口：收盘=窗口内最高收盘", minimum=5, maximum=120),
    ParamSpec(
        "volume_ratio_min",
        float,
        1.5,
        "放量门槛：当日成交量 / 近 5 日均量",
        minimum=1.0,
        maximum=10.0,
    ),
    ParamSpec(
        "pullback_band",
        float,
        0.03,
        "回踩容忍带：收盘距 MA_mid 相对偏差上限",
        minimum=0.005,
        maximum=0.2,
    ),
    ParamSpec(
        "lower_band_pct",
        float,
        0.4,
        "底部区间比例：收盘处于长窗口区间下 X% 判低位",
        minimum=0.1,
        maximum=0.9,
    ),
)


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
    lower_band_pct: float,
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
    in_lower_band = span > 0 and (last.close - window_low) / span <= lower_band_pct
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


class TrendMomentumStrategy:
    """三形态并集趋势动量初筛（P4.5 基线的策略化形态）。"""

    strategy_id = "trend_momentum"
    display_name = "趋势动量（三形态并集）"
    description = (
        "放量突破 / 回踩企稳 / 底部金叉三形态并集打分，多头排列加分；"
        "按分值 + 近 20 日日均成交额排序取 Top-N。"
    )

    def params_schema(self) -> tuple[ParamSpec, ...]:
        return _SCHEMA

    def compute(self, bars: list[ScreenBar], params: Mapping[str, Any]) -> dict[str, Any] | None:
        return compute_features(
            bars,
            ma_short=int(params["ma_short"]),
            ma_mid=int(params["ma_mid"]),
            ma_long=int(params["ma_long"]),
            breakout_window=int(params["breakout_window"]),
            volume_ratio_min=float(params["volume_ratio_min"]),
            pullback_band=float(params["pullback_band"]),
            lower_band_pct=float(params["lower_band_pct"]),
        )
