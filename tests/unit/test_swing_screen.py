"""P4.5 波段初筛纯函数与全市场 SQL 过滤测试（零 LLM）。

P4.5.1 起 compute_features 归属 trend_momentum 策略模块（逻辑逐行等价），
screen.py 仅保留全市场 SQL 初筛入口；本文件同时覆盖策略化后的等价性。
"""

from __future__ import annotations

from datetime import date, timedelta

from viqtrader.agents.swing.screen import ScreenBar, screen_candidates
from viqtrader.agents.swing.strategies.trend_momentum import compute_features

_PARAMS = dict(
    ma_short=5,
    ma_mid=20,
    ma_long=60,
    breakout_window=20,
    volume_ratio_min=1.5,
    pullback_band=0.03,
    lower_band_pct=0.4,
)


def _bar(day: date, close: float, volume: float) -> ScreenBar:
    return ScreenBar(
        trade_date=day,
        open=close,
        high=close + 0.05,
        low=close - 0.05,
        close=close,
        volume=volume,
        amount=close * volume,
    )


def _flat_breakout_bars() -> list[ScreenBar]:
    base = date(2024, 1, 1)
    bars = [_bar(base + timedelta(days=i), 10.0, 10_000_000) for i in range(59)]
    last = ScreenBar(
        trade_date=base + timedelta(days=59),
        open=10.1,
        high=10.5,
        low=10.0,
        close=10.5,
        volume=20_000_000,
        amount=10.5 * 20_000_000,
    )
    bars.append(last)
    return bars


def test_compute_features_flags_breakout() -> None:
    f = compute_features(_flat_breakout_bars(), **_PARAMS)
    assert f is not None
    assert "breakout" in f["patterns"]
    assert f["volume_ratio"] == 2.0
    assert f["bullish_alignment"] is True  # 放量长阳同步建立多头排列，加分项


def test_compute_features_flags_pullback_in_uptrend() -> None:
    base = date(2024, 1, 1)
    bars = [_bar(base + timedelta(days=i), 10 + i * 0.02, 10_000_000) for i in range(60)]
    f = compute_features(bars, **_PARAMS)
    assert f is not None
    assert "pullback" in f["patterns"]
    assert "breakout" not in f["patterns"]  # 无量比，收盘虽创新高也不算放量突破
    assert f["ma_mid_rising"] is True


def test_compute_features_flat_market_no_pattern() -> None:
    base = date(2024, 1, 1)
    bars = [_bar(base + timedelta(days=i), 10.0, 10_000_000) for i in range(60)]
    f = compute_features(bars, **_PARAMS)
    assert f is not None
    assert f["patterns"] == []
    assert f["bullish_alignment"] is False


def test_compute_features_insufficient_bars() -> None:
    base = date(2024, 1, 1)
    bars = [_bar(base + timedelta(days=i), 10.0, 10_000_000) for i in range(40)]
    assert compute_features(bars, **_PARAMS) is None


# ---------------- SQL 全市场初筛：过滤、排序、Top-N ----------------


def _seed_series(
    conn,
    code: str,
    name: str,
    closes: list[float],
    volumes: list[float],
    *,
    market: str = "SH",
    day_offset: int = 0,
) -> None:
    conn.execute(
        "INSERT INTO dim_stock (code, market, name, industry, list_date, is_hk_connect) "
        "VALUES (?, ?, ?, NULL, '2000-01-01', false)",
        [code, market, name],
    )
    base = date(2024, 1, 1) + timedelta(days=day_offset)
    for i, (close, volume) in enumerate(zip(closes, volumes, strict=True)):
        conn.execute(
            """
            INSERT INTO fact_daily_bar
                (code, trade_date, open, high, low, close, volume, amount, adj_factor)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1.0)
            """,
            [
                code,
                base + timedelta(days=i),
                close,
                close + 0.05,
                close - 0.05,
                close,
                volume,
                close * volume,
            ],
        )


def _breakout_closes() -> list[float]:
    return [10.0] * 59 + [10.5]


def _breakout_volumes() -> list[float]:
    return [10_000_000] * 59 + [20_000_000]


def _pullback_closes() -> list[float]:
    return [10 + i * 0.02 for i in range(60)]


def test_screen_filters_and_ranking(conn, config) -> None:
    # 有效：放量突破（SH）
    _seed_series(conn, "600001", "突破股份", _breakout_closes(), _breakout_volumes())
    # 有效：回踩企稳（SZ）
    _seed_series(conn, "000002", "回踩股份", _pullback_closes(), [10_000_000] * 60, market="SZ")
    # ST：同名形态也必须排除
    _seed_series(conn, "600003", "ST 退退", _breakout_closes(), _breakout_volumes())
    # 流动性不足：量缩 100 倍
    _seed_series(
        conn,
        "600004",
        "冷门股份",
        _breakout_closes(),
        [100_000] * 59 + [200_000],
    )
    # 停牌：60 根 bar 但最后交易日停在全局最新日的前一天
    _seed_series(
        conn,
        "600005",
        "停牌股份",
        _breakout_closes(),
        _breakout_volumes(),
        day_offset=-1,
    )
    # 次新：只有 40 根 bar
    _seed_series(
        conn,
        "600006",
        "次新股份",
        _breakout_closes()[-40:],
        _breakout_volumes()[-40:],
    )

    candidates = screen_candidates(conn, config)
    codes = [c.code for c in candidates]
    assert codes == ["600001", "000002"]  # 突破分值(3+趋势)高于回踩(2+趋势)
    assert candidates[0].patterns == ("breakout",) or "breakout" in candidates[0].patterns
    assert "pullback" in candidates[1].patterns
    assert candidates[0].market == "SH"
    assert candidates[1].market == "SZ"
    assert candidates[0].bars[-1].trade_date == date(2024, 2, 29)


def test_screen_respects_top_n(conn, config) -> None:
    _seed_series(conn, "600001", "突破股份", _breakout_closes(), _breakout_volumes())
    _seed_series(conn, "000002", "回踩股份", _pullback_closes(), [10_000_000] * 60, market="SZ")
    assert [c.code for c in screen_candidates(conn, config, top_n=1)] == ["600001"]


def test_screen_empty_database(conn, config) -> None:
    assert screen_candidates(conn, config) == []


def test_screen_defaults_when_config_section_empty(conn) -> None:
    """swing_track.agent 配置段为空时靠默认值兜底，不抛 KeyError（空库返回空）。"""

    class _StubConfig:
        def get(self, key: str, default=None):
            return {} if key == "swing_track.agent" else default

    assert screen_candidates(conn, _StubConfig()) == []  # type: ignore[arg-type]
