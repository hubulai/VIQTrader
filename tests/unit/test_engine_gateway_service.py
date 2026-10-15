"""P2 网关撮合 + ExecutionService 端到端管线测试。"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from viqtrader.engine.execution.gateway import DryRunGateway, SimulatedGateway
from viqtrader.engine.execution.service import ExecutionService
from viqtrader.engine.rules.account import (
    CashSufficiencyRule,
    ModeIsolationRule,
    PositionLimitRule,
    QuotaRule,
)
from viqtrader.engine.rules.market import (
    HKConnectRule,
    LotSizeRule,
    PriceLimitRule,
    SuspensionRule,
    T1Rule,
    TradingDayRule,
)
from viqtrader.engine.types import (
    IntentStatus,
    OrderIntent,
    OrderSide,
    OrderType,
)

TRADING_DATE = date(2026, 1, 6)


def make_intent(
    *,
    side: OrderSide = OrderSide.BUY,
    market: str = "SH",
    code: str = "600519",
    order_type: OrderType = OrderType.MARKET,
    qty: int = 100,
    price: Decimal | None = None,
    mode: str = "value",
    account_id: str = "acc_value",
    thesis_id: str | None = "t-001",
) -> OrderIntent:
    return OrderIntent(
        intent_id=str(uuid.uuid4()),
        account_id=account_id,
        mode=mode,
        code=code,
        market=market,
        side=side,
        order_type=order_type,
        qty=qty,
        price=price,
        submitted_at=datetime(2026, 1, 6, 9, 30),
        thesis_id=thesis_id,
    )


def default_rules() -> list:
    return [
        T1Rule(),
        TradingDayRule(),
        SuspensionRule(),
        PriceLimitRule(),
        LotSizeRule(),
        HKConnectRule(),
        CashSufficiencyRule(),
        PositionLimitRule(),
        ModeIsolationRule(),
        QuotaRule(),
    ]


def seed_env(conn, seed_stock, seed_bar, config) -> None:
    """标准环境：茅台前收 100、当日开 100.5 高 101 低 99.5 收 100.8。"""
    seed_stock("600519", "SH", "贵州茅台")
    seed_bar("600519", "2026-01-05", close=100.0)
    seed_bar("600519", "2026-01-06", open_=100.5, high=101.0, low=99.5, close=100.8)
    from viqtrader.engine.market_status import refresh_market_status

    refresh_market_status(conn, config.as_dict(), TRADING_DATE)


class TestSimulatedGateway:
    def test_market_buy_slippage(self, conn, seed_stock, seed_bar, config) -> None:
        seed_env(conn, seed_stock, seed_bar, config)
        gw = SimulatedGateway(conn, config)
        result = gw.fill(make_intent(), TRADING_DATE)
        assert result.status.value == "FILLED"
        # 10bp 滑点：100.5 x (1 + 0.001) = 100.6005 → 四舍五入 100.60
        assert result.fill_price == Decimal("100.60")

    def test_market_sell_slippage(self, conn, seed_stock, seed_bar, config) -> None:
        seed_env(conn, seed_stock, seed_bar, config)
        gw = SimulatedGateway(conn, config)
        result = gw.fill(make_intent(side=OrderSide.SELL), TRADING_DATE)
        # 100.5 x (1 - 0.001) = 100.3995 → 100.40
        assert result.fill_price == Decimal("100.40")

    def test_market_buy_blocked_at_up_limit(self, conn, seed_stock, seed_bar, config) -> None:
        seed_stock("300750", "SZ", "宁德时代")
        seed_bar("300750", "2026-01-05", close=100.0)
        seed_bar("300750", "2026-01-06", open_=120.0)  # 开盘即 20% 涨停
        from viqtrader.engine.market_status import refresh_market_status

        refresh_market_status(conn, config.as_dict(), TRADING_DATE)
        gw = SimulatedGateway(conn, config)
        result = gw.fill(
            make_intent(code="300750", market="SZ", mode="swing", account_id="acc_swing"),
            TRADING_DATE,
        )
        assert result.status.value == "NOT_FILLED"
        assert "涨停" in result.message

    def test_limit_buy_fills_at_better_of_open_or_limit(
        self, conn, seed_stock, seed_bar, config
    ) -> None:
        seed_env(conn, seed_stock, seed_bar, config)
        gw = SimulatedGateway(conn, config)
        # 限价 100.2 < 开盘 100.5，当日低 99.5 触及 → 按限价成交
        result = gw.fill(
            make_intent(order_type=OrderType.LIMIT, price=Decimal("100.20")), TRADING_DATE
        )
        assert result.fill_price == Decimal("100.20")

    def test_limit_buy_not_touched(self, conn, seed_stock, seed_bar, config) -> None:
        seed_env(conn, seed_stock, seed_bar, config)
        gw = SimulatedGateway(conn, config)
        result = gw.fill(
            make_intent(order_type=OrderType.LIMIT, price=Decimal("99.00")), TRADING_DATE
        )
        assert result.status.value == "NOT_FILLED"

    def test_no_bar_not_filled(self, conn, seed_stock, seed_bar, config) -> None:
        seed_env(conn, seed_stock, seed_bar, config)
        gw = SimulatedGateway(conn, config)
        result = gw.fill(make_intent(code="000858"), TRADING_DATE)  # 未插入 bar
        assert result.status.value == "NOT_FILLED"

    def test_stop_unsupported(self, conn, seed_stock, seed_bar, config) -> None:
        seed_env(conn, seed_stock, seed_bar, config)
        gw = SimulatedGateway(conn, config)
        result = gw.fill(make_intent(order_type=OrderType.STOP), TRADING_DATE)
        assert result.status.value == "UNSUPPORTED"


class TestDryRunGateway:
    def test_dry_run_market_fills_at_open(self, conn, seed_stock, seed_bar, config) -> None:
        seed_env(conn, seed_stock, seed_bar, config)
        gw = DryRunGateway(conn, config)
        result = gw.fill(make_intent(), TRADING_DATE)
        assert result.status.value == "FILLED"
        assert result.fill_price == Decimal("100.60")  # 同滑点口径

    def test_dry_run_limit_respects_price(self, conn, seed_stock, seed_bar, config) -> None:
        seed_env(conn, seed_stock, seed_bar, config)
        gw = DryRunGateway(conn, config)
        ok = gw.fill(make_intent(order_type=OrderType.LIMIT, price=Decimal("101.00")), TRADING_DATE)
        assert ok.fill_price == Decimal("101.00")
        rejected = gw.fill(
            make_intent(order_type=OrderType.LIMIT, price=Decimal("99.00")), TRADING_DATE
        )
        assert rejected.status.value == "NOT_FILLED"


class TestExecutionServiceEndToEnd:
    def test_market_buy_fills(self, conn, seed_stock, seed_bar, config) -> None:
        seed_env(conn, seed_stock, seed_bar, config)
        svc = ExecutionService(conn, config, default_rules(), SimulatedGateway(conn, config))
        intent = make_intent()
        result = svc.submit_order(intent, TRADING_DATE)
        assert result.status == IntentStatus.FILLED
        assert result.trade is not None

        row = conn.execute(
            "SELECT status, filled_price FROM order_intent WHERE intent_id = ?",
            [intent.intent_id],
        ).fetchone()
        assert row[0] == "FILLED"
        assert row[1] == 100.60

        # trade_log 关联 + rule_check_log 完整 + 现金流水
        assert (
            conn.execute(
                "SELECT count(*) FROM trade_log WHERE intent_id = ?", [intent.intent_id]
            ).fetchone()[0]
            == 1
        )
        checks = conn.execute(
            "SELECT phase, rule_type, passed FROM rule_check_log WHERE intent_id = ?"
            " ORDER BY phase",
            [intent.intent_id],
        ).fetchall()
        assert len(checks) == 10
        assert (
            conn.execute(
                "SELECT count(*) FROM cash_ledger WHERE account_id='acc_value'"
            ).fetchone()[0]
            == 1
        )

    def test_lot_size_rejection(self, conn, seed_stock, seed_bar, config) -> None:
        seed_env(conn, seed_stock, seed_bar, config)
        svc = ExecutionService(conn, config, default_rules(), SimulatedGateway(conn, config))
        intent = make_intent(qty=150)
        result = svc.submit_order(intent, TRADING_DATE)
        assert result.status == IntentStatus.REJECTED
        assert "LOT_SIZE" in result.rejection_reason
        row = conn.execute(
            "SELECT status, rejection_reason FROM order_intent WHERE intent_id = ?",
            [intent.intent_id],
        ).fetchone()
        assert row[0] == "REJECTED"
        assert "整百" in row[1]
        # 拒绝路径不产生任何资金变动
        assert conn.execute("SELECT count(*) FROM cash_ledger").fetchone()[0] == 0

    def test_suspension_rejection(self, conn, seed_stock, seed_bar, config) -> None:
        seed_stock("000858", "SZ", "五粮液")
        seed_bar("000858", "2026-01-05", close=100.0)
        # 000858 当日无 bar → 停牌
        from viqtrader.engine.market_status import refresh_market_status

        refresh_market_status(conn, config.as_dict(), TRADING_DATE)
        svc = ExecutionService(conn, config, default_rules(), SimulatedGateway(conn, config))
        intent = make_intent(code="000858", market="SZ")
        result = svc.submit_order(intent, TRADING_DATE)
        assert result.status == IntentStatus.REJECTED
        assert "SUSPENSION" in result.rejection_reason

    def test_t1_sell_blocked_same_day(self, conn, seed_stock, seed_bar, config) -> None:
        """当日买入后当日卖出被 T+1 拒绝。"""
        seed_env(conn, seed_stock, seed_bar, config)
        svc = ExecutionService(conn, config, default_rules(), SimulatedGateway(conn, config))
        buy = make_intent()
        assert svc.submit_order(buy, TRADING_DATE).status == IntentStatus.FILLED
        sell = make_intent(side=OrderSide.SELL, thesis_id=None)
        result = svc.submit_order(sell, TRADING_DATE)
        assert result.status == IntentStatus.REJECTED
        assert "T1" in result.rejection_reason

    def test_quota_warn_writes_risk_event(self, conn, seed_stock, seed_bar, config) -> None:
        """双轨配额软限制：超限不阻断，写 WARN risk_event。"""
        seed_env(conn, seed_stock, seed_bar, config)
        # 价值轨加一笔大持仓，使占比漂到 0.9
        conn.execute(
            """
            INSERT INTO position
                (position_id, account_id, mode, code, qty, avg_cost, avg_cost_cny,
                 open_date, frozen_qty)
            VALUES ('p-big', 'acc_value', 'value', '000858', 70000, 10.0, 10.0,
                    '2025-12-01', 0)
            """
        )
        seed_stock("000858", "SZ", "五粮液")
        seed_bar("000858", "2026-01-06", close=10.0)
        # 价值轨市值 700000 + 现金 800000 = 1500000；波段 200000 → 占比 0.882 > 0.85
        svc = ExecutionService(conn, config, default_rules(), SimulatedGateway(conn, config))
        intent = make_intent()
        result = svc.submit_order(intent, TRADING_DATE)
        assert result.status == IntentStatus.FILLED
        assert len(result.warnings) == 1
        assert result.warnings[0].rule_id == "QUOTA"
        ev = conn.execute("SELECT severity, decision, rule_key FROM risk_event").fetchone()
        assert ev == ("WARN", "ALERT", "QUOTA")

    def test_hk_buy_requires_connect_list(self, conn, seed_stock, seed_bar, config) -> None:
        """港股通调出名单后买入被拒。"""
        seed_stock("00700", "HK", "腾讯控股", is_hk_connect=True)
        seed_bar("00700", "2026-01-05", close=300.0)
        seed_bar("00700", "2026-01-06", open_=300.5, high=302, low=299, close=301)
        from viqtrader.engine.market_status import refresh_market_status

        refresh_market_status(conn, config.as_dict(), TRADING_DATE)
        svc = ExecutionService(conn, config, default_rules(), SimulatedGateway(conn, config))

        # 在名单内可买（600 股 x ~300.6 ≈ 180,360 < 波段轨现金 200,000）
        in_list = make_intent(
            market="HK", code="00700", qty=600, account_id="acc_swing", mode="swing"
        )
        assert svc.submit_order(in_list, TRADING_DATE).status == IntentStatus.FILLED

        # 模拟调出名单
        conn.execute("UPDATE market_status SET is_hk_connect = FALSE WHERE code='00700'")
        out_list = make_intent(
            market="HK", code="00700", qty=100, account_id="acc_swing", mode="swing"
        )
        result = svc.submit_order(out_list, TRADING_DATE)
        assert result.status == IntentStatus.REJECTED
        assert "HK_CONNECT" in result.rejection_reason

    def test_cash_sufficiency_rejection(self, conn, seed_stock, seed_bar, config) -> None:
        """资金不足：Phase 2 CashSufficiencyRule 直接拒绝（FILLED 重检兜底在 ledger 测试覆盖）。"""
        seed_env(conn, seed_stock, seed_bar, config)
        svc = ExecutionService(conn, config, default_rules(), SimulatedGateway(conn, config))
        # 先让现金几乎耗尽：手工入账一笔大额支出
        conn.execute(
            """
            INSERT INTO cash_ledger (entry_id, account_id, trade_date, change_type,
                                     amount_cny, balance_cny)
            VALUES ('drain', 'acc_value', '2026-01-05', '费用', -795000, 5000)
            """
        )
        # 校验时：needs 100x100.6 ≈ 10060 > 5000 → CashSufficiency 直接拒绝
        intent = make_intent(qty=100)
        result = svc.submit_order(intent, TRADING_DATE)
        assert result.status == IntentStatus.REJECTED
        assert "CASH_SUFFICIENCY" in result.rejection_reason
