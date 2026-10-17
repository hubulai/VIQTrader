"""P4.5.1 波段多策略架构测试：注册表 / 参数解析 / 策略化 screen 入口。

覆盖：
- list_strategies 展示契约完整性（P6 渲染用元数据）；
- get_strategy 未知 id 报错并列出可用项；
- resolve_params 三层合并优先级、未知键、越界、字符串数字强转；
- TrendMomentumStrategy 薄封装与 compute_features 等价；
- lower_band_pct 敏感性（0.4 命中 reversal、0.2 不命中）；
- screen_candidates 的 as_of_date 历史重放、params_override 生效、默认走 active_strategy。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from viqtrader.agents.swing.screen import screen_candidates
from viqtrader.agents.swing.strategies import (
    ScreenBar,
    ScreenStrategy,
    get_strategy,
    list_strategies,
    resolve_params,
)
from viqtrader.agents.swing.strategies.trend_momentum import (
    TrendMomentumStrategy,
    compute_features,
)

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


def _reversal_bars() -> list[ScreenBar]:
    """60 根：高位 20 跌至 15.5 走平，近 5 日小幅回升触发 MA5 上穿 MA20。

    收盘处于 60 日区间约 22.8% 处：lower_band_pct=0.4 命中 reversal，0.2 不命中；
    量比恒 1.0 不触发 breakout，收盘偏离 MA20 约 5.4% 不触发 pullback。
    """
    base = date(2024, 1, 1)
    closes = (
        [20.0] * 20
        + [20.0 - 0.3 * i for i in range(1, 16)]  # 19.7 → 15.5
        + [15.5] * 20
        + [15.7, 15.9, 16.1, 16.3, 16.5]
    )
    assert len(closes) == 60
    return [_bar(base + timedelta(days=i), c, 10_000_000) for i, c in enumerate(closes)]


# ---------------- 注册表与展示契约 ----------------


def test_list_strategies_exposes_rendering_contract() -> None:
    infos = list_strategies()
    assert [i.strategy_id for i in infos] == ["trend_momentum"]
    info = infos[0]
    assert info.display_name and info.description
    assert len(info.params_schema) == 7
    for spec in info.params_schema:
        assert spec.description  # 中文说明必填，P6 直接作为表单项 label
        assert spec.kind in (int, float)
        if spec.minimum is not None:
            assert spec.minimum <= spec.default
        if spec.maximum is not None:
            assert spec.default <= spec.maximum


def test_get_strategy_unknown_id_raises() -> None:
    with pytest.raises(ValueError, match="未知波段初筛策略"):
        get_strategy("不存在的策略")
    assert isinstance(get_strategy("trend_momentum"), ScreenStrategy)


# ---------------- resolve_params 三层合并 ----------------


def test_resolve_params_three_layer_merge() -> None:
    schema = TrendMomentumStrategy().params_schema()
    merged = resolve_params(schema, {"ma_short": 10}, {"ma_short": 15, "volume_ratio_min": 2.0})
    assert merged["ma_short"] == 15  # 调用覆盖 > YAML 节
    assert merged["volume_ratio_min"] == 2.0
    assert merged["ma_mid"] == 20  # 未触及的键回落 schema 默认值


def test_resolve_params_rejects_unknown_key() -> None:
    with pytest.raises(ValueError, match="未声明的策略参数"):
        resolve_params(TrendMomentumStrategy().params_schema(), {"typo_key": 1})


def test_resolve_params_coerces_string_numbers() -> None:
    merged = resolve_params(
        TrendMomentumStrategy().params_schema(),
        {"ma_short": "7", "pullback_band": "0.05"},
    )
    assert merged["ma_short"] == 7
    assert merged["pullback_band"] == 0.05


@pytest.mark.parametrize(
    "bad",
    [
        {"ma_short": 1},  # 低于下限 2
        {"volume_ratio_min": 99.0},  # 高于上限 10
        {"lower_band_pct": 0.05},  # 低于下限 0.1
    ],
)
def test_resolve_params_out_of_range_rejected(bad) -> None:
    with pytest.raises(ValueError):
        resolve_params(TrendMomentumStrategy().params_schema(), bad)


# ---------------- 策略等价性与参数敏感性 ----------------


def test_strategy_wrapper_matches_compute_features() -> None:
    bars = _reversal_bars()
    strategy = TrendMomentumStrategy()
    resolved = resolve_params(strategy.params_schema(), _PARAMS)
    assert strategy.compute(bars, resolved) == compute_features(bars, **_PARAMS)


def test_lower_band_pct_sensitivity() -> None:
    bars = _reversal_bars()
    hit = compute_features(bars, **_PARAMS)  # lower_band_pct=0.4
    assert hit is not None
    assert hit["patterns"] == ["reversal"]
    assert hit["score"] == 1

    miss = compute_features(bars, **{**_PARAMS, "lower_band_pct": 0.2})
    assert miss is not None
    assert miss["patterns"] == []
    assert miss["score"] == 0


# ---------------- screen 策略化入口：as-of / 覆盖 / 默认策略 ----------------


def _seed_series(
    conn,
    code: str,
    name: str,
    closes: list[float],
    volumes: list[float],
    *,
    market: str = "SH",
) -> None:
    conn.execute(
        "INSERT INTO dim_stock (code, market, name, industry, list_date, is_hk_connect) "
        "VALUES (?, ?, ?, NULL, '2000-01-01', false)",
        [code, market, name],
    )
    base = date(2024, 1, 1)
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


def test_screen_as_of_date_replays_history(conn, config) -> None:
    # A：第 59 根（2-29）放量突破，第 60 根（3-01）走平
    _seed_series(
        conn,
        "600001",
        "突破股份",
        [10.0] * 59 + [10.5, 10.5],
        [10_000_000] * 59 + [20_000_000, 10_000_000],
    )
    # B：突破只发生在最后一日（3-01）
    _seed_series(
        conn,
        "600002",
        "后起股份",
        [10.0] * 60 + [10.5],
        [10_000_000] * 60 + [20_000_000],
    )

    latest = screen_candidates(conn, config)
    assert [c.code for c in latest] == ["600002"]
    assert latest[0].signal_date == date(2024, 3, 1)

    replayed = screen_candidates(conn, config, as_of_date=date(2024, 2, 29))
    assert [c.code for c in replayed] == ["600001"]
    candidate = replayed[0]
    assert candidate.signal_date == date(2024, 2, 29)
    assert candidate.bars[-1].trade_date == date(2024, 2, 29)  # 仅用 ≤ as-of 的 bar
    assert candidate.features["close"] == 10.5


def test_screen_params_override_takes_effect(conn, config) -> None:
    _seed_series(
        conn,
        "600001",
        "突破股份",
        [10.0] * 59 + [10.5],
        [10_000_000] * 59 + [20_000_000],
    )
    _seed_series(
        conn,
        "000002",
        "回踩股份",
        [10 + i * 0.02 for i in range(60)],
        [10_000_000] * 60,
        market="SZ",
    )

    baseline = screen_candidates(conn, config)
    assert [c.code for c in baseline] == ["600001", "000002"]

    tightened = screen_candidates(conn, config, params_override={"volume_ratio_min": 10.0})
    assert [c.code for c in tightened] == ["000002"]  # 量比 2.0 够不到收紧后的门槛


def test_screen_defaults_to_active_strategy(conn, config) -> None:
    _seed_series(
        conn,
        "600001",
        "突破股份",
        [10.0] * 59 + [10.5],
        [10_000_000] * 59 + [20_000_000],
    )

    defaulted = screen_candidates(conn, config)
    assert defaulted[0].strategy_id == "trend_momentum"  # 读 swing_track.active_strategy

    explicit = screen_candidates(conn, config, strategy_id="trend_momentum")
    assert [c.code for c in explicit] == [c.code for c in defaulted]


def test_screen_unknown_strategy_raises(conn, config) -> None:
    with pytest.raises(ValueError, match="未知波段初筛策略"):
        screen_candidates(conn, config, strategy_id="ghost_strategy")
