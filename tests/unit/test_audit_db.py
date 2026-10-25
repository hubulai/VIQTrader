"""P7 audit_db 单元测试（全离线，内存 DuckDB + 合成数据；仅断言逻辑，不校验真实库数值）。"""

from __future__ import annotations

import json
from datetime import date

import duckdb
import pytest

from viqtrader.data.audit import (
    audit_database,
    caliber_balance_violations,
    flow_monotonic_count,
    judge_caliber,
    judge_coverage,
    judge_flow_caliber,
    judge_lookahead,
    judge_market_cap,
    judge_null_rates,
    judge_orphan,
    judge_periods,
)
from viqtrader.store import migrate as _migrate

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def conn() -> duckdb.DuckDBPyConnection:
    c = duckdb.connect(":memory:")
    _migrate.migrate(c)
    return c


def _insert_stock(
    c: duckdb.DuckDBPyConnection, code: str, market: str = "SH", name: str = "测试股"
) -> None:
    c.execute(
        "INSERT INTO dim_stock "
        "(code, market, name, industry, list_date, is_hk_connect, "
        "hk_connect_in_date, hk_connect_out_date) VALUES (?,?,?,?,?,?,?,?)",
        [code, market, name, None, None, False, None, None],
    )


def _insert_financial(
    c: duckdb.DuckDBPyConnection,
    code: str,
    report_period: date,
    ann_date: date | None,
    *,
    revenue: float | None = 100.0,
    net_profit: float | None = 50.0,
    net_margin: float | None = 0.5,
    total_assets: float | None = 1000.0,
    total_equity: float | None = 600.0,
    debt_ratio: float | None = 0.4,
) -> None:
    c.execute(
        "INSERT INTO fact_financial "
        "(code, report_period, ann_date, revenue, net_profit, roe, roa, gross_margin, "
        "net_margin, debt_ratio, ocf, fcf, eps, bps, total_assets, total_equity) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            code,
            report_period,
            ann_date,
            revenue,
            net_profit,
            0.15,  # roe
            None,  # roa（QMT 无字段）
            0.5,  # gross_margin
            net_margin,
            debt_ratio,
            80.0,  # ocf
            60.0,  # fcf
            1.5,  # eps
            4.0,  # bps
            total_assets,
            total_equity,
        ],
    )
    if ann_date is not None:  # 版本表主键含 ann_date（NOT NULL）
        c.execute(
            "INSERT INTO fact_financial_version "
            "(code, report_period, ann_date, revenue, net_profit, roe, roa, gross_margin, "
            "net_margin, debt_ratio, ocf, fcf, eps, bps, total_assets, total_equity) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                code,
                report_period,
                ann_date,
                revenue,
                net_profit,
                0.15,
                None,
                0.5,
                net_margin,
                debt_ratio,
                80.0,
                60.0,
                1.5,
                4.0,
                total_assets,
                total_equity,
            ],
        )


def _insert_valuation(
    c: duckdb.DuckDBPyConnection,
    code: str,
    trade_date: date,
    *,
    pe_ttm: float | None = 20.0,
    pb: float | None = 3.0,
    market_cap: float | None = 1000.0,
) -> None:
    c.execute(
        "INSERT INTO fact_valuation "
        "(code, trade_date, pe_ttm, pb, ps_ttm, dv_ratio, market_cap, pe_percentile) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [code, trade_date, pe_ttm, pb, 5.0, 0.03, market_cap, 0.5],
    )


def _insert_bar(
    c: duckdb.DuckDBPyConnection, code: str, trade_date: date, *, close: float = 10.0
) -> None:
    c.execute(
        "INSERT INTO fact_daily_bar "
        "(code, trade_date, open, high, low, close, volume, amount, adj_factor, turnover_rate) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        [code, trade_date, close, close, close, close, 1000, 100000.0, 1.0, 0.01],
    )


def _insert_shares(
    c: duckdb.DuckDBPyConnection, code: str, change_date: date, total_shares: float
) -> None:
    c.execute(
        "INSERT INTO fact_share_structure "
        "(code, change_date, total_shares, total_a_shares, listed_a_shares, change_reason) "
        "VALUES (?,?,?,?,?,?)",
        [code, change_date, total_shares, total_shares, total_shares, "首发"],
    )


# ---------------------------------------------------------------------------
# 1. 纯判定函数
# ---------------------------------------------------------------------------


class TestPureJudges:
    def test_coverage_ok_and_fail_and_warn(self) -> None:
        assert judge_coverage(9, 10, min_ratio=0.9)[0] == "OK"
        assert judge_coverage(8, 10, min_ratio=0.9)[0] == "FAIL"
        assert judge_coverage(0, 0, min_ratio=0.9)[0] == "WARN"

    def test_periods_off_calendar_and_short_history(self) -> None:
        ok = [date(y, m, 28) for y in (2023, 2024) for m in (3, 6, 9, 12)]
        assert judge_periods(ok, min_quarters=8)[0] == "OK"
        # 出现非季末报告期 → FAIL
        assert judge_periods([date(2024, 5, 31)], min_quarters=1)[0] == "FAIL"
        # 期数不足 → WARN
        assert judge_periods([date(2024, 3, 31)], min_quarters=8)[0] == "WARN"
        assert judge_periods([], min_quarters=8)[0] == "WARN"

    def test_null_rates(self) -> None:
        assert judge_null_rates({"revenue": 0.1}, max_rate=0.3, label="x")[0] == "OK"
        assert judge_null_rates({"revenue": 0.5}, max_rate=0.3, label="x")[0] == "FAIL"
        assert judge_null_rates({}, max_rate=0.3, label="x")[0] == "WARN"

    def test_lookahead(self) -> None:
        assert judge_lookahead(0, max_rows=0)[0] == "OK"
        assert judge_lookahead(3, max_rows=0)[0] == "FAIL"

    def test_caliber(self) -> None:
        assert judge_caliber(100, 5, max_violation_ratio=0.1)[0] == "OK"
        assert judge_caliber(100, 50, max_violation_ratio=0.1)[0] == "FAIL"
        assert judge_caliber(0, 0, max_violation_ratio=0.1)[0] == "WARN"

    def test_orphan(self) -> None:
        assert judge_orphan(1, 1000, max_ratio=0.02)[0] == "OK"
        assert judge_orphan(100, 1000, max_ratio=0.02)[0] == "FAIL"
        assert judge_orphan(0, 0, max_ratio=0.02)[0] == "WARN"

    def test_market_cap(self) -> None:
        assert judge_market_cap([0.01, 0.02], rel_tol=0.05)[0] == "OK"
        assert judge_market_cap([0.5], rel_tol=0.05)[0] == "FAIL"
        assert judge_market_cap([], rel_tol=0.05)[0] == "WARN"

    def test_flow_caliber(self) -> None:
        assert judge_flow_caliber(100, 20, max_ratio=0.9, min_years=50)[0] == "OK"
        assert judge_flow_caliber(100, 96, max_ratio=0.9, min_years=50)[0] == "FAIL"
        assert judge_flow_caliber(10, 5, max_ratio=0.9, min_years=50)[0] == "WARN"


# ---------------------------------------------------------------------------
# 2. 口径一致性纯函数
# ---------------------------------------------------------------------------


class TestCaliber:
    def _records(self, bad_debt: float | None = None) -> list:
        # (code, report_period, total_assets, total_equity, debt_ratio)
        recs = []
        for m in (3, 6, 9, 12):
            dr = 0.4 if bad_debt is None or m != 6 else bad_debt
            recs.append(("600519", date(2024, m, 28), 1000.0, 600.0, dr))
        return recs

    def test_consistent_balance_no_violation(self) -> None:
        comparable, violations = caliber_balance_violations(self._records(), rel_tol=0.05)
        assert comparable == 4
        assert violations == 0

    def test_inconsistent_debt_ratio_detected(self) -> None:
        comparable, violations = caliber_balance_violations(
            self._records(bad_debt=0.8), rel_tol=0.05
        )
        assert comparable == 4
        assert violations == 1

    def test_zero_assets_row_skipped(self) -> None:
        recs = [
            ("600519", date(2024, 3, 28), 0.0, 0.0, 0.4),  # 分母为 0 → 跳过
            ("600519", date(2024, 6, 28), 1000.0, 600.0, 0.4),
        ]
        comparable, violations = caliber_balance_violations(recs, rel_tol=0.05)
        assert comparable == 1
        assert violations == 0

    def test_flow_monotonic_count(self) -> None:
        # 2023 全部递增（累计嫌疑）；2024 非递增
        recs = [("600519", date(2023, m, 28), float(i)) for i, m in enumerate((3, 6, 9, 12), 1)]
        recs += [
            ("600519", date(2024, 3, 28), 300.0),
            ("600519", date(2024, 6, 28), 100.0),
            ("600519", date(2024, 9, 28), 200.0),
            ("600519", date(2024, 12, 28), 150.0),
        ]
        years, monotonic = flow_monotonic_count(recs)
        assert years == 2
        assert monotonic == 1

    def test_flow_monotonic_skips_incomplete_year(self) -> None:
        recs = [("600519", date(2024, 3, 28), 1.0), ("600519", date(2024, 6, 28), 2.0)]
        assert flow_monotonic_count(recs) == (0, 0)


# ---------------------------------------------------------------------------
# 3. 端到端：空库安全
# ---------------------------------------------------------------------------


class TestEmptyDatabase:
    def test_empty_db_no_exception(self, conn) -> None:
        report = audit_database(conn, {})
        assert report.fail_count == 0
        assert report.samples == []
        # 空库无数据可判定 → 全部 WARN/OK，绝不抛异常
        assert report.worst_state in ("OK", "WARN")
        # JSON 可序列化
        json.dumps(report.to_dict(), ensure_ascii=False)


# ---------------------------------------------------------------------------
# 4. 端到端：各检查项触发
# ---------------------------------------------------------------------------


class TestChecksTrigger:
    def test_coverage_fail_when_universe_without_financial(self, conn) -> None:
        for i in range(10):
            _insert_stock(conn, f"60000{i}", "SH")
        report = audit_database(conn, {})
        coverage = [f for f in report.findings if f.check == "coverage"]
        assert any(f.state == "FAIL" for f in coverage), coverage

    def test_lookahead_fail(self, conn) -> None:
        _insert_stock(conn, "600519")
        _insert_financial(conn, "600519", date(2024, 12, 31), date(2024, 1, 1))
        report = audit_database(conn, {})
        fin = [f for f in report.findings if f.section == "financial" and f.check == "lookahead"]
        assert fin and fin[0].state == "FAIL", fin

    def test_ann_lag_ok_for_normal_disclosure(self, conn) -> None:
        """正常披露节奏（滞后 ~90 天）：滞后分布检查通过。"""
        _insert_stock(conn, "600519")
        _insert_financial(conn, "600519", date(2024, 12, 31), date(2025, 4, 1))
        report = audit_database(conn, {})
        lag = [f for f in report.findings if f.check == "ann_lag"]
        assert lag and lag[0].state == "OK", lag

    def test_ann_lag_fail_for_restated_dates(self, conn) -> None:
        """公告日误取重述日（滞后 > 130 天）：滞后分布检查 FAIL。"""
        _insert_stock(conn, "600519")
        for period, ann in [
            (date(2022, 12, 31), date(2024, 4, 20)),
            (date(2023, 12, 31), date(2025, 4, 20)),
            (date(2024, 12, 31), date(2026, 4, 20)),
        ]:
            _insert_financial(conn, "600519", period, ann)
        report = audit_database(conn, {})
        lag = [f for f in report.findings if f.check == "ann_lag"]
        assert lag and lag[0].state == "FAIL", lag

    def test_valuation_orphan_fail(self, conn) -> None:
        _insert_stock(conn, "600519")
        _insert_financial(conn, "600519", date(2024, 12, 31), date(2025, 4, 1))
        # 估值行给一个行情表不存在的交易日 → 孤立
        _insert_valuation(conn, "600519", date(2025, 1, 2))
        report = audit_database(conn, {})
        orphan = [f for f in report.findings if f.check == "valuation_bar_match"]
        assert orphan and orphan[0].state == "FAIL", orphan

    def test_market_cap_consistent_ok(self, conn) -> None:
        _insert_stock(conn, "600519")
        _insert_financial(conn, "600519", date(2024, 12, 31), date(2025, 4, 1))
        _insert_bar(conn, "600519", date(2025, 1, 2), close=10.0)
        _insert_shares(conn, "600519", date(2020, 1, 1), 100.0)
        _insert_valuation(conn, "600519", date(2025, 1, 2), market_cap=1000.0)
        report = audit_database(conn, {})
        mc = [f for f in report.findings if f.check == "market_cap"]
        assert mc and mc[0].state == "OK", mc

    def test_market_cap_deviation_fail(self, conn) -> None:
        _insert_stock(conn, "600519")
        _insert_financial(conn, "600519", date(2024, 12, 31), date(2025, 4, 1))
        _insert_bar(conn, "600519", date(2025, 1, 2), close=10.0)
        _insert_shares(conn, "600519", date(2020, 1, 1), 100.0)
        # 期望市值 = 10 x 100 = 1000，实际写 2000 → 偏差 100%
        _insert_valuation(conn, "600519", date(2025, 1, 2), market_cap=2000.0)
        report = audit_database(conn, {})
        mc = [f for f in report.findings if f.check == "market_cap"]
        assert mc and mc[0].state == "FAIL", mc

    def test_samples_collected_and_jsonable(self, conn) -> None:
        _insert_stock(conn, "600519")
        _insert_financial(conn, "600519", date(2024, 12, 31), date(2025, 4, 1))
        _insert_bar(conn, "600519", date(2025, 1, 2), close=10.0)
        _insert_valuation(conn, "600519", date(2025, 1, 2), market_cap=1000.0)
        report = audit_database(conn, {}, sample_size=5, seed=1)
        assert len(report.samples) == 1
        assert report.samples[0].code == "600519"
        json.dumps(report.to_dict(), ensure_ascii=False)

    def test_sample_size_zero_skips_caliber(self, conn) -> None:
        _insert_stock(conn, "600519")
        _insert_financial(conn, "600519", date(2024, 12, 31), date(2025, 4, 1))
        report = audit_database(conn, {}, sample_size=0)
        caliber = [f for f in report.findings if f.check == "caliber.balance"]
        assert caliber and caliber[0].state == "WARN", caliber
