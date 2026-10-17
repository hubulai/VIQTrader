"""P5.3 学习地基单测：打标纯函数 / 打标配装 / 评分卡聚合。"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from viqtrader.learning.labeler import (
    label_swing_signals,
    label_valuation_sessions,
)
from viqtrader.learning.labels import (
    BarRow,
    t_n_label,
    touch_label,
    value_eval_label,
)
from viqtrader.learning.scorecard import (
    LabelFact,
    aggregate_swing,
    aggregate_valuation,
    write_scorecards,
)
from viqtrader.store.db import connect_memory
from viqtrader.store.migrate import migrate

# ---------------------------------------------------------------------------
# 纯函数：t_n_label
# ---------------------------------------------------------------------------


def _bars(start: date, closes: list[float]) -> list[BarRow]:
    """构造连续日线：close 按列表，high/low 取 close±1。"""
    return [
        BarRow(
            trade_date=start + timedelta(days=i),
            high=c + 1.0,
            low=c - 1.0,
            close=c,
        )
        for i, c in enumerate(closes)
    ]


def test_t_n_label_hit_and_return() -> None:
    bars = _bars(date(2026, 1, 2), [101.0, 102.0, 103.0, 104.0, 110.0])
    lbl = t_n_label(
        decision_type="swing_pick",
        decision_ref="s1",
        code="600000",
        decision_date=date(2026, 1, 1),
        horizon_days=5,
        base_close=100.0,
        bars_after=bars,
    )
    assert lbl is not None
    assert lbl.horizon == "T+5"
    assert lbl.label_date == date(2026, 1, 6)
    assert lbl.return_pct == pytest.approx(0.10)
    assert lbl.hit is True
    assert lbl.detail["base_close"] == 100.0
    assert lbl.detail["label_close"] == 110.0


def test_t_n_label_not_mature_returns_none() -> None:
    bars = _bars(date(2026, 1, 2), [101.0, 102.0])
    lbl = t_n_label(
        decision_type="swing_pick",
        decision_ref="s1",
        code="600000",
        decision_date=date(2026, 1, 1),
        horizon_days=5,
        base_close=100.0,
        bars_after=bars,
    )
    assert lbl is None


def test_t_n_label_negative_return_not_hit() -> None:
    bars = _bars(date(2026, 1, 2), [90.0])
    lbl = t_n_label(
        decision_type="swing_pick",
        decision_ref="s1",
        code="600000",
        decision_date=date(2026, 1, 1),
        horizon_days=1,
        base_close=100.0,
        bars_after=bars,
    )
    assert lbl is not None and lbl.hit is False
    assert lbl.return_pct == pytest.approx(-0.10)


def test_t_n_label_invalid_args() -> None:
    with pytest.raises(ValueError, match="horizon_days"):
        t_n_label(
            decision_type="x",
            decision_ref="s",
            code="c",
            decision_date=date(2026, 1, 1),
            horizon_days=0,
            base_close=100.0,
            bars_after=[],
        )
    with pytest.raises(ValueError, match="base_close"):
        t_n_label(
            decision_type="x",
            decision_ref="s",
            code="c",
            decision_date=date(2026, 1, 1),
            horizon_days=1,
            base_close=0.0,
            bars_after=[],
        )


# ---------------------------------------------------------------------------
# 纯函数：touch_label
# ---------------------------------------------------------------------------

_TOUCH_KW = dict(
    decision_type="swing_pick",
    decision_ref="s1",
    code="600000",
    decision_date=date(2026, 1, 1),
    entry_ref=100.0,
    stop_ref=95.0,
    target_ref=110.0,
)


def test_touch_label_target_first() -> None:
    bars = [
        BarRow(date(2026, 1, 2), high=105.0, low=99.0, close=103.0),
        BarRow(date(2026, 1, 3), high=111.0, low=104.0, close=109.0),
    ]
    lbl = touch_label(bars_after=bars, **_TOUCH_KW)
    assert lbl is not None
    assert lbl.hit is True
    assert lbl.label_date == date(2026, 1, 3)
    assert lbl.return_pct == pytest.approx(0.10)  # 按 target_ref 估算
    assert lbl.detail["side"] == "target"
    assert lbl.detail["days_to_touch"] == 2


def test_touch_label_stop_first() -> None:
    bars = [
        BarRow(date(2026, 1, 2), high=104.0, low=94.5, close=96.0),
    ]
    lbl = touch_label(bars_after=bars, **_TOUCH_KW)
    assert lbl is not None
    assert lbl.hit is False
    assert lbl.return_pct == pytest.approx(-0.05)  # 按 stop_ref 估算
    assert lbl.detail["side"] == "stop"


def test_touch_label_same_day_both_conservative_stop() -> None:
    """同日触两侧：保守按 stop 先。"""
    bars = [BarRow(date(2026, 1, 2), high=115.0, low=90.0, close=100.0)]
    lbl = touch_label(bars_after=bars, **_TOUCH_KW)
    assert lbl is not None
    assert lbl.hit is False
    assert lbl.detail["side"] == "stop"
    assert lbl.detail["same_day_both"] is True


def test_touch_label_no_touch_returns_none() -> None:
    bars = [BarRow(date(2026, 1, 2), high=105.0, low=98.0, close=102.0)]
    assert touch_label(bars_after=bars, **_TOUCH_KW) is None


# ---------------------------------------------------------------------------
# 纯函数：value_eval_label
# ---------------------------------------------------------------------------


def test_value_eval_label_attaches_valuation_error() -> None:
    bars = _bars(date(2026, 1, 2), [120.0])
    lbl = value_eval_label(
        decision_type="valuation",
        decision_ref="sess1",
        code="600519",
        decision_date=date(2026, 1, 1),
        horizon_days=1,
        base_close=100.0,
        bars_after=bars,
        estimate_mid=150.0,
    )
    assert lbl is not None
    assert lbl.label_kind == "value_eval"
    # 估值误差 =（估值中枢 - 判定价）/ 判定价 = (150-120)/120 = +25%（高估）
    assert lbl.detail["valuation_error"] == pytest.approx(0.25)
    assert lbl.detail["estimate_mid"] == 150.0


def test_value_eval_label_without_estimate_keeps_plain() -> None:
    bars = _bars(date(2026, 1, 2), [120.0])
    lbl = value_eval_label(
        decision_type="valuation",
        decision_ref="sess1",
        code="600519",
        decision_date=date(2026, 1, 1),
        horizon_days=1,
        base_close=100.0,
        bars_after=bars,
        estimate_mid=None,
    )
    assert lbl is not None
    assert "valuation_error" not in lbl.detail


# ---------------------------------------------------------------------------
# 装配：swing_signal 打标（内存库，幂等）
# ---------------------------------------------------------------------------


def _seed_daily_bars(conn, code: str, start: date, closes: list[float]) -> None:
    for i, c in enumerate(closes):
        conn.execute(
            """
            INSERT INTO fact_daily_bar (
                code, trade_date, open, high, low, close, volume, amount, adj_factor
            ) VALUES (?, ?, ?, ?, ?, ?, 1000, 100000.0, 1.0)
            """,
            [code, start + timedelta(days=i), c, c + 1.0, c - 1.0, c],
        )


def _seed_swing_signal(conn, signal_id: str, code: str, signal_date: date) -> None:
    conn.execute(
        """
        INSERT INTO swing_signal (
            signal_id, code, market, name, signal_date, screen_features_json,
            entry_ref, stop_ref, target_ref, status
        ) VALUES (?, ?, 'SH', '测试', ?, '{}', 100.0, 95.0, 120.0, 'pending')
        """,
        [signal_id, code, signal_date],
    )


def test_label_swing_signals_inserts_four_labels_and_idempotent() -> None:
    conn = connect_memory()
    migrate(conn)
    d0 = date(2026, 1, 5)  # 决策日，收盘 100
    _seed_daily_bars(conn, "600000", d0, [100.0])  # 决策日
    _seed_daily_bars(conn, "600000", date(2026, 1, 6), [102.0] * 25)  # 25 根后续
    _seed_swing_signal(conn, "sig1", "600000", d0)

    report = label_swing_signals(conn)
    assert report.scanned == 1
    # T+5/10/20 三枚已到期 + first_touch：close=102 区间 [101,103] 不触 95/120 → 观察中
    assert report.inserted == 3
    assert report.pending == 1

    rows = conn.execute(
        "SELECT label_kind, horizon, hit, return_pct FROM outcome_label "
        "WHERE decision_type = 'swing_pick' ORDER BY horizon"
    ).fetchall()
    assert [(r[0], r[1]) for r in rows] == [
        ("t_n", "T+10"),
        ("t_n", "T+20"),
        ("t_n", "T+5"),
    ]
    for _, _, hit, ret in rows:
        assert hit is True
        assert ret == pytest.approx(0.02)

    # 重跑幂等：不再插入
    report2 = label_swing_signals(conn)
    assert report2.inserted == 0
    count = conn.execute("SELECT count(*) FROM outcome_label").fetchone()[0]
    assert count == 3


def test_label_swing_signals_touch_inserted_when_touched() -> None:
    conn = connect_memory()
    migrate(conn)
    d0 = date(2026, 1, 5)
    _seed_daily_bars(conn, "600000", d0, [100.0])
    # 第 3 天 close 119 → high 120 触目标 120
    _seed_daily_bars(conn, "600000", date(2026, 1, 6), [102.0, 104.0, 119.0, 110.0])
    _seed_swing_signal(conn, "sig2", "600000", d0)

    report = label_swing_signals(conn)
    touch = conn.execute(
        "SELECT hit, label_date, detail_json FROM outcome_label WHERE label_kind = 'touch'"
    ).fetchone()
    assert report.inserted == 1  # 只有 touch 到期（T+5 未到期）
    assert touch[0] is True
    assert str(touch[1]) == "2026-01-08"
    assert json.loads(touch[2])["side"] == "target"


def test_label_swing_signals_skips_when_no_bars() -> None:
    conn = connect_memory()
    migrate(conn)
    _seed_swing_signal(conn, "sig3", "600000", date(2026, 1, 5))
    report = label_swing_signals(conn)
    assert report.inserted == 0
    assert len(report.warnings) == 1


# ---------------------------------------------------------------------------
# 装配：valuation 打标
# ---------------------------------------------------------------------------


def _seed_valuation_session(conn, session_id: str, code: str, started: date) -> None:
    conn.execute(
        """
        INSERT INTO agent_run (
            run_id, agent_name, started_at, model, prompt_version,
            status, config_hash, code_version
        ) VALUES ('run1', 'valuation', ?, 'm', 'v1', 'SUCCESS', 'h', 'c')
        """,
        [started],
    )
    conn.execute(
        "INSERT INTO agent_output (output_id, run_id, output_json) VALUES (?, 'run1', ?)",
        ["out1", json.dumps({"intrinsic_value_range": [140.0, 160.0]})],
    )
    conn.execute(
        """
        INSERT INTO committee_session (session_id, code, started_at, valuation_output_id)
        VALUES (?, ?, ?, 'out1')
        """,
        [session_id, code, started],
    )


def test_label_valuation_sessions_value_eval() -> None:
    conn = connect_memory()
    migrate(conn)
    d0 = date(2026, 1, 5)
    _seed_daily_bars(conn, "600519", d0, [100.0])
    _seed_daily_bars(conn, "600519", date(2026, 1, 6), [120.0] * 25)
    _seed_valuation_session(conn, "sess1", "600519", d0)

    report = label_valuation_sessions(conn)
    assert report.scanned == 1
    assert report.inserted == 1  # T+20 到期；T+60/T+250 未到期
    assert report.pending == 2

    row = conn.execute(
        "SELECT horizon, hit, detail_json FROM outcome_label WHERE decision_type = 'valuation'"
    ).fetchone()
    assert row[0] == "T+20"
    assert row[1] is True
    detail = json.loads(row[2])
    # estimate_mid = 150，label_close = 120 → 误差 +25%（高估）
    assert detail["estimate_mid"] == pytest.approx(150.0)
    assert detail["valuation_error"] == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# 评分卡：纯函数聚合
# ---------------------------------------------------------------------------


def test_aggregate_swing_empty_returns_none() -> None:
    assert aggregate_swing([]) is None


def test_aggregate_swing_hit_rate_and_fpr() -> None:
    facts = [
        LabelFact("t_n", "T+5", True),
        LabelFact("t_n", "T+5", False),
        LabelFact("t_n", "T+10", True),
        LabelFact("touch", "first_touch", False),
    ]
    m = aggregate_swing(facts)
    assert m is not None
    assert m.sample_size == 4
    assert m.hit_rate == pytest.approx(0.5)
    assert m.false_positive_rate == pytest.approx(0.5)  # T+5 两中一误报
    assert m.error_mean is None


def test_aggregate_swing_fpr_none_without_t5() -> None:
    m = aggregate_swing([LabelFact("t_n", "T+10", True)])
    assert m is not None and m.false_positive_rate is None


def test_aggregate_valuation_error_stats() -> None:
    facts = [
        LabelFact("value_eval", "T+20", True, valuation_error=0.10),
        LabelFact("value_eval", "T+20", False, valuation_error=-0.10),
        LabelFact("value_eval", "T+60", True, valuation_error=None),
    ]
    m = aggregate_valuation(facts)
    assert m is not None
    assert m.sample_size == 3
    assert m.hit_rate == pytest.approx(2 / 3)
    assert m.error_mean == pytest.approx(0.0)
    # 样本 std（ddof=1）：[0.1, -0.1] → sqrt(0.02)
    assert m.error_std == pytest.approx(0.14142135, abs=1e-6)
    assert m.false_positive_rate is None


def test_aggregate_valuation_single_error_std_none() -> None:
    m = aggregate_valuation([LabelFact("value_eval", "T+20", True, valuation_error=0.1)])
    assert m is not None and m.error_std is None


# ---------------------------------------------------------------------------
# 评分卡：写入幂等
# ---------------------------------------------------------------------------


def test_write_scorecards_idempotent() -> None:
    conn = connect_memory()
    migrate(conn)
    window_end = date(2026, 2, 10)
    # 窗口内两枚波段标签（T+5 一中一误）
    for ref, horizon, hit in (("s1", "T+5", True), ("s2", "T+5", False)):
        conn.execute(
            """
            INSERT INTO outcome_label (
                decision_type, decision_ref, code, decision_date,
                label_kind, horizon, label_date, return_pct, hit, detail_json
            ) VALUES ('swing_pick', ?, '600000', '2026-01-20', 't_n', ?, '2026-01-27',
                      0.01, ?, '{}')
            """,
            [ref, horizon, hit],
        )
    # 窗口外（91 天前）标签不计入
    conn.execute(
        """
        INSERT INTO outcome_label (
            decision_type, decision_ref, code, decision_date,
            label_kind, horizon, label_date, return_pct, hit, detail_json
        ) VALUES ('swing_pick', 's_old', '600000', '2025-10-01', 't_n', 'T+5',
                  '2025-10-08', -0.05, FALSE, '{}')
        """
    )

    written = write_scorecards(conn, window_end=window_end)
    assert written == ("swing_pick",)  # valuation 无样本跳过
    row = conn.execute(
        "SELECT sample_size, hit_rate, false_positive_rate FROM agent_scorecard "
        "WHERE agent_name = 'swing_pick'"
    ).fetchone()
    assert row[0] == 2
    assert row[1] == pytest.approx(0.5)
    assert row[2] == pytest.approx(0.5)

    # 重跑幂等：仍一行
    write_scorecards(conn, window_end=window_end)
    count = conn.execute(
        "SELECT count(*) FROM agent_scorecard WHERE agent_name = 'swing_pick'"
    ).fetchone()[0]
    assert count == 1
