"""P3.5 Walk-Forward 滚动验证测试。

验证目标：
1. 窗口切分：滚动 IS/OOS 对齐、尾段不足 OOS 丢弃；
2. 过拟合检出：构造"仅 IS 期有效的参数组"，验证其 oos_is_ratio 塌陷、被门禁拦下；
3. 边界规则：IS<=0 记 0、全无效参数跳过该窗、样本不足 min_windows 不放行；
4. 状态隔离：多参数组/多窗口连跑后，主账户 acc_value/acc_swing 持仓现金零污染；
5. 阈值外置：min_oos_is_ratio 等全部来自 config swing_track.walk_forward。

合成数据：单标的（600519）380 个交易日、收盘价恒 10.00（无趋势），
策略 A 固定开多（IS 期可赚、OOS 期不亏不赚但 Sharpe 无波动→None），
策略 B 全程不开仓（Sharpe 恒 None）。用两个自定义参数组做 IS 寻优对照。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest

from viqtrader.engine.backtest import StrategyContext
from viqtrader.engine.types import OrderSide
from viqtrader.lifecycle.walk_forward import TRIAL_ACCOUNT, WalkForwardRunner

CODE = "600519"
MARKET = "SH"


def _make_days(start: date, n: int) -> list[date]:
    """连续 n 个自然日（测试用，忽略真实交易日历——fact_daily_bar 即日历）。"""
    return [start + timedelta(days=i) for i in range(n)]


@pytest.fixture()
def flat_env(conn, seed_stock, seed_bar):
    """380 日恒定收盘 10.00 的合成行情（无趋势、无波动）。"""
    seed_stock(CODE, MARKET, "测试标的")
    for d in _make_days(date(2024, 1, 1), 380):
        seed_bar(CODE, d.isoformat(), open_=10.0, high=10.0, low=10.0, close=10.0)
    return conn


class _HoldOnce:
    """按参数 hold=True/False 决定是否首个有历史日买入 100 股并持有。

    恒定收盘价下：hold=True 有成交但净值无波动（Sharpe None）；
    hold=False 全程现金（Sharpe None）。两者 Sharpe 均不可算。
    """

    def __init__(self, hold: bool) -> None:
        self._hold = hold
        self._done = False

    def on_bar(self, ctx: StrategyContext) -> list[Any]:
        if self._done or not self._hold or ctx.latest_bar(CODE) is None:
            return []
        self._done = True
        return [ctx.make_intent("acc_swing", "swing", CODE, MARKET, OrderSide.BUY, 100)]


def _factory_flat(params: dict[str, Any]) -> _HoldOnce:
    return _HoldOnce(hold=bool(params.get("hold", False)))


class _BuyEveryNDays:
    """每 N 日买入 100 股（T+1 次日卖出），制造确定性现金流与波动。

    恒定收盘价 10.00 下，买卖同价、仅损耗费用（佣金+印花税+过户费+滑点），
    净值单调缓降——Sharpe 为负（稳定可算），用于验证 IS/OOS 负 Sharpe 边界。
    """

    def __init__(self, step: int) -> None:
        self._step = max(1, step)
        self._counter = 0
        self._holding = False

    def on_bar(self, ctx: StrategyContext) -> list[Any]:
        if ctx.latest_bar(CODE) is None:
            return []
        self._counter += 1
        if self._holding:
            self._holding = False
            return [ctx.make_intent("acc_swing", "swing", CODE, MARKET, OrderSide.SELL, 100)]
        if self._counter % self._step == 0:
            self._holding = True
            return [ctx.make_intent("acc_swing", "swing", CODE, MARKET, OrderSide.BUY, 100)]
        return []


def _factory_churn(params: dict[str, Any]) -> _BuyEveryNDays:
    return _BuyEveryNDays(step=int(params.get("step", 2)))


class TestWindowSplit:
    def test_tail_segment_dropped(self, flat_env, config) -> None:
        """380 交易日、is=250/oos=60 → 2 个完整窗口，尾 10 日丢弃。"""
        runner = WalkForwardRunner(flat_env, config)
        days = runner._trading_days(date(2024, 1, 1), date(2025, 1, 14))
        assert len(days) == 380
        windows = runner._split_windows(days)
        assert len(windows) == 2
        is_w, oos_w = windows[0]
        assert len(is_w) == 250 and len(oos_w) == 60
        # 第二窗紧跟第一窗 OOS 之后
        is_w2, oos_w2 = windows[1]
        assert is_w2[0] == days[60]
        assert oos_w2[-1] == days[369]  # 第 370 个（索引 369），尾 370~379 丢弃

    def test_insufficient_days_raises(self, flat_env, config) -> None:
        runner = WalkForwardRunner(flat_env, config)
        with pytest.raises(ValueError, match="不足一个完整窗口"):
            runner.run(_factory_flat, [{"hold": True}], date(2024, 1, 1), date(2024, 6, 1))

    def test_empty_param_grid_raises(self, flat_env, config) -> None:
        runner = WalkForwardRunner(flat_env, config)
        with pytest.raises(ValueError, match="param_grid"):
            runner.run(_factory_flat, [], date(2024, 1, 1), date(2025, 1, 14))


class TestGateLogic:
    def test_flat_strategy_blocked_by_zero_sharpe(self, flat_env, config) -> None:
        """hold=True 组首日手续费微亏致 IS Sharpe 小幅为负，hold=False 组恒现金 Sharpe None。

        负 IS 的窗 ratio 保守记 0 → 门禁拒绝（防止"负样本内"被比率翻转语义放行）。

        边界语义（前视防线 latest_bar 强制 <T）：窗口首日能否成交取决于前一
        交易日是否已有行情。window 0 的 IS 首日是全局首个播种日（无前序行情）
        → 次日才买入，首日净值=期初现金、次日扣费微亏，Sharpe 可算为负；
        window 1 的 IS 首日已有前一日行情 → 首日即买入，净值恒定，两组参数
        Sharpe 均 None → 整窗跳过。两种边界规则本用例一并覆盖。
        """
        runner = WalkForwardRunner(flat_env, config)
        result = runner.run(
            _factory_flat,
            [{"hold": True}, {"hold": False}],
            date(2024, 1, 1),
            date(2025, 1, 14),
        )
        assert len(result.windows) == 2
        # window 0：hold=True 有成交（手续费微亏）Sharpe<0，成为 IS 最优
        w0 = result.windows[0]
        assert w0.best_params == {"hold": True}
        assert w0.is_sharpe is not None and w0.is_sharpe < 0
        assert w0.oos_is_ratio == 0.0  # IS<=0 保守记 0
        # window 1：首日即买入净值恒定，两组参数 Sharpe 均 None → 整窗跳过
        w1 = result.windows[1]
        assert w1.best_params is None
        assert w1.oos_is_ratio == 0.0
        assert result.summary_ratio == 0.0
        assert result.passed_gate is False

    def test_all_invalid_params_window_skipped(self, flat_env, config) -> None:
        """全参数组 Sharpe 均 None（恒现金）→ best_params=None 的窗被跳过，valid=0 → 拒绝。"""
        runner = WalkForwardRunner(flat_env, config)
        result = runner.run(
            _factory_flat,
            [{"hold": False}],  # 恒现金，Sharpe None
            date(2024, 1, 1),
            date(2025, 1, 14),
        )
        assert len(result.windows) == 2
        for w in result.windows:
            assert w.best_params is None
            assert w.oos_is_ratio == 0.0
        assert result.passed_gate is False
        assert result.summary_ratio == 0.0

    def test_overfit_strategy_blocked(self, flat_env, config) -> None:
        """高频换手策略稳定亏损（Sharpe<0）→ IS 最优参数 Sharpe 负 → ratio 记 0 → 拒绝。"""
        runner = WalkForwardRunner(flat_env, config)
        result = runner.run(
            _factory_churn,
            [{"step": 2}, {"step": 3}],
            date(2024, 1, 1),
            date(2025, 1, 14),
        )
        assert len(result.windows) == 2
        for w in result.windows:
            assert w.best_params is not None  # 至少一组 Sharpe 可算（负）
            assert w.is_sharpe is not None and w.is_sharpe < 0
            assert w.oos_is_ratio == 0.0  # IS<=0 保守记 0
        assert result.passed_gate is False
        assert result.summary_ratio == 0.0

    def test_min_windows_not_met_blocks(self, flat_env, config) -> None:
        """构造仅 1 个有效窗口（缩短区间）→ 窗口数 < min_windows=2 → 即使 ratio 够也拒绝。"""
        runner = WalkForwardRunner(flat_env, config)
        # 310 交易日 → 仅 1 个完整窗口（250+60）
        result = runner.run(
            _factory_churn,
            [{"step": 2}],
            date(2024, 1, 1),
            date(2024, 11, 5),
        )
        assert len(result.windows) == 1
        assert result.passed_gate is False


class TestIsolation:
    def test_main_accounts_untouched(self, flat_env, config, conn) -> None:
        """多窗口连跑后，主账户无持仓、无现金流水、无成交、无残留试跑账户。"""
        runner = WalkForwardRunner(flat_env, config)
        runner.run(
            _factory_churn,
            [{"step": 2}],
            date(2024, 1, 1),
            date(2025, 1, 14),
        )
        for acc in ("acc_value", "acc_swing"):
            pos = conn.execute(
                "SELECT COUNT(*) FROM position WHERE account_id = ?", [acc]
            ).fetchone()[0]
            cash = conn.execute(
                "SELECT COUNT(*) FROM cash_ledger WHERE account_id = ?", [acc]
            ).fetchone()[0]
            trade = conn.execute(
                "SELECT COUNT(*) FROM trade_log WHERE account_id = ?", [acc]
            ).fetchone()[0]
            assert pos == 0 and cash == 0 and trade == 0, f"{acc} 被污染"
        # 试跑账户已拆除
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM account WHERE account_id = ?", [TRIAL_ACCOUNT]
            ).fetchone()[0]
            == 0
        )
