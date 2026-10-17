"""P4.6 Brinson 装配层单测（内存库构造账本/行情/市值，手工核对装配口径）。"""

from __future__ import annotations

import uuid
from datetime import date

import duckdb
import pytest

from viqtrader.attribution.brinson import compute_brinson
from viqtrader.attribution.loader import UNKNOWN_INDUSTRY, load_brinson_input

START = date(2026, 1, 1)
END = date(2026, 1, 31)
D0 = "2025-12-31"  # ≤ start 的最近行情日
D1 = "2026-01-30"  # ≤ end 的最近行情日


# ---------------------------------------------------------------------------
# 种子工具
# ---------------------------------------------------------------------------


def _stock(conn: duckdb.DuckDBPyConnection, code: str, market: str, industry: str | None) -> None:
    conn.execute(
        """
        INSERT INTO dim_stock (code, market, name, industry, list_date, is_hk_connect)
        VALUES (?, ?, ?, ?, '2000-01-01', FALSE)
        """,
        [code, market, f"测试{code}", industry],
    )


def _bar(conn: duckdb.DuckDBPyConnection, code: str, trade_date: str, close: float) -> None:
    conn.execute(
        """
        INSERT INTO fact_daily_bar
            (code, trade_date, open, high, low, close, volume, amount, adj_factor)
        VALUES (?, ?, ?, ?, ?, ?, 1000000, ?, 1.0)
        """,
        [code, trade_date, close, close, close, close, close * 1_000_000],
    )


def _mcap(conn: duckdb.DuckDBPyConnection, code: str, trade_date: str, market_cap: float) -> None:
    conn.execute(
        "INSERT INTO fact_valuation (code, trade_date, market_cap) VALUES (?, ?, ?)",
        [code, trade_date, market_cap],
    )


def _trade(
    conn: duckdb.DuckDBPyConnection,
    account_id: str,
    code: str,
    side: str,
    qty: int,
    amount_cny: float,
    trade_date: str,
) -> None:
    conn.execute(
        """
        INSERT INTO trade_log (trade_id, account_id, mode, code, side, qty, price,
                               amount_cny, trade_date)
        VALUES (?, ?, 'value', ?, ?, ?, ?, ?, ?)
        """,
        [
            str(uuid.uuid4()),
            account_id,
            code,
            side,
            qty,
            abs(amount_cny) / qty,
            amount_cny,
            trade_date,
        ],
    )


def _cash(
    conn: duckdb.DuckDBPyConnection,
    account_id: str,
    trade_date: str,
    amount_cny: float,
    balance_cny: float,
) -> None:
    conn.execute(
        """
        INSERT INTO cash_ledger (entry_id, account_id, trade_date, change_type,
                                 amount_cny, balance_cny)
        VALUES (?, ?, ?, '买入', ?, ?)
        """,
        [str(uuid.uuid4()), account_id, trade_date, amount_cny, balance_cny],
    )


def _index(conn: duckdb.DuckDBPyConnection, trade_date: str, close: float) -> None:
    conn.execute(
        """
        INSERT INTO fact_index_bar (code, trade_date, open, high, low, close, volume, amount)
        VALUES ('000300', ?, ?, ?, ?, ?, 1000000, 1e9)
        """,
        [trade_date, close, close, close, close],
    )


def _seed_benchmark(conn: duckdb.DuckDBPyConnection) -> None:
    """基准 universe：食品 1 只 + 银行 2 只（市值 1:1:3），HK 一只验证排除。"""
    _stock(conn, "600519", "SH", "食品饮料")
    _stock(conn, "600000", "SH", "银行")
    _stock(conn, "000002", "SZ", "银行")
    _stock(conn, "00700", "HK", "互联网")  # 港股通标的：v1 不入基准 universe
    _bar(conn, "600519", D0, 150.0)
    _bar(conn, "600519", D1, 165.0)  # +10%
    _bar(conn, "600000", D0, 10.0)
    _bar(conn, "600000", D1, 10.5)  # +5%
    _bar(conn, "000002", D0, 20.0)
    _bar(conn, "000002", D1, 20.2)  # +1%
    _bar(conn, "00700", D0, 300.0)
    _bar(conn, "00700", D1, 330.0)
    _mcap(conn, "600519", D0, 1e9)
    _mcap(conn, "600000", D0, 1e9)
    _mcap(conn, "000002", D0, 3e9)
    _mcap(conn, "00700", D0, 5e9)


def _seed_portfolio(conn: duckdb.DuckDBPyConnection) -> None:
    """acc_value：期初持 600519 1000 股（成本 15 万），区间内新建 000002。"""
    _trade(conn, "acc_value", "600519", "BUY", 1000, -150000.0, "2025-12-01")
    _cash(conn, "acc_value", "2025-12-01", -150000.0, 650000.0)
    _trade(conn, "acc_value", "000002", "BUY", 100, -1000.0, "2026-01-15")
    _cash(conn, "acc_value", "2026-01-15", -1000.0, 649000.0)


# ---------------------------------------------------------------------------
# 手工核对主用例
# ---------------------------------------------------------------------------


class TestAssemblyHandChecked:
    """食品持仓 18.75% + 现金 81.25%；基准食品 0.2/银行 0.8。"""

    def test_segments_and_weights(self, conn: duckdb.DuckDBPyConnection) -> None:
        _seed_benchmark(conn)
        _seed_portfolio(conn)
        _index(conn, D0, 5000.0)
        _index(conn, D1, 5100.0)

        bundle = load_brinson_input(conn, "acc_value", START, END)
        assert bundle is not None
        assert bundle.cash_weight == pytest.approx(0.8125)  # 65 万 / 80 万

        seg = {s.segment: s for s in bundle.segments}
        food = seg["食品饮料"]
        assert food.weight_portfolio == pytest.approx(0.1875)  # 15 万 / 80 万
        assert food.return_portfolio == pytest.approx(0.10)
        assert food.weight_benchmark == pytest.approx(0.2)  # 1e9 / 5e9
        assert food.return_benchmark == pytest.approx(0.10)

        bank = seg["银行"]  # 未持有段：wp=0，rp 按约定填 rb
        assert bank.weight_portfolio == 0.0
        assert bank.weight_benchmark == pytest.approx(0.8)  # 4e9 / 5e9
        assert bank.return_benchmark == pytest.approx(0.02)  # (1e9*.05+3e9*.01)/4e9
        assert bank.return_portfolio == pytest.approx(0.02)

        # 区间新建仓单列，且不进组合段（银行 wp 仍为 0）
        assert [(n.code, n.open_date) for n in bundle.new_positions] == [("000002", "2026-01-15")]
        assert bundle.index_return == pytest.approx(0.02)  # 5100/5000-1
        assert bundle.dropped_codes == ()
        # HK 不入 universe，也不计剔除（universe 仅 A 股 3 只全部有效）
        assert bundle.notes == ()

    def test_bundle_feeds_compute_brinson(self, conn: duckdb.DuckDBPyConnection) -> None:
        """接线：装配结果直接喂纯函数，恒等式成立。"""
        _seed_benchmark(conn)
        _seed_portfolio(conn)
        bundle = load_brinson_input(conn, "acc_value", START, END)
        assert bundle is not None
        result = compute_brinson(
            account_id=bundle.account_id,
            start=bundle.start,
            end=bundle.end,
            segments=bundle.segments,
            cash_weight=bundle.cash_weight,
            index_return=bundle.index_return,
            new_positions=bundle.new_positions,
        )
        assert result is not None
        total = result.total_allocation + result.total_selection + result.total_interaction
        assert total == pytest.approx(result.active_return, abs=1e-12)


# ---------------------------------------------------------------------------
# 边界与口径
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_ledger_returns_none(self, conn: duckdb.DuckDBPyConnection) -> None:
        assert load_brinson_input(conn, "acc_value", START, END) is None

    def test_unknown_account_raises(self, conn: duckdb.DuckDBPyConnection) -> None:
        with pytest.raises(ValueError, match="账户不存在"):
            load_brinson_input(conn, "acc_ghost", START, END)

    def test_invalid_period_raises(self, conn: duckdb.DuckDBPyConnection) -> None:
        with pytest.raises(ValueError, match="归因区间非法"):
            load_brinson_input(conn, "acc_value", END, START)

    def test_industry_null_grouped_unknown(self, conn: duckdb.DuckDBPyConnection) -> None:
        """行业 NULL 的持仓归入「未分类」段。"""
        _seed_benchmark(conn)
        _stock(conn, "600999", "SH", None)
        _bar(conn, "600999", D0, 10.0)
        _bar(conn, "600999", D1, 11.0)
        _trade(conn, "acc_value", "600999", "BUY", 1000, -10000.0, "2025-12-01")
        _cash(conn, "acc_value", "2025-12-01", -10000.0, 790000.0)

        bundle = load_brinson_input(conn, "acc_value", START, END)
        assert bundle is not None
        seg = {s.segment: s for s in bundle.segments}
        assert UNKNOWN_INDUSTRY in seg
        assert seg[UNKNOWN_INDUSTRY].return_portfolio == pytest.approx(0.10)

    def test_holding_without_price_dropped(self, conn: duckdb.DuckDBPyConnection) -> None:
        """期初无行情持仓剔除进 dropped_codes，现金权重按调整后总资产归一。"""
        _seed_benchmark(conn)
        _stock(conn, "600998", "SH", "食品饮料")
        _trade(conn, "acc_value", "600998", "BUY", 1000, -10000.0, "2025-12-01")
        _cash(conn, "acc_value", "2025-12-01", -10000.0, 790000.0)
        # 600998 无任何行情

        bundle = load_brinson_input(conn, "acc_value", START, END)
        assert bundle is not None
        assert bundle.dropped_codes == ("600998",)
        assert bundle.cash_weight == pytest.approx(1.0)  # 剔除后全现金
        assert any("600998" in n for n in bundle.notes)

    def test_index_return_none_when_table_empty(self, conn: duckdb.DuckDBPyConnection) -> None:
        _seed_benchmark(conn)
        _seed_portfolio(conn)
        bundle = load_brinson_input(conn, "acc_value", START, END)
        assert bundle is not None
        assert bundle.index_return is None

    def test_benchmark_empty_returns_none(self, conn: duckdb.DuckDBPyConnection) -> None:
        """无市值数据（真实库现状）：基准 universe 空 → None。"""
        _stock(conn, "600519", "SH", "食品饮料")
        _bar(conn, "600519", D0, 150.0)
        _bar(conn, "600519", D1, 165.0)
        _trade(conn, "acc_value", "600519", "BUY", 1000, -150000.0, "2025-12-01")
        _cash(conn, "acc_value", "2025-12-01", -150000.0, 650000.0)
        assert load_brinson_input(conn, "acc_value", START, END) is None

    def test_benchmark_missing_price_counted_in_notes(
        self, conn: duckdb.DuckDBPyConnection
    ) -> None:
        """基准 universe 中缺行情的标的剔除计数入 notes。"""
        _seed_benchmark(conn)
        _seed_portfolio(conn)
        _stock(conn, "601997", "SH", "银行")  # 无市值无行情 → 剔除
        bundle = load_brinson_input(conn, "acc_value", START, END)
        assert bundle is not None
        assert any("剔除 1 只" in n for n in bundle.notes)


@pytest.mark.parametrize("account_id", ["acc_swing"])
class TestSwingAccount:
    def test_swing_account_assembly(self, conn: duckdb.DuckDBPyConnection, account_id: str) -> None:
        """波段子账本同样可装配（双轨共用）。"""
        _seed_benchmark(conn)
        _trade(conn, account_id, "600000", "BUY", 10000, -100000.0, "2025-12-01")
        _cash(conn, account_id, "2025-12-01", -100000.0, 100000.0)
        bundle = load_brinson_input(conn, account_id, START, END)
        assert bundle is not None
        seg = {s.segment: s for s in bundle.segments}
        assert seg["银行"].weight_portfolio == pytest.approx(0.5)  # 10 万 / 20 万
        assert seg["银行"].return_portfolio == pytest.approx(0.05)
