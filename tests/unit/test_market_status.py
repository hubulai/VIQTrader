"""P2 market_status 预计算测试。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from viqtrader.engine.market_status import (
    get_market_status,
    infer_board,
    refresh_market_status,
)


class TestInferBoard:
    def test_main_board(self) -> None:
        assert infer_board("600519") == "MAIN"
        assert infer_board("000858") == "MAIN"

    def test_chinext(self) -> None:
        assert infer_board("300750") == "CHINEXT"

    def test_star(self) -> None:
        assert infer_board("688981") == "STAR"

    def test_bse(self) -> None:
        assert infer_board("830799") == "BSE"
        assert infer_board("430047") == "BSE"

    def test_hk(self) -> None:
        assert infer_board("00700") == "HK"


class TestRefreshMarketStatus:
    def test_main_board_limits(self, conn, seed_stock, seed_bar, config) -> None:
        seed_stock("600519", "SH", "贵州茅台")
        seed_bar("600519", "2026-01-05", close=100.0)
        seed_bar("600519", "2026-01-06")  # 当日 bar 存在 → 非停牌
        n = refresh_market_status(conn, config.as_dict(), date(2026, 1, 6))
        assert n == 1
        ms = get_market_status(conn, "600519", date(2026, 1, 6))
        assert ms is not None
        assert ms.board == "MAIN"
        assert ms.up_limit_price == 110.00  # 100 x 1.10
        assert ms.down_limit_price == 90.00
        assert ms.is_suspended is False
        assert ms.is_st is False

    def test_round_to_cent(self, conn, seed_stock, seed_bar, config) -> None:
        seed_stock("600519", "SH", "贵州茅台")
        seed_bar("600519", "2026-01-05", close=10.03)
        seed_bar("600519", "2026-01-06")
        refresh_market_status(conn, config.as_dict(), date(2026, 1, 6))
        ms = get_market_status(conn, "600519", date(2026, 1, 6))
        assert ms.up_limit_price == Decimal("11.03")  # 10.03 x 1.10 = 11.033 → 四舍五入 11.03
        assert ms.down_limit_price == Decimal("9.03")  # 10.03 x 0.90 = 9.027 → 9.03

    def test_st_uses_5pct(self, conn, seed_stock, seed_bar, config) -> None:
        seed_stock("600519", "SH", "ST 测试")
        seed_bar("600519", "2026-01-05", close=10.0)
        seed_bar("600519", "2026-01-06")
        refresh_market_status(conn, config.as_dict(), date(2026, 1, 6))
        ms = get_market_status(conn, "600519", date(2026, 1, 6))
        assert ms.is_st is True
        assert ms.up_limit_price == 10.50
        assert ms.down_limit_price == 9.50

    def test_chinext_20pct(self, conn, seed_stock, seed_bar, config) -> None:
        seed_stock("300750", "SZ", "宁德时代")
        seed_bar("300750", "2026-01-05", close=50.0)
        seed_bar("300750", "2026-01-06")
        refresh_market_status(conn, config.as_dict(), date(2026, 1, 6))
        ms = get_market_status(conn, "300750", date(2026, 1, 6))
        assert ms.board == "CHINEXT"
        assert ms.up_limit_price == 60.00
        assert ms.down_limit_price == 40.00

    def test_hk_no_limits(self, conn, seed_stock, seed_bar, config) -> None:
        seed_stock("00700", "HK", "腾讯控股", is_hk_connect=True)
        seed_bar("00700", "2026-01-05", close=300.0)
        seed_bar("00700", "2026-01-06")
        refresh_market_status(conn, config.as_dict(), date(2026, 1, 6))
        ms = get_market_status(conn, "00700", date(2026, 1, 6))
        assert ms.board == "HK"
        assert ms.up_limit_price is None
        assert ms.down_limit_price is None
        assert ms.is_hk_connect is True

    def test_suspended_when_no_bar(self, conn, seed_stock, seed_bar, config) -> None:
        seed_stock("600519", "SH", "贵州茅台")
        seed_bar("600519", "2026-01-05", close=100.0)
        # 2026-01-06 无 bar → 停牌
        refresh_market_status(conn, config.as_dict(), date(2026, 1, 6))
        ms = get_market_status(conn, "600519", date(2026, 1, 6))
        assert ms.is_suspended is True

    def test_suspended_when_zero_volume(self, conn, seed_stock, seed_bar, config) -> None:
        seed_stock("600519", "SH", "贵州茅台")
        seed_bar("600519", "2026-01-05", close=100.0)
        seed_bar("600519", "2026-01-06", volume=0)
        refresh_market_status(conn, config.as_dict(), date(2026, 1, 6))
        ms = get_market_status(conn, "600519", date(2026, 1, 6))
        assert ms.is_suspended is True

    def test_board_backfilled_into_dim_stock(self, conn, seed_stock, seed_bar, config) -> None:
        seed_stock("688981", "SH", "中芯国际")
        seed_bar("688981", "2026-01-06")
        refresh_market_status(conn, config.as_dict(), date(2026, 1, 6))
        board = conn.execute("SELECT board FROM dim_stock WHERE code = '688981'").fetchone()[0]
        assert board == "STAR"

    def test_rerun_is_idempotent(self, conn, seed_stock, seed_bar, config) -> None:
        seed_stock("600519", "SH", "贵州茅台")
        seed_bar("600519", "2026-01-05", close=100.0)
        seed_bar("600519", "2026-01-06")
        refresh_market_status(conn, config.as_dict(), date(2026, 1, 6))
        refresh_market_status(conn, config.as_dict(), date(2026, 1, 6))
        count = conn.execute(
            "SELECT count(*) FROM market_status WHERE trade_date = '2026-01-06'"
        ).fetchone()[0]
        assert count == 1

    def test_missing_stock_returns_none(self, conn, config) -> None:
        refresh_market_status(conn, config.as_dict(), date(2026, 1, 6))
        assert get_market_status(conn, "999999", date(2026, 1, 6)) is None
