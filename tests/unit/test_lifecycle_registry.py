"""P3.6 策略生命周期状态机测试：注册/晋级/降级/两种退役/配额联动/退役清仓/价值轨拒入。

合成数据设计（全为手工可核对的确定性序列）：
- 标的 600519 收盘价按日期单调变动，控制策略净值曲线形状；
- 通过 BacktestEngine 回放 _ScriptedStrategy 产生真实 trade_log/order_intent 归因到 strategy_id，
  再由 LifecycleEngine.evaluate 读 equity 重放序列做 KPI 判定；
- 阶段迁移走 evaluate_and_apply 或显式 apply，校验 strategy_stage 当前态 + history 流水。

判定优先级覆盖：单日亏损立即退役 > 持续低 Sharpe 退役 > 降级 > 晋级。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import duckdb
import pytest

from viqtrader.engine.backtest import BacktestEngine, StrategyContext
from viqtrader.engine.types import OrderSide
from viqtrader.lifecycle.registry import LifecycleEngine, Stage

CODE = "600519"
MARKET = "SH"

# 测试置位常用 SQL（手动推进阶段，绕过 KPI 判定模拟已晋级/已服役状态）
_TO_PROBATION_SQL = (
    "UPDATE strategy_stage SET stage = 'PROBATION', capital_pct = 0.05 WHERE strategy_id = ?"
)
_TO_PRODUCTION_SQL = (
    "UPDATE strategy_stage SET stage = 'PRODUCTION', capital_pct = 0.30, "
    "entered_at = ? WHERE strategy_id = ?"
)


def _days(start: date, n: int) -> list[date]:
    return [start + timedelta(days=i) for i in range(n)]


@pytest.fixture()
def lifecycle(conn, config) -> LifecycleEngine:
    return LifecycleEngine(conn, config)


def _seed_prices(
    conn, seed_stock, seed_bar, closes: list[float], start: date = date(2025, 1, 1)
) -> list[date]:
    """按收盘价列表种行情（open=close，无波动内部）。"""
    seed_stock(CODE, MARKET, "测试标的")
    days = _days(start, len(closes))
    for d, c in zip(days, closes, strict=True):
        seed_bar(CODE, d.isoformat(), open_=c, high=c, low=c, close=c)
    return days


class _ScriptedBuy:
    """首个有历史日买入 qty 股并持有到结束（不回补）。"""

    def __init__(self, qty: int = 100) -> None:
        self._qty = qty
        self._done = False

    def on_bar(self, ctx: StrategyContext) -> list[Any]:
        if self._done or ctx.latest_bar(CODE) is None:
            return []
        self._done = True
        return [ctx.make_intent("acc_swing", "swing", CODE, MARKET, OrderSide.BUY, self._qty)]


def _register_and_stage(
    lc: LifecycleEngine, name: str = "s1", version: str = "1.0", as_of: date | None = None
) -> str:
    return lc.register(name, version, {"k": 1}, as_of=as_of)


class TestRegister:
    def test_initial_stage_incubating(self, lifecycle: LifecycleEngine, conn) -> None:
        sid = _register_and_stage(lifecycle)
        assert lifecycle.stage_of(sid) == Stage.INCUBATING
        assert lifecycle.capital_pct_of(sid) == 0.0
        hist = lifecycle.history(sid)
        assert len(hist) == 1 and hist[0]["to_stage"] == "INCUBATING"
        assert hist[0]["from_stage"] is None

    def test_value_track_rejected(self, lifecycle: LifecycleEngine) -> None:
        with pytest.raises(ValueError, match="价值轨"):
            lifecycle.register("v1", "1.0", None, track="value")

    def test_duplicate_name_version_rejected(self, lifecycle: LifecycleEngine) -> None:
        _register_and_stage(lifecycle, name="dup")
        with pytest.raises(duckdb.ConstraintException):  # UNIQUE(name, version)
            _register_and_stage(lifecycle, name="dup")


class TestPromotion:
    def test_incubating_to_paper_via_wf_gate(self, lifecycle: LifecycleEngine) -> None:
        sid = _register_and_stage(lifecycle)
        ev = lifecycle.evaluate_and_apply(sid, date(2025, 1, 10), wf_gate_passed=True)
        assert ev.target_stage == Stage.PAPER
        assert lifecycle.stage_of(sid) == Stage.PAPER
        assert lifecycle.capital_pct_of(sid) == 0.0
        assert any(h["to_stage"] == "PAPER" for h in lifecycle.history(sid))

    def test_incubating_stays_without_gate(self, lifecycle: LifecycleEngine) -> None:
        sid = _register_and_stage(lifecycle)
        ev = lifecycle.evaluate_and_apply(sid, date(2025, 1, 10), wf_gate_passed=False)
        assert ev.target_stage == Stage.INCUBATING
        assert lifecycle.stage_of(sid) == Stage.INCUBATING

    def test_paper_to_probation_after_min_days(
        self, lifecycle: LifecycleEngine, conn, config, seed_stock, seed_bar
    ) -> None:
        """PAPER 满 20 交易日、累计收益 > 0、回撤 > -5% → 晋级 PROBATION。"""
        # 21 日温和上涨：100 股从 10.00 涨到 11.00（+10%），无回撤
        closes = [10.0 + i * 0.05 for i in range(21)]
        days = _seed_prices(conn, seed_stock, seed_bar, closes)
        engine = BacktestEngine(conn, config)
        engine.run(_ScriptedBuy(100), days[0], days[-1], accounts=["acc_swing"])

        sid = _register_and_stage(lifecycle, as_of=days[0])
        lifecycle.evaluate_and_apply(sid, days[0], wf_gate_passed=True)  # → PAPER
        # 把该策略名下首笔买入 Intent 归因（脚本策略由 BacktestEngine 落库，手工回填 strategy_id）
        conn.execute("UPDATE order_intent SET strategy_id = ? WHERE side = 'BUY'", [sid])
        # PAPER 评估日 = 最后一日（满 21 交易日 > paper_min_days 20）
        ev = lifecycle.evaluate_and_apply(sid, days[-1])
        assert ev.target_stage == Stage.PROBATION, ev.reason
        assert lifecycle.stage_of(sid) == Stage.PROBATION
        assert lifecycle.capital_pct_of(sid) == pytest.approx(0.05)

    def test_probation_to_production_high_sharpe(
        self, lifecycle: LifecycleEngine, conn, config, seed_stock, seed_bar
    ) -> None:
        """PROBATION 滚动 30 日 Sharpe > 1.0 → 晋升 PRODUCTION。"""
        # 32 日稳定上涨（每日 +0.5%），滚动 30 日 Sharpe 显著 > 1
        closes = [10.0 * (1.005**i) for i in range(32)]
        days = _seed_prices(conn, seed_stock, seed_bar, closes)
        engine = BacktestEngine(conn, config)
        engine.run(_ScriptedBuy(100), days[0], days[-1], accounts=["acc_swing"])

        sid = _register_and_stage(lifecycle, as_of=days[0])
        lifecycle.evaluate_and_apply(sid, days[0], wf_gate_passed=True)  # → PAPER
        conn.execute("UPDATE order_intent SET strategy_id = ? WHERE side = 'BUY'", [sid])
        # 手动推进到 PROBATION（绕过 paper_min_days，直接 UPDATE 当前态模拟已晋级）
        conn.execute(_TO_PROBATION_SQL, [sid])
        conn.execute(
            "UPDATE strategy_stage SET entered_at = ? WHERE strategy_id = ?", [days[0], sid]
        )
        ev = lifecycle.evaluate_and_apply(sid, days[-1])
        assert ev.target_stage == Stage.PRODUCTION, ev.reason
        assert lifecycle.capital_pct_of(sid) == pytest.approx(0.30)


class TestDemotionAndRetire:
    def test_production_demoted_on_low_sharpe(
        self, lifecycle: LifecycleEngine, conn, config, seed_stock, seed_bar
    ) -> None:
        """PRODUCTION 滚动 30 日 Sharpe 跌破 1.0 → 降级回 PROBATION。"""
        # 40 日交替 +0.8% / -0.75%：末 30 交易日窗口 15 涨 15 跌，
        # 均值 +0.025%/日、波动 0.775% → 滚动 Sharpe ≈ 0.5 ∈ [0.3, 1.0)，
        # 只触发保持线降级（最大单日 -0.75% 不触单日退役，末日滚动 Sharpe ≥ 0.3
        # 连续低迷天数为 0 不触低迷退役）
        closes = [10.0]
        for i in range(1, 40):
            closes.append(closes[-1] * (1.008 if i % 2 == 1 else 0.9925))
        days = _seed_prices(conn, seed_stock, seed_bar, closes)
        engine = BacktestEngine(conn, config)
        engine.run(_ScriptedBuy(100), days[0], days[-1], accounts=["acc_swing"])

        sid = _register_and_stage(lifecycle, as_of=days[0])
        conn.execute("UPDATE order_intent SET strategy_id = ? WHERE side = 'BUY'", [sid])
        conn.execute(_TO_PRODUCTION_SQL, [days[0], sid])
        ev = lifecycle.evaluate_and_apply(sid, days[-1])
        assert ev.target_stage == Stage.PROBATION, ev.reason
        assert "降级" in ev.reason or "保持线" in ev.reason

    def test_immediate_retire_on_single_day_loss(
        self, lifecycle: LifecycleEngine, conn, config, seed_stock, seed_bar
    ) -> None:
        """单日亏损 ≤ -5% → 立即退役（优先级最高）。"""
        # 先平稳，末日暴跌 -8%
        closes = [10.0] * 20 + [9.2]
        days = _seed_prices(conn, seed_stock, seed_bar, closes)
        engine = BacktestEngine(conn, config)
        engine.run(_ScriptedBuy(100), days[0], days[-1], accounts=["acc_swing"])

        sid = _register_and_stage(lifecycle, as_of=days[0])
        conn.execute("UPDATE order_intent SET strategy_id = ? WHERE side = 'BUY'", [sid])
        ev = lifecycle.evaluate_and_apply(sid, days[-1])
        assert ev.target_stage == Stage.RETIRED, ev.reason
        assert "单日" in ev.reason
        assert lifecycle.capital_pct_of(sid) == 0.0
        # 退役清仓 Intent：该策略名下 acc_swing 持仓 100 股 → 1 个 MARKET SELL Intent
        assert len(ev.liquidation_intents) == 1
        li = ev.liquidation_intents[0]
        assert li.code == CODE and li.side == OrderSide.SELL and li.qty == 100
        assert li.account_id == "acc_swing" and li.strategy_id == sid

    def test_low_sharpe_sustained_retire(
        self, lifecycle: LifecycleEngine, conn, config, seed_stock, seed_bar
    ) -> None:
        """滚动 30 日 Sharpe 持续 < 0.3 超 14 自然日 → 退役。"""
        # 45 日持续阴跌（每日 -0.3%），滚动 Sharpe 恒负且远低于 0.3
        closes = [10.0 * (0.997**i) for i in range(45)]
        days = _seed_prices(conn, seed_stock, seed_bar, closes)
        engine = BacktestEngine(conn, config)
        engine.run(_ScriptedBuy(100), days[0], days[-1], accounts=["acc_swing"])

        sid = _register_and_stage(lifecycle, as_of=days[0])
        conn.execute("UPDATE order_intent SET strategy_id = ? WHERE side = 'BUY'", [sid])
        conn.execute(_TO_PRODUCTION_SQL, [days[0], sid])
        ev = lifecycle.evaluate_and_apply(sid, days[-1])
        assert ev.target_stage == Stage.RETIRED, ev.reason
        assert "持续" in ev.reason or "低迷" in ev.reason


class TestLiquidation:
    def test_retired_stage_no_further_migration(self, lifecycle: LifecycleEngine) -> None:
        sid = _register_and_stage(lifecycle)
        lifecycle.evaluate_and_apply(sid, date(2025, 1, 10), wf_gate_passed=True)
        # 强制退役
        from viqtrader.lifecycle.registry import Stage as S

        lifecycle._conn.execute(
            "UPDATE strategy_stage SET stage = 'RETIRED' WHERE strategy_id = ?", [sid]
        )
        ev = lifecycle.evaluate(sid, date(2025, 1, 20))
        assert ev.target_stage == S.RETIRED and ev.current_stage == S.RETIRED

    def test_no_position_no_liquidation_intents(self, lifecycle: LifecycleEngine) -> None:
        """策略名下无持仓 → 退役时 liquidation_intents 为空。"""
        sid = _register_and_stage(lifecycle)
        ev = lifecycle.evaluate(sid, date(2025, 1, 10))
        assert ev.liquidation_intents == ()


class TestKpiSnapshot:
    def test_no_change_refreshes_kpi_only(self, lifecycle: LifecycleEngine, conn) -> None:
        """未触发迁移时仅更新 kpi_json，不写 history。"""
        sid = _register_and_stage(lifecycle)
        before = len(lifecycle.history(sid))
        lifecycle.evaluate_and_apply(sid, date(2025, 1, 10), wf_gate_passed=False)
        assert len(lifecycle.history(sid)) == before
        row = conn.execute(
            "SELECT kpi_json FROM strategy_stage WHERE strategy_id = ?", [sid]
        ).fetchone()
        assert row is not None and row[0] is not None
