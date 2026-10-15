"""P2 LedgerEngine 测试：成交记账 / T+1 结算 / 分红 / 资金重检。"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

import pytest

from viqtrader.engine.ledger.engine import LedgerEngine
from viqtrader.engine.types import (
    FeeBreakdown,
    InsufficientCashAtFill,
    OrderIntent,
    OrderSide,
    OrderType,
)


def make_intent(
    *,
    side: OrderSide = OrderSide.BUY,
    market: str = "SH",
    code: str = "600519",
    qty: int = 100,
    price: Decimal = Decimal("10.00"),
    mode: str = "value",
    account_id: str = "acc_value",
) -> OrderIntent:
    return OrderIntent(
        intent_id=str(uuid.uuid4()),
        account_id=account_id,
        mode=mode,
        code=code,
        market=market,
        side=side,
        order_type=OrderType.LIMIT,
        qty=qty,
        price=price,
        submitted_at=datetime(2026, 1, 6, 9, 30),
    )


def fees(cny: str = "5.00") -> FeeBreakdown:
    total = Decimal(cny)
    return FeeBreakdown(commission=total, stamp_tax=Decimal("0"), transfer_fee=Decimal("0"))


@pytest.fixture()
def ledger(conn, config) -> LedgerEngine:
    return LedgerEngine(conn, config)


class TestBuyFill:
    def test_buy_creates_position_and_cash_out(self, conn, ledger: LedgerEngine) -> None:
        intent = make_intent(qty=100, price=Decimal("10.00"))
        trade = ledger.execute_fill(intent, Decimal("10.00"), fees("5.00"), date(2026, 1, 6))
        assert trade.amount_cny == Decimal("-1005.00")

        pos = conn.execute(
            "SELECT qty, avg_cost, avg_cost_cny, frozen_qty FROM position"
            " WHERE account_id='acc_value' AND code='600519'"
        ).fetchone()
        assert pos == (100, 10.0, 10.0, 100)  # A 股 T+1 当日全额冻结

        assert ledger.get_available_cash("acc_value") == Decimal("800000") - Decimal("1005")

        tl = conn.execute(
            "SELECT side, qty, price, amount_cny, intent_id FROM trade_log"
        ).fetchone()
        assert tl[0] == "BUY" and tl[1] == 100 and tl[2] == 10.0
        assert abs(tl[3] - (-1005.0)) < 1e-9
        assert tl[4] == intent.intent_id

    def test_weighted_avg_cost(self, conn, ledger: LedgerEngine) -> None:
        ledger.execute_fill(
            make_intent(qty=100, price=Decimal("10.00")),
            Decimal("10.00"),
            fees("5.00"),
            date(2026, 1, 6),
        )
        ledger.settle_end_of_day(date(2026, 1, 6))
        ledger.execute_fill(
            make_intent(qty=100, price=Decimal("14.00")),
            Decimal("14.00"),
            fees("5.00"),
            date(2026, 1, 7),
        )
        qty, avg, _avg_cny, frozen = conn.execute(
            "SELECT qty, avg_cost, avg_cost_cny, frozen_qty FROM position"
            " WHERE account_id='acc_value'"
        ).fetchone()
        assert qty == 200
        assert abs(avg - 12.0) < 1e-9
        assert frozen == 100  # 仅第二笔买入冻结

    def test_insufficient_cash_at_fill_rolls_back(self, conn, ledger: LedgerEngine) -> None:
        intent = make_intent(qty=100000, price=Decimal("10.00"))  # 需要 100 万 > 80 万
        with pytest.raises(InsufficientCashAtFill):
            ledger.execute_fill(intent, Decimal("10.00"), fees(), date(2026, 1, 6))
        # 回滚：无持仓、无流水、无 trade_log
        assert conn.execute("SELECT count(*) FROM position").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM cash_ledger").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM trade_log").fetchone()[0] == 0


class TestSellFill:
    def _seed_position(self, conn) -> None:
        conn.execute(
            """
            INSERT INTO position
                (position_id, account_id, mode, code, qty, avg_cost, avg_cost_cny,
                 open_date, frozen_qty)
            VALUES ('p1', 'acc_value', 'value', '600519', 200, 10.0, 10.0, '2026-01-05', 0)
            """
        )

    def test_sell_updates_position_and_cash_in(self, conn, ledger: LedgerEngine) -> None:
        self._seed_position(conn)
        intent = make_intent(side=OrderSide.SELL, qty=100, price=Decimal("12.00"))
        trade = ledger.execute_fill(intent, Decimal("12.00"), fees("7.00"), date(2026, 1, 6))
        # realized_pnl = (12 - 10) x 100 - 7
        assert trade.realized_pnl_cny == Decimal("193.00")
        qty = conn.execute(
            "SELECT qty FROM position WHERE account_id='acc_value' AND code='600519'"
        ).fetchone()[0]
        assert qty == 100
        assert ledger.get_available_cash("acc_value") == Decimal("800000") + Decimal("1193.00")

    def test_full_sell_deletes_position(self, conn, ledger: LedgerEngine) -> None:
        self._seed_position(conn)
        intent = make_intent(side=OrderSide.SELL, qty=200, price=Decimal("10.00"))
        ledger.execute_fill(intent, Decimal("10.00"), fees(), date(2026, 1, 6))
        assert conn.execute("SELECT count(*) FROM position").fetchone()[0] == 0

    def test_sell_beyond_available_rejected(self, conn, ledger: LedgerEngine) -> None:
        self._seed_position(conn)
        conn.execute("UPDATE position SET frozen_qty = 150 WHERE position_id = 'p1'")
        intent = make_intent(side=OrderSide.SELL, qty=100, price=Decimal("10.00"))
        with pytest.raises(ValueError, match="可用持仓不足"):
            ledger.execute_fill(intent, Decimal("10.00"), fees(), date(2026, 1, 6))

    def test_sell_without_position_rejected(self, ledger: LedgerEngine) -> None:
        intent = make_intent(side=OrderSide.SELL, qty=100)
        with pytest.raises(ValueError, match="无持仓"):
            ledger.execute_fill(intent, Decimal("10.00"), fees(), date(2026, 1, 6))


class TestSettlement:
    def test_settle_clears_frozen(self, conn, ledger: LedgerEngine) -> None:
        ledger.execute_fill(make_intent(qty=100), Decimal("10.00"), fees(), date(2026, 1, 6))
        ledger.settle_end_of_day(date(2026, 1, 6))
        frozen = conn.execute(
            "SELECT frozen_qty FROM position WHERE account_id='acc_value'"
        ).fetchone()[0]
        assert frozen == 0

    def test_t1_lifecycle(self, conn, ledger: LedgerEngine) -> None:
        """T+1 全流程：当日买入冻结 → 当日卖被拒 → 日终解冻 → 次日可卖。"""
        ledger.execute_fill(make_intent(qty=100), Decimal("10.00"), fees(), date(2026, 1, 6))
        sell_today = make_intent(side=OrderSide.SELL, qty=100)
        with pytest.raises(ValueError, match="可用持仓不足"):
            ledger.execute_fill(sell_today, Decimal("10.00"), fees(), date(2026, 1, 6))
        ledger.settle_end_of_day(date(2026, 1, 6))
        sell_next = make_intent(side=OrderSide.SELL, qty=100)
        trade = ledger.execute_fill(sell_next, Decimal("10.00"), fees(), date(2026, 1, 7))
        assert trade.side == "SELL"


class TestDividend:
    def test_a_share_dividend(self, conn, ledger: LedgerEngine) -> None:
        conn.execute(
            """
            INSERT INTO position
                (position_id, account_id, mode, code, qty, avg_cost, avg_cost_cny,
                 open_date, frozen_qty)
            VALUES ('p1', 'acc_value', 'value', '600519', 100, 10.0, 10.0, '2026-01-05', 0)
            """
        )
        amounts = ledger.process_dividend("600519", date(2026, 1, 6), Decimal("1.50"), "现金")
        assert amounts == [Decimal("150.00")]
        row = conn.execute(
            "SELECT change_type, amount_cny FROM cash_ledger WHERE account_id='acc_value'"
        ).fetchone()
        assert row[0] == "分红"
        assert abs(row[1] - 150.0) < 1e-9

    def test_hk_dividend_net_of_tax(self, conn, ledger: LedgerEngine) -> None:
        """港股通分红：统一 0.20 税率，按汇率折 CNY。"""
        conn.execute(
            """
            INSERT INTO position
                (position_id, account_id, mode, code, qty, avg_cost, avg_cost_cny,
                 open_date, frozen_qty)
            VALUES ('p2', 'acc_swing', 'swing', '00700', 100, 300.0, 275.0, '2026-01-05', 0)
            """
        )
        conn.execute("INSERT INTO fact_fx_rate VALUES ('HKD/CNY', '2026-01-05', 0.92)")
        amounts = ledger.process_dividend("00700", date(2026, 1, 6), Decimal("2.00"), "现金")
        # 100 股 x 2.0 HKD x 0.8 x 0.92 = 147.20 CNY
        assert len(amounts) == 1
        assert abs(amounts[0] - Decimal("147.20")) < Decimal("0.001")

    def test_both_tracks_credited_independently(self, conn, ledger: LedgerEngine) -> None:
        """同标的两轨各入各的账（双轨隔离红线）。"""
        for pid, acc, mode in (("p1", "acc_value", "value"), ("p2", "acc_swing", "swing")):
            conn.execute(
                f"""
                INSERT INTO position
                    (position_id, account_id, mode, code, qty, avg_cost, avg_cost_cny,
                     open_date, frozen_qty)
                VALUES ('{pid}', '{acc}', '{mode}', '600519', 100, 10.0, 10.0, '2026-01-05', 0)
                """
            )
        amounts = ledger.process_dividend("600519", date(2026, 1, 6), Decimal("1.00"), "现金")
        assert len(amounts) == 2
        rows = conn.execute(
            "SELECT account_id, amount_cny FROM cash_ledger WHERE change_type='分红'"
            " ORDER BY account_id"
        ).fetchall()
        assert {r[0] for r in rows} == {"acc_value", "acc_swing"}

    def test_stock_dividend_not_supported(self, ledger: LedgerEngine) -> None:
        with pytest.raises(NotImplementedError):
            ledger.process_dividend("600519", date(2026, 1, 6), Decimal("1"), "送股")


class TestTrackValue:
    def test_cash_only(self, ledger: LedgerEngine) -> None:
        assert ledger.get_track_value("acc_value", date(2026, 1, 6)) == Decimal("800000")

    def test_with_position(self, conn, ledger: LedgerEngine) -> None:
        conn.execute(
            """
            INSERT INTO position
                (position_id, account_id, mode, code, qty, avg_cost, avg_cost_cny,
                 open_date, frozen_qty)
            VALUES ('p1', 'acc_value', 'value', '600519', 100, 10.0, 10.0, '2026-01-05', 0)
            """
        )
        conn.execute(
            "INSERT INTO fact_daily_bar (code, trade_date, close) "
            "VALUES ('600519', '2026-01-06', 11.0)"
        )
        value = ledger.get_track_value("acc_value", date(2026, 1, 6))
        assert value == Decimal("800000") + Decimal("1100.00")

    def test_unknown_account_raises(self, ledger: LedgerEngine) -> None:
        with pytest.raises(ValueError, match="账户不存在"):
            ledger.get_available_cash("acc_none")
