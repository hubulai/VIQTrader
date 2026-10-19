"""P6.4 复盘报告页单测：Brinson 归因 + 持仓变动 + 成交明细。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import duckdb
import pytest
from fastapi.testclient import TestClient

from viqtrader.config.loader import Config
from viqtrader.store.db import connect
from viqtrader.store.migrate import migrate
from viqtrader.web.app import create_app
from viqtrader.web.services.review import build_review, list_accounts

START = date(2026, 7, 1)
END = date(2026, 9, 30)
REVIEW_START = date(2026, 7, 1)
REVIEW_END = date(2026, 9, 30)


@pytest.fixture()
def config(tmp_path: Path) -> Config:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "default.yaml").write_text(
        f"platform:\n  data_dir: {(tmp_path / 'data').as_posix()}\n  duckdb_file: test.duckdb\n",
        encoding="utf-8",
    )
    return Config.load(config_dir)


@pytest.fixture()
def conn(config: Config) -> Iterator[duckdb.DuckDBPyConnection]:
    c = connect(config)
    migrate(c)
    yield c
    c.close()


@pytest.fixture()
def client(config: Config, conn: duckdb.DuckDBPyConnection) -> Iterator[TestClient]:
    conn.close()
    yield TestClient(create_app(config))


@pytest.fixture()
def seeded_client(config: Config, conn: duckdb.DuckDBPyConnection) -> TestClient:
    """带完整账本种子的 TestClient（先 seed 再 close）。"""
    _seed_full(conn)
    conn.close()
    return TestClient(create_app(config))


def _seed_full(conn: duckdb.DuckDBPyConnection) -> None:
    """完整账本 + 行情 + 基准 universe（供 Brinson 归因）。"""
    conn.execute("INSERT INTO account VALUES ('acc_value', 'value', 'CNY', 500000, 0.8)")

    # 3 只行业分散标的（白酒 / 金融 / 科技）
    for code, name, industry, price_start, price_end, mcap in [
        ("600519", "贵州茅台", "白酒", 1800.0, 1900.0, 2.4e12),
        ("601318", "中国平安", "保险", 50.0, 55.0, 1.0e12),
        ("000001", "平安银行", "银行", 12.0, 11.0, 2.2e11),
    ]:
        conn.execute(
            "INSERT INTO dim_stock (code, market, name, industry, list_date, is_hk_connect) "
            "VALUES (?, 'SH', ?, ?, '2000-01-01', false)",
            [code, name, industry],
        )
        conn.execute(
            "INSERT INTO fact_daily_bar "
            "(code, trade_date, open, high, low, close, volume, adj_factor) "
            "VALUES (?, ?, ?, ?, ?, ?, 1000000, 1.0)",
            [code, START, price_start, price_start, price_start, price_start],
        )
        conn.execute(
            "INSERT INTO fact_daily_bar "
            "(code, trade_date, open, high, low, close, volume, adj_factor) "
            "VALUES (?, ?, ?, ?, ?, ?, 1000000, 1.0)",
            [code, END, price_end, price_end, price_end, price_end],
        )
        # market_cap 基准 universe
        conn.execute(
            "INSERT INTO fact_valuation (code, trade_date, market_cap) VALUES (?, ?, ?)",
            [code, START, mcap],
        )

    # 期初持仓：茅台 100 股（白酒）
    conn.execute(
        """
        INSERT INTO trade_log
            (trade_id, account_id, mode, code, side, qty, price, amount_cny, trade_date)
        VALUES ('t_init', 'acc_value', 'value', '600519', 'BUY', 100, 1800, 180000, '2026-06-15')
        """
    )
    conn.execute(
        """
        INSERT INTO cash_ledger
            (entry_id, account_id, trade_date, change_type, amount_cny, balance_cny)
        VALUES ('c_init', 'acc_value', '2026-06-15', '买入', -180000, 320000)
        """
    )

    # 区间内交易：买入平安银行（新建仓）
    conn.execute(
        """
        INSERT INTO trade_log
            (trade_id, account_id, mode, code, side, qty, price, amount_cny, trade_date)
        VALUES ('t_new', 'acc_value', 'value', '000001', 'BUY', 2000, 12.0, 24000, '2026-08-10')
        """
    )
    conn.execute(
        """
        INSERT INTO cash_ledger
            (entry_id, account_id, trade_date, change_type, amount_cny, balance_cny)
        VALUES ('c_new', 'acc_value', '2026-08-10', '买入', -24000, 296000)
        """
    )

    # 期末持仓快照（2 只）
    conn.execute(
        """
        INSERT INTO position
            (position_id, account_id, mode, code, qty, avg_cost, avg_cost_cny, open_date)
        VALUES ('p1', 'acc_value', 'value', '600519', 100, 1800, 1800, '2026-06-15'),
               ('p2', 'acc_value', 'value', '000001', 2000, 12.0, 12.0, '2026-08-10')
        """
    )

    # 指数基准（沪深300）
    conn.execute(
        "INSERT INTO fact_index_bar "
        "(code, trade_date, open, high, low, close) "
        "VALUES ('000300', ?, 3800, 3800, 3800, 3800)",
        [START],
    )
    conn.execute(
        "INSERT INTO fact_index_bar "
        "(code, trade_date, open, high, low, close) "
        "VALUES ('000300', ?, 4000, 4000, 4000, 4000)",
        [END],
    )


# ---------------------------------------------------------------------------
# 服务层测试
# ---------------------------------------------------------------------------


class TestReviewService:
    def test_list_accounts(self, conn: duckdb.DuckDBPyConnection) -> None:
        _seed_full(conn)
        accounts = list_accounts(conn)
        assert "acc_value" in accounts

    def test_vacuum_account_unavailable(self, conn: duckdb.DuckDBPyConnection) -> None:
        """纯空账本 → brinson unavailable。"""
        conn.execute("INSERT INTO account VALUES ('acc_value', 'value', 'CNY', 500000, 0.8)")
        result = build_review(conn, "acc_value", REVIEW_START, REVIEW_END)
        assert result["brinson"].unavailable is True
        assert "账本为空" in result["brinson"].unavailable_reason

    def test_full_review(self, conn: duckdb.DuckDBPyConnection) -> None:
        """完整账本 → Brinson 可用 + 持仓变动 + 成交明细。"""
        _seed_full(conn)
        result = build_review(conn, "acc_value", REVIEW_START, REVIEW_END)

        # Summary
        s = result["summary"]
        assert s.account_id == "acc_value"
        assert s.start_mv_cny > 0
        assert s.end_mv_cny > 0
        assert s.pnl_cny == pytest.approx(s.end_mv_cny - s.start_mv_cny, rel=1e-9)

        # Brinson 可用
        b = result["brinson"]
        assert b.unavailable is False
        assert len(b.segments) >= 2  # 白酒 + 银行（保险基准段无持仓）
        assert b.active_return == pytest.approx(sum(seg.total for seg in b.segments), abs=1e-9)

        # 新建仓：平安银行 BUY 2026-08-10 ∈ [start, end]
        assert len(b.new_positions) == 1
        assert b.new_positions[0].code == "000001"

        # 持仓变动：期初茅台 100 → 期末茅台 100 + 平安银行 2000
        diff = result["position_diff"]
        status_by_code = {d.code: d.status for d in diff}
        assert status_by_code["600519"] == "holding"
        assert status_by_code["000001"] == "opened"

        # 成交明细：区间内只有 000001（2026-08-10），期初茅台买在 2026-06-15 早于 start
        trades = result["trade_summary"]
        codes = {t.code for t in trades}
        assert codes == {"000001"}


# ---------------------------------------------------------------------------
# 页面测试
# ---------------------------------------------------------------------------


class TestReviewPage:
    def test_no_param_renders_form(self, client: TestClient) -> None:
        """空库 → 仍渲染表单（无账户下拉项）。"""
        resp = client.get("/review")
        assert resp.status_code == 200
        text = resp.text
        assert "复盘报告" in text
        assert '<select name="account">' in text

    def test_full_page(self, seeded_client: TestClient) -> None:
        resp = seeded_client.get("/review")  # 无参数 → 表单页
        assert resp.status_code == 200
        assert "复盘报告" in resp.text

        # 带参数生成报告
        resp = seeded_client.get(
            f"/review?account=acc_value&start={REVIEW_START.isoformat()}&end={REVIEW_END.isoformat()}"
        )
        assert resp.status_code == 200
        text = resp.text
        assert "区间总览" in text
        assert "Brinson 三效应分解" in text
        assert "持仓变动" in text
        assert "成交明细" in text
