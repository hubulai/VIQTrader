"""P1.6 quality.py 单元测试（全离线，内存 DuckDB + 直接造数据入库）。"""

from __future__ import annotations

from datetime import date

import duckdb
import pytest

from viqtrader.data.base import Capability
from viqtrader.data.quality import DataQualityChecker
from viqtrader.store import migrate as _migrate

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def conn() -> duckdb.DuckDBPyConnection:
    c = duckdb.connect(":memory:")
    _migrate.migrate(c)
    return c


@pytest.fixture()
def checker(conn) -> DataQualityChecker:
    return DataQualityChecker(conn, config={})


def _insert_daily_bar(
    conn: duckdb.DuckDBPyConnection,
    code: str,
    dates: list[date],
    *,
    close: float = 100.0,
    volume: int = 1000,
    amount: float = 100000.0,
) -> None:
    tuples = [
        (code, d, close, close + 2, close - 2, close, volume, amount, 1.0, 0.001) for d in dates
    ]
    conn.executemany(
        "INSERT INTO fact_daily_bar VALUES (?,?,?,?,?,?,?,?,?,?)",
        tuples,
    )


# ---------------------------------------------------------------------------
# 1. 停牌检测：volume=0 且 amount=0 → zero_volume_days
# ---------------------------------------------------------------------------


class TestHaltedDetection:
    def test_zero_volume_detected(self, conn, checker) -> None:
        # 3 天正常 + 2 天停牌
        normal = [date(2025, 1, 2), date(2025, 1, 3), date(2025, 1, 6)]
        halted = [date(2025, 1, 7), date(2025, 1, 8)]
        _insert_daily_bar(conn, "600519", normal)
        _insert_daily_bar(conn, "600519", halted, volume=0, amount=0.0)

        reports = checker.check(codes=["600519"], capabilities=[Capability.DAILY_BAR])
        bar_report = reports[0]
        assert set(bar_report.zero_volume_days) == set(halted), (
            f"停牌日应精确匹配，实际 {bar_report.zero_volume_days}"
        )


# ---------------------------------------------------------------------------
# 2. duplicate_keys 检测
# ---------------------------------------------------------------------------


class TestDuplicateKeys:
    def test_duplicate_primary_key_detected(self, conn, checker) -> None:
        # DuckDB 不支持 DROP PRIMARY KEY，用 CREATE TABLE AS 重建不带约束的表
        conn.execute(
            """
            CREATE TABLE _tmp_bar AS SELECT * FROM fact_daily_bar
            """
        )
        conn.execute(
            """
            INSERT INTO _tmp_bar VALUES
            ('600519', '2025-01-02', 100, 102, 98, 100, 1000, 100000, 1.0, 0.001),
            ('600519', '2025-01-02', 101, 103, 99, 101, 1100, 110000, 1.0, 0.001)
            """
        )
        # 让 checker 从临时表读 → 但 checker 写死表名... 所以我们直接
        # 调用 _check_duplicate_keys 来验证 pandas 去重逻辑
        from viqtrader.data.base import SCHEMAS, Capability
        from viqtrader.data.quality import _check_duplicate_keys

        df = conn.execute("SELECT * FROM _tmp_bar").df()
        dup_keys = _check_duplicate_keys(df, SCHEMAS[Capability.DAILY_BAR])
        assert dup_keys, "应检测到主键重复"
        assert any("2025-01-02" in k for k in dup_keys)
        conn.execute("DROP TABLE _tmp_bar")


# ---------------------------------------------------------------------------
# 3. ann_date < report_period 前视偏差（financial）
# ---------------------------------------------------------------------------


class TestFinancialLookahead:
    def test_ann_before_report_period_warning(self, conn, checker) -> None:
        conn.execute(
            """
            INSERT INTO fact_financial VALUES
            ('600519', '2025-12-31', '2025-01-01', 100, 50, 0.25, 0.15, 0.50, 0.30,
             0.30, 40, 30, 1.50, 25.0, 500, 300)
            """
        )
        reports = checker.check(codes=["600519"], capabilities=[Capability.FINANCIAL])
        fin_report = reports[0]
        assert any("ann_date < report_period" in w for w in fin_report.warnings), (
            f"应告警前视偏差，实际 warnings={fin_report.warnings}"
        )


# ---------------------------------------------------------------------------
# 4. 日期连续性缺口
# ---------------------------------------------------------------------------


class TestDateContinuity:
    def test_gap_in_daily_bar(self, conn, checker) -> None:
        dates = [date(2025, 1, 2), date(2025, 1, 3), date(2025, 1, 8), date(2025, 1, 9)]
        _insert_daily_bar(conn, "600519", dates)

        reports = checker.check(codes=["600519"], capabilities=[Capability.DAILY_BAR])
        bar_report = reports[0]
        assert bar_report.gaps, f"应检测到缺口，实际 gaps={bar_report.gaps}"
        gap = bar_report.gaps[0]
        # 01-04 到 01-07
        assert gap[0] == date(2025, 1, 4)
        assert gap[1] == date(2025, 1, 7)

    def test_no_gap_when_continuous(self, conn, checker) -> None:
        # 真正连续的日期（2025-01-02 周四 ~ 01-05 周日，不跳过周末）
        dates = [date(2025, 1, 2), date(2025, 1, 3), date(2025, 1, 4), date(2025, 1, 5)]
        _insert_daily_bar(conn, "600519", dates)

        reports = checker.check(codes=["600519"], capabilities=[Capability.DAILY_BAR])
        assert reports[0].gaps == []


# ---------------------------------------------------------------------------
# 5. 分红专项检查
# ---------------------------------------------------------------------------


class TestDividendChecks:
    def test_negative_dividend_warning(self, conn, checker) -> None:
        conn.execute(
            """
            INSERT INTO fact_dividend VALUES
            ('600519', '2025-06-10', '2025-07-10', -5.0, '现金')
            """
        )
        reports = checker.check(codes=["600519"], capabilities=[Capability.DIVIDEND])
        div_report = reports[0]
        assert any("dividend_per_share < 0" in w for w in div_report.warnings), (
            f"应告警负分红，实际 warnings={div_report.warnings}"
        )

    def test_ex_date_after_pay_date_warning(self, conn, checker) -> None:
        conn.execute(
            """
            INSERT INTO fact_dividend VALUES
            ('600519', '2025-08-01', '2025-07-01', 1.0, '现金')
            """
        )
        reports = checker.check(codes=["600519"], capabilities=[Capability.DIVIDEND])
        div_report = reports[0]
        assert any("ex_date > pay_date" in w for w in div_report.warnings)


# ---------------------------------------------------------------------------
# 6. FX_RATE 专项
# ---------------------------------------------------------------------------


class TestFxRateChecks:
    def test_non_positive_rate_warning(self, conn, checker) -> None:
        conn.execute(
            """
            INSERT INTO fact_fx_rate VALUES
            ('HKD/CNY', '2025-01-02', 0.0),
            ('HKD/CNY', '2025-01-03', -1.0)
            """
        )
        reports = checker.check(capabilities=[Capability.FX_RATE])
        fx_report = reports[0]
        assert any("rate ≤ 0" in w for w in fx_report.warnings), (
            f"应告警 rate ≤ 0，实际 warnings={fx_report.warnings}"
        )


# ---------------------------------------------------------------------------
# 7. STOCK_LIST market 范围
# ---------------------------------------------------------------------------


class TestStockListChecks:
    def test_illegal_market_warning(self, conn, checker) -> None:
        conn.execute(
            """
            INSERT INTO dim_stock (code, market, name, is_hk_connect) VALUES
            ('600519', 'XX', '非法市场', FALSE)
            """
        )
        reports = checker.check(capabilities=[Capability.STOCK_LIST])
        sl_report = reports[0]
        assert any("market 非法" in w for w in sl_report.warnings), (
            f"应告警非法 market，实际 warnings={sl_report.warnings}"
        )


# ---------------------------------------------------------------------------
# 8. null_rate 阈值检查
# ---------------------------------------------------------------------------


class TestNullRate:
    def test_null_exceeds_threshold_warning(self, conn) -> None:
        # 用高阈值 0.3，但插一半 null
        cfg = {"data": {"sync": {"quality_check_null_warn_threshold": 0.3}}}
        c = DataQualityChecker(conn, config=cfg)
        # 3 行：1 行 revenue 为 NULL
        conn.execute(
            """
            INSERT INTO fact_financial VALUES
            ('600519', '2024-12-31', '2025-04-03', 100, 50, 0.25, 0.15, 0.50, 0.30,
             0.30, 40, 30, 1.50, 25.0, 500, 300),
            ('600519', '2024-09-30', '2024-10-28', NULL, 50, 0.25, 0.15, 0.50, 0.30,
             0.30, 40, 30, 1.50, 25.0, 500, 300),
            ('600519', '2024-06-30', '2024-08-30', NULL, 50, 0.25, 0.15, 0.50, 0.30,
             0.30, 40, 30, 1.50, 25.0, 500, 300)
            """
        )
        reports = c.check(codes=["600519"], capabilities=[Capability.FINANCIAL])
        fin_report = reports[0]
        assert "revenue" in fin_report.null_rate, (
            f"revenue null_rate 应为 2/3 > 0.3，实际 null_rate={fin_report.null_rate}"
        )


# ---------------------------------------------------------------------------
# 9. 空数据集友好
# ---------------------------------------------------------------------------


class TestEmptyDataset:
    def test_no_data_still_returns_report(self, conn, checker) -> None:
        reports = checker.check(codes=["600519"], capabilities=[Capability.DAILY_BAR])
        assert len(reports) == 1
        assert reports[0].total_rows == 0
        assert any("空数据集" in w for w in reports[0].warnings)
