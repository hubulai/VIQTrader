"""P1.6 sync.py 单元测试（全离线，内存 DuckDB + 伪造适配器）。"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import duckdb
import pandas as pd
import pytest

from viqtrader.data.base import Capability, empty_frame
from viqtrader.data.sync import IncrementalSyncer, SyncConfig
from viqtrader.store import migrate as _migrate

# ---------------------------------------------------------------------------
# 灵活的假适配器：每次调用可返回指定数据，捕获调用参数
# ---------------------------------------------------------------------------


class SpyAdapter:
    """记录每次 fetch_* 调用的参数，返回预设 DataFrame。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._responses: dict[str, pd.DataFrame] = {}

    def set_response(self, name: str, df: pd.DataFrame) -> None:
        self._responses[name] = df

    @property
    def name(self) -> str:
        return "spy"

    def capabilities(self) -> frozenset[Capability]:
        return frozenset(
            {
                Capability.STOCK_LIST,
                Capability.DAILY_BAR,
                Capability.FINANCIAL,
                Capability.DIVIDEND,
                Capability.FX_RATE,
            }
        )

    def fetch_stock_list(self, market=None):
        self.calls.append({"fetch_stock_list": {"market": market}})
        return self._responses.get("stock_list", empty_frame(Capability.STOCK_LIST))

    def fetch_daily_bar(self, code, start, end):
        self.calls.append({"fetch_daily_bar": {"code": code, "start": start, "end": end}})
        return self._responses.get(f"daily_bar:{code}", empty_frame(Capability.DAILY_BAR))

    def fetch_financial(self, code, periods=None):
        self.calls.append({"fetch_financial": {"code": code, "periods": periods}})
        return self._responses.get(f"financial:{code}", empty_frame(Capability.FINANCIAL))

    def fetch_dividend(self, code):
        self.calls.append({"fetch_dividend": {"code": code}})
        return self._responses.get(f"dividend:{code}", empty_frame(Capability.DIVIDEND))

    def fetch_disclosure(self, code, start, end):
        return empty_frame(Capability.DISCLOSURE)

    def fetch_fx_rate(self, pair, start, end):
        self.calls.append({"fetch_fx_rate": {"pair": pair, "start": start, "end": end}})
        return self._responses.get(f"fx_rate:{pair}", empty_frame(Capability.FX_RATE))

    def fetch_hk_connect_history(self):
        return empty_frame(Capability.HK_CONNECT_HISTORY)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def conn() -> duckdb.DuckDBPyConnection:
    c = duckdb.connect(":memory:")
    _migrate.migrate(c)
    return c


@pytest.fixture()
def cfg_dict() -> dict:
    return {
        "hk_connect": {"dividend_tax_h_share": 0.20},
        "data": {
            "sync": {
                "lookback_days_on_gap": 10,
                "financial_lookback_years": 3,
                "daily_lookback_days": 365,
                "quality_check_null_warn_threshold": 0.30,
            },
            "valuation": {
                "pe_percentile_window_days": 250,
                "pe_percentile_min_samples": 60,
            },
        },
    }


@pytest.fixture()
def syncer(conn, cfg_dict) -> IncrementalSyncer:
    return IncrementalSyncer(conn, cfg_dict)


@pytest.fixture()
def adapter() -> SpyAdapter:
    return SpyAdapter()


def _make_daily_bar(code: str, start: date, days: int) -> pd.DataFrame:
    """生成连续日线（跳过周末）。"""
    dates: list[date] = []
    d = start
    while len(dates) < days:
        if d.weekday() < 5:
            dates.append(d)
        d += timedelta(days=1)
    return pd.DataFrame(
        {
            "code": [code] * len(dates),
            "trade_date": pd.to_datetime(dates),
            "open": [100.0] * len(dates),
            "high": [102.0] * len(dates),
            "low": [98.0] * len(dates),
            "close": [100.0] * len(dates),
            "volume": [1000] * len(dates),
            "amount": [100000.0] * len(dates),
            "adj_factor": [1.0] * len(dates),
            "turnover_rate": [0.001] * len(dates),
        }
    )


# ---------------------------------------------------------------------------
# 1. 增量起点：库里已有数据 → 从最后日期 + 1 开始拉
# ---------------------------------------------------------------------------


class TestIncrementalStart:
    def test_resumes_after_last_date(self, conn, syncer, adapter) -> None:
        # 先人工插一批日线到 2025-01-03
        conn.execute(
            """
            INSERT INTO fact_daily_bar VALUES
            ('600519', '2025-01-02', 100, 102, 98, 100, 1000, 100000, 1.0, 0.001),
            ('600519', '2025-01-03', 100, 102, 98, 100, 1000, 100000, 1.0, 0.001)
            """
        )

        # adapter 返回更多数据（假装从 01-04 开始）
        fake_data = _make_daily_bar("600519", date(2025, 1, 4), 5)
        adapter.set_response("daily_bar:600519", fake_data)

        # 强制 end_date 固定，避免 today 漂移
        cfg = SyncConfig(
            codes=["600519"],
            start_date=None,
            end_date=date(2025, 1, 10),
            adapter=adapter,
            capabilities=[Capability.DAILY_BAR],
        )
        syncer.sync(cfg)

        # 检查 adapter 被调用时的 start 参数 = 2025-01-04（即 01-03 + 1）
        daily_calls = [c for c in adapter.calls if "fetch_daily_bar" in c]
        assert len(daily_calls) == 1
        args = daily_calls[0]["fetch_daily_bar"]
        assert args["start"] == date(
            2025, 1, 4
        ), f"增量起点应为 2025-01-04（最后日期 + 1），实际 {args['start']}"
        assert args["end"] == date(2025, 1, 10)

    def test_fresh_stock_defaults_to_lookback_window(self, syncer, adapter) -> None:
        # 全新标的，不传 start_date，默认应该是 end_date - 365
        end = date(2025, 6, 15)
        fake_data = _make_daily_bar("600519", end - timedelta(days=400), 100)
        adapter.set_response("daily_bar:600519", fake_data)

        cfg = SyncConfig(
            codes=["600519"],
            start_date=None,
            end_date=end,
            adapter=adapter,
            capabilities=[Capability.DAILY_BAR],
        )
        syncer.sync(cfg)

        daily_calls = [c for c in adapter.calls if "fetch_daily_bar" in c]
        args = daily_calls[0]["fetch_daily_bar"]
        expected_start = end - timedelta(days=365)
        assert (
            args["start"] == expected_start
        ), f"全新标的应从 {expected_start} 开始（end - 365），实际 {args['start']}"

    def test_explicit_start_date_overrides(self, syncer, adapter) -> None:
        adapter.set_response(
            "daily_bar:600519",
            _make_daily_bar("600519", date(2024, 6, 1), 30),
        )
        cfg = SyncConfig(
            codes=["600519"],
            start_date=date(2024, 6, 1),
            end_date=date(2024, 7, 1),
            adapter=adapter,
            capabilities=[Capability.DAILY_BAR],
        )
        syncer.sync(cfg)
        daily_calls = [c for c in adapter.calls if "fetch_daily_bar" in c]
        args = daily_calls[0]["fetch_daily_bar"]
        assert args["start"] == date(2024, 6, 1)


# ---------------------------------------------------------------------------
# 2. 缺口检测
# ---------------------------------------------------------------------------


class TestGapDetection:
    def test_gap_detected_in_middle(self, syncer, adapter) -> None:
        # 伪造一段有 3 天缺口的数据
        dates = [date(2025, 1, 2), date(2025, 1, 3), date(2025, 1, 8), date(2025, 1, 9)]
        df = pd.DataFrame(
            {
                "code": ["600519"] * 4,
                "trade_date": pd.to_datetime(dates),
                "open": [100.0] * 4,
                "high": [102.0] * 4,
                "low": [98.0] * 4,
                "close": [100.0] * 4,
                "volume": [1000] * 4,
                "amount": [100000.0] * 4,
                "adj_factor": [1.0] * 4,
                "turnover_rate": [0.001] * 4,
            }
        )
        adapter.set_response("daily_bar:600519", df)

        cfg = SyncConfig(
            codes=["600519"],
            start_date=date(2025, 1, 2),
            end_date=date(2025, 1, 10),
            adapter=adapter,
            capabilities=[Capability.DAILY_BAR],
        )
        reports = syncer.sync(cfg)

        daily_report = next(r for r in reports if r.capability == "daily_bar")
        assert daily_report.gaps_found, "应检测到缺口"
        # 2025-01-04 到 2025-01-07（因为 01-03 到 01-08 有 5 天空白）
        gap = daily_report.gaps_found[0]
        assert gap[0] == date(2025, 1, 4)
        assert gap[1] == date(2025, 1, 7)


# ---------------------------------------------------------------------------
# 3. 幂等：重复跑 sync，行数不变，无重复
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_no_duplicates_after_two_runs(self, conn, syncer, adapter) -> None:
        fake_data = _make_daily_bar("600519", date(2025, 1, 2), 10)
        adapter.set_response("daily_bar:600519", fake_data)

        cfg = SyncConfig(
            codes=["600519"],
            start_date=date(2025, 1, 2),
            end_date=date(2025, 1, 20),
            adapter=adapter,
            capabilities=[Capability.DAILY_BAR],
        )
        syncer.sync(cfg)
        rows1 = conn.execute("SELECT COUNT(*) FROM fact_daily_bar WHERE code='600519'").fetchone()[
            0
        ]

        # 第二次跑同一个区间
        adapter2 = SpyAdapter()
        adapter2.set_response("daily_bar:600519", fake_data)
        syncer2 = IncrementalSyncer(conn, {"data": {"sync": {}}})
        cfg2 = SyncConfig(
            codes=["600519"],
            start_date=date(2025, 1, 2),
            end_date=date(2025, 1, 20),
            adapter=adapter2,
            capabilities=[Capability.DAILY_BAR],
        )
        syncer2.sync(cfg2)
        rows2 = conn.execute("SELECT COUNT(*) FROM fact_daily_bar WHERE code='600519'").fetchone()[
            0
        ]

        assert (
            rows1 == rows2 == len(fake_data)
        ), f"幂等失败：第一次 {rows1} 行，第二次 {rows2} 行，预期 {len(fake_data)}"

        # 确认无主键重复
        dup = conn.execute(
            "SELECT code, trade_date, COUNT(*) c FROM fact_daily_bar GROUP BY 1,2 HAVING c > 1"
        ).fetchall()
        assert not dup, f"主键重复: {dup}"


# ---------------------------------------------------------------------------
# 4. 估值串联：同步完基础数据后 fact_valuation 有对应行
# ---------------------------------------------------------------------------


class TestValuationChaining:
    def test_fact_valuation_populated_after_sync(self, conn, cfg_dict) -> None:
        """完整同步日线+财务+分红 → fact_valuation 应有行。"""
        # 构造足够的日线（3年 ≈ 750 交易日）+ 财务 + 分红 + dim_stock
        trade_dates: list[date] = []
        d = date(2022, 1, 3)
        while len(trade_dates) < 750:
            if d.weekday() < 5:
                trade_dates.append(d)
            d += timedelta(days=1)

        bar_df = pd.DataFrame(
            {
                "code": ["600519"] * 750,
                "trade_date": pd.to_datetime(trade_dates),
                "open": [100.0] * 750,
                "high": [101.0] * 750,
                "low": [99.0] * 750,
                "close": [100.0] * 750,
                "volume": [1000] * 750,
                "amount": [100000.0] * 750,
                "adj_factor": [1.0] * 750,
                "turnover_rate": [0.001] * 750,
            }
        )

        fin_df = pd.DataFrame(
            [
                {
                    "code": "600519",
                    "report_period": date(y, 12, 31),
                    "ann_date": date(y + 1, 4, 3),
                    "revenue": 100.0,
                    "net_profit": 50.0,
                    "roe": 0.25,
                    "roa": 0.15,
                    "gross_margin": 0.50,
                    "net_margin": 0.30,
                    "debt_ratio": 0.30,
                    "ocf": 40.0,
                    "fcf": 30.0,
                    "eps": 1.50,
                    "bps": 25.0,
                    "total_assets": 500.0,
                    "total_equity": 300.0,
                }
                for y in range(2020, 2025)
            ]
        )

        div_df = pd.DataFrame(
            [
                {
                    "code": "600519",
                    "ex_date": date(2024, 6, 10),
                    "pay_date": date(2024, 7, 10),
                    "dividend_per_share": 1.50,
                    "dividend_type": "现金",
                }
            ]
        )

        stock_list_df = pd.DataFrame(
            [
                {
                    "code": "600519",
                    "market": "SH",
                    "name": "贵州茅台",
                    "industry": "白酒",
                    "list_date": date(2001, 8, 27),
                    "is_hk_connect": False,
                    "hk_connect_in_date": None,
                    "hk_connect_out_date": None,
                }
            ]
        )

        adapter = SpyAdapter()
        adapter.set_response("daily_bar:600519", bar_df)
        adapter.set_response("financial:600519", fin_df)
        adapter.set_response("dividend:600519", div_df)
        adapter.set_response("stock_list", stock_list_df)

        syncer = IncrementalSyncer(conn, cfg_dict)
        cfg = SyncConfig(
            codes=["600519"],
            start_date=date(2022, 1, 3),
            end_date=date(2024, 12, 31),
            adapter=adapter,
            capabilities=[
                Capability.STOCK_LIST,
                Capability.DAILY_BAR,
                Capability.FINANCIAL,
                Capability.DIVIDEND,
            ],
        )
        syncer.sync(cfg)

        # 检查 fact_valuation 有数据
        val_rows = conn.execute(
            "SELECT COUNT(*) FROM fact_valuation WHERE code='600519'"
        ).fetchone()[0]
        assert val_rows > 0, "估值表应有行，实际 0"

        # pe_ttm / pb 等列有值（部分行可能为 NaN）
        sample = conn.execute(
            "SELECT pe_ttm, pb FROM fact_valuation WHERE code='600519' LIMIT 10"
        ).fetchall()
        pe_vals = [r[0] for r in sample if r[0] is not None]
        assert len(pe_vals) > 0, "pe_ttm 应有有效值"


# ---------------------------------------------------------------------------
# 5. ann_date < report_period 前视偏差 → 拒绝 + 告警
# ---------------------------------------------------------------------------


class TestLookaheadRejection:
    def test_ann_before_report_period_is_rejected(self, syncer, adapter) -> None:
        # report_period=2025-12-31, ann_date=2025-01-01 → 前视偏差
        fin_bad = pd.DataFrame(
            [
                {
                    "code": "600519",
                    "report_period": date(2025, 12, 31),
                    "ann_date": date(2025, 1, 1),  # ← 早于报告期
                    "revenue": 100.0,
                    "net_profit": 50.0,
                    "roe": 0.25,
                    "roa": 0.15,
                    "gross_margin": 0.50,
                    "net_margin": 0.30,
                    "debt_ratio": 0.30,
                    "ocf": 40.0,
                    "fcf": 30.0,
                    "eps": 1.50,
                    "bps": 25.0,
                    "total_assets": 500.0,
                    "total_equity": 300.0,
                }
            ]
        )
        adapter.set_response("financial:600519", fin_bad)

        cfg = SyncConfig(
            codes=["600519"],
            adapter=adapter,
            capabilities=[Capability.FINANCIAL],
        )
        reports = syncer.sync(cfg)

        fin_report = next(r for r in reports if r.capability == "financial")
        assert any(
            "ann_date < report_period" in w for w in fin_report.warnings
        ), f"应检测到 ann_date < report_period 告警，实际 warnings={fin_report.warnings}"
        # 被拒绝的行不应入库
        assert fin_report.rows_inserted == 0, "违规行应被拒绝入库"


# ---------------------------------------------------------------------------
# 6. STOCK_LIST 全量覆盖
# ---------------------------------------------------------------------------


class TestStockListFullRefresh:
    def test_stock_list_full_override(self, conn, syncer, adapter) -> None:
        # 先插一条旧数据
        conn.execute(
            "INSERT INTO dim_stock VALUES ('OLD', 'SH', '旧股票', NULL, NULL, FALSE, NULL, NULL)"
        )

        new_list = pd.DataFrame(
            [
                {
                    "code": "600519",
                    "market": "SH",
                    "name": "贵州茅台",
                    "industry": "白酒",
                    "list_date": date(2001, 8, 27),
                    "is_hk_connect": False,
                    "hk_connect_in_date": None,
                    "hk_connect_out_date": None,
                }
            ]
        )
        adapter.set_response("stock_list", new_list)

        cfg = SyncConfig(
            codes=[],
            adapter=adapter,
            capabilities=[Capability.STOCK_LIST],
        )
        syncer.sync(cfg)

        codes = [r[0] for r in conn.execute("SELECT code FROM dim_stock").fetchall()]
        assert codes == ["600519"], f"全量覆盖失败，实际 codes={codes}"
