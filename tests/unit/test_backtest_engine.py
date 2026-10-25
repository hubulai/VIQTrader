"""P3.1 回测引擎测试：成交价/净值/指标手工核对 + 前视防线 + T+1 + 分红。

合成环境：茅台 2026-01-05 ~ 01-16 共 10 个交易日，价格序列专为手算设计。
费用模型（default.yaml）：佣金双边 min(0.025%, 5 元) + 印花税卖出 0.05% + 过户费双边 0.001%；
滑点 10bp（MARKET 单买入开盘价 x1.001 四舍五入到分）。
"""

from __future__ import annotations

import itertools
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from viqtrader.engine.backtest import BacktestEngine, StrategyContext
from viqtrader.engine.types import OrderIntent, OrderSide, OrderType

GOLDEN_DIR = Path(__file__).resolve().parents[1] / "golden"

# (date, open, high, low, close)——主板 10% 涨跌幅内，专为手算设计
MAOTAI_BARS = [
    (date(2026, 1, 5), 100.0, 100.5, 99.5, 100.0),
    (date(2026, 1, 6), 100.5, 101.0, 99.8, 100.8),
    (date(2026, 1, 7), 100.6, 101.5, 100.0, 101.0),
    (date(2026, 1, 8), 101.0, 101.5, 100.0, 100.5),
    (date(2026, 1, 9), 100.0, 100.5, 98.5, 99.0),
    (date(2026, 1, 12), 99.0, 99.5, 97.5, 98.0),
    (date(2026, 1, 13), 98.0, 99.5, 97.8, 99.0),
    (date(2026, 1, 14), 99.5, 100.5, 99.0, 100.0),
    (date(2026, 1, 15), 100.0, 102.5, 99.8, 102.0),
    (date(2026, 1, 16), 102.0, 103.5, 101.5, 103.0),
]
START = date(2026, 1, 5)
END = date(2026, 1, 16)


class BuyOnce:
    """首个有历史数据的交易日买入 100 股 MARKET 单（即第 2 个交易日成交）。"""

    def __init__(self, code: str = "600519", market: str = "SH") -> None:
        self._done = False
        self._code = code
        self._market = market

    def on_bar(self, ctx: StrategyContext) -> list[OrderIntent]:
        if self._done:
            return []
        if ctx.latest_bar(self._code) is None:
            return []  # 首日无历史数据，不决策（方案 A 语义）
        self._done = True
        return [ctx.make_intent("acc_value", "value", self._code, self._market, OrderSide.BUY, 100)]


class NeverTrades:
    def on_bar(self, ctx: StrategyContext) -> list[OrderIntent]:
        return []


@pytest.fixture()
def maotai_env(conn, seed_stock, seed_bar):
    """茅台 10 日合成环境。"""
    seed_stock("600519", "SH", "贵州茅台")
    for d, o, h, low, c in MAOTAI_BARS:
        seed_bar("600519", d.isoformat(), open_=o, high=h, low=low, close=c)
    return conn


@pytest.fixture()
def engine(conn, config) -> BacktestEngine:
    return BacktestEngine(conn, config)


def equity_of(result, account_id: str, day: date) -> Decimal:
    for e in result.equity_curve:
        if e.account_id == account_id and e.trade_date == day:
            return e.total_value
    raise AssertionError(f"无净值记录: {account_id} {day}")


# ---------------------------------------------------------------------------
# 成交价与净值手工核对
# ---------------------------------------------------------------------------


class TestBuyAndHold:
    def test_fill_price_with_slippage(self, maotai_env, engine: BacktestEngine) -> None:
        """01-06 开盘价 100.50 x 1.001 = 100.6005 -> 100.60（四舍五入到分）。"""
        result = engine.run(BuyOnce(), START, END)
        assert len(result.trades) == 1
        trade = result.trades[0]
        assert trade.trade_date == date(2026, 1, 6)
        assert trade.qty == 100
        assert trade.price == Decimal("100.60")
        # 费用：佣金 max(10060x0.00025, 5)=5.00 + 过户费 10060x0.00001=0.10 -> 5.10
        assert trade.amount_cny == Decimal("-10065.10")

    def test_equity_curve_hand_checked(self, maotai_env, engine: BacktestEngine) -> None:
        """逐日净值 = 现金 789934.90 + 100 股 x 当日收盘（波段轨恒 200000）。"""
        result = engine.run(BuyOnce(), START, END)
        cash = Decimal("789934.90")
        expected = {d: cash + 100 * Decimal(str(c)) for d, _o, _h, _l, c in MAOTAI_BARS}
        expected[date(2026, 1, 5)] = Decimal("800000")  # 首日未买入
        for d, *_ in MAOTAI_BARS:
            assert equity_of(result, "acc_value", d) == expected[d], f"{d} 净值不符"
            assert equity_of(result, "acc_swing", d) == Decimal("200000")

    def test_first_day_no_history_no_trade(self, maotai_env, engine: BacktestEngine) -> None:
        """方案 A：首日无任何历史数据，策略不产出，首日无成交。"""
        result = engine.run(BuyOnce(), START, END)
        assert all(t.trade_date != START for t in result.trades)


# ---------------------------------------------------------------------------
# 通用指标手工核对
# ---------------------------------------------------------------------------


class TestMetrics:
    def test_hand_checked_metrics(self, maotai_env, engine: BacktestEngine) -> None:
        """组合总值序列（万分之一精度）：

        峰值 1000034.90（01-07），谷底 999734.90（01-12），末值 1000234.90（01-16）。
        """
        result = engine.run(BuyOnce(), START, END)
        m = result.metrics()
        assert m["trading_days"] == 10
        assert m["total_return"] == pytest.approx(234.90 / 1_000_000, abs=1e-9)
        assert m["max_drawdown"] == pytest.approx(-300.0 / 1_000_034.90, abs=1e-9)
        assert m["sharpe"] is not None

    def test_never_trades_zero_return(self, maotai_env, engine: BacktestEngine) -> None:
        result = engine.run(NeverTrades(), START, END)
        m = result.metrics()
        assert m["total_return"] == pytest.approx(0.0)
        assert m["max_drawdown"] == pytest.approx(0.0)
        assert m["sharpe"] is None  # 日收益 std=0，Sharpe 不可算
        assert result.trades == []

    def test_single_day_metrics_none(self, maotai_env, engine: BacktestEngine) -> None:
        result = engine.run(NeverTrades(), START, START)
        m = result.metrics()
        assert m["trading_days"] == 1
        assert m["total_return"] is None

    def test_precision_note_present(self, maotai_env, engine: BacktestEngine) -> None:
        """回测报告强制标注精度受限（设计文档第九章硬约束）。"""
        result = engine.run(NeverTrades(), START, END)
        assert "精度受限" in result.precision_note

    def test_empty_range_fails(self, engine: BacktestEngine) -> None:
        with pytest.raises(ValueError, match="无交易日"):
            engine.run(NeverTrades(), date(2030, 1, 1), date(2030, 12, 31))


# ---------------------------------------------------------------------------
# 前视偏差防线（任务 3.2）
# ---------------------------------------------------------------------------


class TestLookaheadGuard:
    def test_bar_guard(self, maotai_env, engine: BacktestEngine) -> None:
        """T 日 ctx 能见的最新 bar 必须是 T-1 交易日，首日必须为 None。"""
        seen: dict[date, date | None] = {}

        class Recorder:
            def on_bar(self, ctx: StrategyContext) -> list[OrderIntent]:
                bar = ctx.latest_bar("600519")
                seen[ctx.trade_date] = bar.trade_date if bar else None
                return []

        engine.run(Recorder(), START, END)
        days = [d for d, *_ in MAOTAI_BARS]
        assert seen[days[0]] is None
        for prev, cur in itertools.pairwise(days):
            assert seen[cur] == prev, f"{cur} 应只能看到 {prev}，实际 {seen[cur]}"

    def test_financial_guard(self, maotai_env, conn, engine: BacktestEngine) -> None:
        """ann_date = T 的财报 T 日不可见（公告次日生效），T+1 可见。"""
        conn.execute(
            """
            INSERT INTO fact_financial_version
                (code, report_period, ann_date, revenue, net_profit, roe)
            VALUES ('600519', '2025-12-31', '2026-01-08', 1000.0, 500.0, 0.30)
            """
        )
        seen: dict[date, date | None] = {}

        class Recorder:
            def on_bar(self, ctx: StrategyContext) -> list[OrderIntent]:
                fin = ctx.latest_financial("600519")
                seen[ctx.trade_date] = fin.report_period if fin else None
                return []

        engine.run(Recorder(), START, END)
        assert seen[date(2026, 1, 8)] is None  # 公告当日不可见
        assert seen[date(2026, 1, 9)] == date(2025, 12, 31)  # 次日起可见

    def test_valuation_guard(self, maotai_env, conn, engine: BacktestEngine) -> None:
        """trade_date = T 的估值记录 T 日不可见。"""
        conn.execute(
            """
            INSERT INTO fact_valuation
                (code, trade_date, pe_ttm, pb, ps_ttm, dv_ratio, market_cap, pe_percentile)
            VALUES ('600519', '2026-01-07', 30.0, 10.0, 15.0, 0.01, 1.2e12, 0.5)
            """
        )
        seen: dict[date, date | None] = {}

        class Recorder:
            def on_bar(self, ctx: StrategyContext) -> list[OrderIntent]:
                val = ctx.latest_valuation("600519")
                seen[ctx.trade_date] = val.trade_date if val else None
                return []

        engine.run(Recorder(), START, END)
        assert seen[date(2026, 1, 7)] is None
        assert seen[date(2026, 1, 8)] == date(2026, 1, 7)


# ---------------------------------------------------------------------------
# T+1 与未成交（回测环境下的全链路规则复用）
# ---------------------------------------------------------------------------


class TestRulesInBacktest:
    def test_t1_sell_rejected_same_day(self, maotai_env, engine: BacktestEngine) -> None:
        """同一交易日 BUY + SELL：BUY 成交，SELL 被 T1 拒绝（当日买入全额冻结）。"""

        class BuyThenSell:
            def __init__(self) -> None:
                self._done = False

            def on_bar(self, ctx: StrategyContext) -> list[OrderIntent]:
                if self._done or ctx.latest_bar("600519") is None:
                    return []
                self._done = True
                buy = ctx.make_intent("acc_value", "value", "600519", "SH", OrderSide.BUY, 100)
                sell = ctx.make_intent("acc_value", "value", "600519", "SH", OrderSide.SELL, 100)
                return [buy, sell]

        result = engine.run(BuyThenSell(), START, END)
        assert len(result.trades) == 1
        assert result.trades[0].side == "BUY"
        assert len(result.rejected) == 1
        assert "T1" in result.rejected[0].rejection_reason

    def test_limit_up_rejected(self, conn, seed_stock, seed_bar, engine: BacktestEngine) -> None:
        """一字涨停（开盘=涨停价）MARKET 买入被 PriceLimitRule 拒绝（规则层双保险）。"""
        seed_stock("600519", "SH", "贵州茅台")
        seed_bar("600519", "2026-01-05", open_=100.0, high=100.5, low=99.5, close=100.0)
        # 前收 100 -> 涨停 110.00；01-06 一字涨停
        seed_bar("600519", "2026-01-06", open_=110.0, high=110.0, low=110.0, close=110.0)
        result = engine.run(BuyOnce(), date(2026, 1, 5), date(2026, 1, 6))
        assert result.trades == []
        assert len(result.rejected) == 1
        assert "PRICE_LIMIT" in result.rejected[0].rejection_reason

    def test_limit_order_not_touch_unfilled(self, maotai_env, engine: BacktestEngine) -> None:
        """LIMIT 买入价低于当日最低 -> 不成交。"""

        class LowLimitBuy:
            def __init__(self) -> None:
                self._done = False

            def on_bar(self, ctx: StrategyContext) -> list[OrderIntent]:
                if self._done or ctx.latest_bar("600519") is None:
                    return []
                self._done = True
                return [
                    ctx.make_intent(
                        "acc_value",
                        "value",
                        "600519",
                        "SH",
                        OrderSide.BUY,
                        100,
                        order_type=OrderType.LIMIT,
                        price=Decimal("90.00"),  # 远低于当日最低 99.8
                    )
                ]

        result = engine.run(LowLimitBuy(), START, END)
        assert result.trades == []
        assert len(result.unfilled) == 1


# ---------------------------------------------------------------------------
# 分红（回测主循环接入账本分红）
# ---------------------------------------------------------------------------


class TestDividendInBacktest:
    def test_a_share_dividend_credited_before_trading(
        self, maotai_env, conn, engine: BacktestEngine
    ) -> None:
        """ex_date=01-08 现金分红按开盘前持仓入账：100 股 x 1.00 = 100.00。"""
        conn.execute(
            """
            INSERT INTO fact_dividend (code, ex_date, pay_date, dividend_per_share, dividend_type)
            VALUES ('600519', '2026-01-08', '2026-01-20', 1.00, '现金')
            """
        )
        result = engine.run(BuyOnce(), START, END)
        row = conn.execute("SELECT amount_cny FROM cash_ledger WHERE change_type='分红'").fetchone()
        assert row is not None
        assert abs(row[0] - 100.0) < 1e-9
        # 01-08 净值 = (789934.90 + 100.00) + 100 x 100.5 = 800084.90
        assert equity_of(result, "acc_value", date(2026, 1, 8)) == Decimal("800084.90")

    def test_hk_dividend_net_of_tax_and_fx(
        self, maotai_env, conn, seed_stock, engine: BacktestEngine
    ) -> None:
        """港股通分红：100 股 x 2.00 HKD x 0.8 x 0.92 = 147.20 CNY 入波段轨。"""
        seed_stock("00700", "HK", "腾讯控股", is_hk_connect=True)
        conn.execute(
            """
            INSERT INTO position
                (position_id, account_id, mode, code, qty, avg_cost, avg_cost_cny,
                 open_date, frozen_qty)
            VALUES ('p-hk', 'acc_swing', 'swing', '00700', 100, 300.0, 275.0, '2026-01-05', 0)
            """
        )
        conn.execute("INSERT INTO fact_fx_rate VALUES ('HKD/CNY', '2026-01-05', 0.92)")
        conn.execute(
            """
            INSERT INTO fact_dividend (code, ex_date, pay_date, dividend_per_share, dividend_type)
            VALUES ('00700', '2026-01-08', '2026-01-30', 2.00, '现金')
            """
        )
        result = engine.run(NeverTrades(), START, END)
        row = conn.execute(
            "SELECT amount_cny FROM cash_ledger WHERE change_type='分红' AND account_id='acc_swing'"
        ).fetchone()
        assert row is not None
        assert abs(row[0] - 147.20) < 1e-9
        # 波段轨现金 = 200000 + 147.20（00700 无行情，市值计 0）
        assert equity_of(result, "acc_swing", date(2026, 1, 8)) == Decimal("200147.20")

    def test_ex_date_buy_not_entitled(self, maotai_env, conn, engine: BacktestEngine) -> None:
        """除息日当日买入不享受当日分红（分红按开盘前持仓入账）。"""

        class BuyOnExDate:
            def __init__(self) -> None:
                self._done = False

            def on_bar(self, ctx: StrategyContext) -> list[OrderIntent]:
                if self._done or ctx.trade_date < date(2026, 1, 8):
                    return []
                self._done = True
                return [ctx.make_intent("acc_value", "value", "600519", "SH", OrderSide.BUY, 100)]

        conn.execute(
            """
            INSERT INTO fact_dividend (code, ex_date, pay_date, dividend_per_share, dividend_type)
            VALUES ('600519', '2026-01-08', '2026-01-20', 1.00, '现金')
            """
        )
        engine.run(BuyOnExDate(), START, END)
        assert (
            conn.execute("SELECT count(*) FROM cash_ledger WHERE change_type='分红'").fetchone()[0]
            == 0
        )


# ---------------------------------------------------------------------------
# 黄金数据集真实回放（任务卡：成交记录手工核对）
# ---------------------------------------------------------------------------


class TestGoldenMaotai:
    @pytest.fixture()
    def golden_conn(self, conn):
        """加载黄金茅台 3 年真实日线进内存库。"""
        import pandas as pd

        df = pd.read_parquet(GOLDEN_DIR / "SH_600519_daily_bar.parquet")
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
        conn.register("golden_df", df)
        conn.execute(
            """
            INSERT INTO fact_daily_bar
            SELECT code, trade_date, open, high, low, close, volume, amount,
                   adj_factor, turnover_rate
            FROM golden_df
            """
        )
        conn.unregister("golden_df")
        conn.execute(
            """
            INSERT INTO dim_stock (code, market, name, industry, list_date, is_hk_connect)
            VALUES ('600519', 'SH', '贵州茅台', '白酒', '2001-08-27', false)
            """
        )
        return conn

    def test_golden_2023_buy_and_hold(self, golden_conn, engine: BacktestEngine) -> None:
        """2023 全年回放买入持有，成交价与末日净值按真实数据手工核对。

        手工核对依据（parquet 实读）：
        - 回测首日 2023-01-03 已能见 2022 年数据（黄金集自 2022-01-04 起），
          BuyOnce 于 01-03 决策买入；
        - 01-03 开盘价 1531.74 x 1.001 = 1533.27174 -> 1533.27；
        - 成交额 153327.00；佣金 153327x0.00025=38.33175->38.33，
          过户费 153327x0.00001=1.53327->1.53；费用合计 39.86；
        - 末日 2023-12-29 收盘 1568.47；
          净值 = (800000-153366.86) + 100x1568.47 = 803480.14。
        """
        result = engine.run(BuyOnce(), date(2023, 1, 1), date(2023, 12, 31))
        assert len(result.trades) == 1
        trade = result.trades[0]
        assert trade.trade_date == date(2023, 1, 3)
        assert trade.price == Decimal("1533.27")
        assert trade.amount_cny == Decimal("-153366.86")

        last = date(2023, 12, 29)
        assert equity_of(result, "acc_value", last) == Decimal("803480.14")
        m = result.metrics("acc_value")
        assert m["trading_days"] == 242
        # 基期 = 首日净值（01-03 买入后：646633.14 + 100x1530.69 = 799702.14）
        assert m["total_return"] == pytest.approx(803480.14 / 799702.14 - 1, abs=1e-6)
