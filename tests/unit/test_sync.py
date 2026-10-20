"""P1.6 sync.py 单元测试（全离线，内存 DuckDB + 伪造适配器）。"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import duckdb
import pandas as pd
import pytest

from viqtrader.data.base import Capability, DataSourceError, empty_frame
from viqtrader.data.sync import IncrementalSyncer, SyncConfig, _quarterly_periods
from viqtrader.store import migrate as _migrate

# ---------------------------------------------------------------------------
# 0. _quarterly_periods 季度报告期生成
# ---------------------------------------------------------------------------


class TestQuarterlyPeriods:
    def test_ten_years_lookback(self) -> None:
        # 2026-10-17 视角近 10 年：2017Q1 ~ 2026Q3，共 39 期（Q4 未到）
        periods = _quarterly_periods(date(2026, 10, 17), 10)
        assert periods[0] == date(2017, 3, 31)
        assert periods[-1] == date(2026, 9, 30)
        assert len(periods) == 39
        assert date(2026, 12, 31) not in periods  # 未来季度末不含

    def test_year_start_includes_prior_q4(self) -> None:
        # 年初视角：上一年 Q4 报告期已过去应包含（公告未出无害，适配器按存在性过滤）
        periods = _quarterly_periods(date(2026, 1, 15), 10)
        assert periods[-1] == date(2025, 12, 31)
        assert periods[0] == date(2017, 3, 31)

    def test_years_floor_is_one(self) -> None:
        # years<=0 按 1 年兜底：仅当年已过去的季度末
        periods = _quarterly_periods(date(2026, 5, 1), 0)
        assert periods == [date(2026, 3, 31)]

    def test_financial_sync_passes_periods(self, conn, syncer, adapter) -> None:
        """_sync_financial 应按 config financial_lookback_years(3)生成季度期列表传给适配器。"""
        adapter.set_response("financial:600519", empty_frame(Capability.FINANCIAL))
        cfg = SyncConfig(codes=["600519"], adapter=adapter, capabilities=[Capability.FINANCIAL])
        syncer.sync(cfg)
        call = next(c for c in adapter.calls if "fetch_financial" in c)
        periods = call["fetch_financial"]["periods"]
        assert periods is not None, "财务同步应显式传 periods（不再依赖适配器默认全量）"
        assert 9 <= len(periods) <= 12, f"3 年回拉约 11~12 期，实际 {len(periods)}"
        assert all(p.month in (3, 6, 9, 12) for p in periods), "均为自然季度末"


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
                Capability.INDEX_BAR,
                Capability.INDUSTRY_MAP,
                Capability.SHARE_STRUCTURE,
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

    def fetch_industry_map(self):
        self.calls.append({"fetch_industry_map": {}})
        return self._responses.get("industry_map", empty_frame(Capability.INDUSTRY_MAP))

    def fetch_index_bar(self, code, start, end):
        self.calls.append({"fetch_index_bar": {"code": code, "start": start, "end": end}})
        return self._responses.get(f"index_bar:{code}", empty_frame(Capability.INDEX_BAR))

    def fetch_share_structure(self):
        self.calls.append({"fetch_share_structure": {}})
        return self._responses.get("share_structure", empty_frame(Capability.SHARE_STRUCTURE))


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
        assert args["start"] == date(2025, 1, 4), (
            f"增量起点应为 2025-01-04（最后日期 + 1），实际 {args['start']}"
        )
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
        assert args["start"] == expected_start, (
            f"全新标的应从 {expected_start} 开始（end - 365），实际 {args['start']}"
        )

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

        assert rows1 == rows2 == len(fake_data), (
            f"幂等失败：第一次 {rows1} 行，第二次 {rows2} 行，预期 {len(fake_data)}"
        )

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
        assert any("ann_date < report_period" in w for w in fin_report.warnings), (
            f"应检测到 ann_date < report_period 告警，实际 warnings={fin_report.warnings}"
        )
        # 被拒绝的行不应入库
        assert fin_report.rows_inserted == 0, "违规行应被拒绝入库"


# ---------------------------------------------------------------------------
# 6. STOCK_LIST 全量覆盖
# ---------------------------------------------------------------------------


class TestStockListFullRefresh:
    def test_stock_list_full_override(self, conn, syncer, adapter) -> None:
        # 先插一条旧数据
        conn.execute(
            "INSERT INTO dim_stock (code, market, name, is_hk_connect) VALUES"
            " ('OLD', 'SH', '旧股票', FALSE)"
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


# ---------------------------------------------------------------------------
# 7. INDEX_BAR：fact_index_bar 增量 + 幂等
# ---------------------------------------------------------------------------


def _make_index_bar(code: str, dates: list[date]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "code": [code] * len(dates),
            "trade_date": pd.to_datetime(dates),
            "open": [4000.0] * len(dates),
            "high": [4010.0] * len(dates),
            "low": [3990.0] * len(dates),
            "close": [4005.0] * len(dates),
            "volume": [1000000] * len(dates),
            "amount": [2.0e8] * len(dates),
        }
    )


class TestIndexBarSync:
    def test_insert_and_incremental_resume(self, conn, syncer, adapter) -> None:
        dates = [date(2025, 1, 2), date(2025, 1, 3), date(2025, 1, 6)]
        adapter.set_response("index_bar:000300", _make_index_bar("000300", dates))

        cfg = SyncConfig(
            codes=["000300"],
            start_date=date(2025, 1, 2),
            end_date=date(2025, 1, 6),
            adapter=adapter,
            capabilities=[Capability.INDEX_BAR],
        )
        reports = syncer.sync(cfg)
        idx_report = next(r for r in reports if r.capability == "index_bar")
        assert idx_report.rows_inserted == 3

        rows = conn.execute(
            "SELECT count(*), min(trade_date), max(trade_date) FROM fact_index_bar"
            " WHERE code='000300'"
        ).fetchone()
        assert rows[0] == 3
        assert str(rows[1]) == "2025-01-02"
        assert str(rows[2]) == "2025-01-06"

        # 增量续传：库内已有到 01-06，再跑应从上日 +1 起拉
        adapter.set_response("index_bar:000300", _make_index_bar("000300", [date(2025, 1, 7)]))
        cfg2 = SyncConfig(
            codes=["000300"],
            start_date=None,
            end_date=date(2025, 1, 7),
            adapter=adapter,
            capabilities=[Capability.INDEX_BAR],
        )
        syncer.sync(cfg2)
        idx_calls = [c for c in adapter.calls if "fetch_index_bar" in c]
        assert idx_calls[-1]["fetch_index_bar"]["start"] == date(2025, 1, 7)
        total = conn.execute("SELECT count(*) FROM fact_index_bar WHERE code='000300'").fetchone()[
            0
        ]
        assert total == 4

    def test_idempotent_no_duplicate_pk(self, conn, syncer, adapter) -> None:
        dates = [date(2025, 1, 2), date(2025, 1, 3)]
        adapter.set_response("index_bar:000300", _make_index_bar("000300", dates))
        cfg = SyncConfig(
            codes=["000300"],
            start_date=date(2025, 1, 2),
            end_date=date(2025, 1, 3),
            adapter=adapter,
            capabilities=[Capability.INDEX_BAR],
        )
        syncer.sync(cfg)
        syncer.sync(cfg)  # 同区间重跑
        dup = conn.execute(
            "SELECT code, trade_date, COUNT(*) c FROM fact_index_bar GROUP BY 1,2 HAVING c > 1"
        ).fetchall()
        assert not dup, f"主键重复: {dup}"


# ---------------------------------------------------------------------------
# 8. INDUSTRY_MAP：UPDATE 回填 dim_stock.industry
# ---------------------------------------------------------------------------


def _seed_dim_stock(conn) -> None:
    conn.execute(
        "INSERT INTO dim_stock (code, market, name, is_hk_connect) VALUES"
        " ('600519', 'SH', '贵州茅台', FALSE),"
        " ('000001', 'SZ', '平安银行', FALSE),"
        " ('300999', 'SZ', '未命中股份', FALSE)"
    )


class TestIndustryMapBackfill:
    def test_update_matched_and_keep_unmatched(self, conn, syncer, adapter) -> None:
        _seed_dim_stock(conn)
        adapter.set_response(
            "industry_map",
            pd.DataFrame(
                {
                    "code": ["600519", "000001", "999999"],  # 999999 不在 dim_stock
                    "industry": ["白酒", "银行", "幽灵行业"],
                }
            ),
        )

        report = syncer.sync_industry_map(adapter)

        assert report.rows_updated == 2
        rows = dict(conn.execute("SELECT code, industry FROM dim_stock ORDER BY code").fetchall())
        assert rows["600519"] == "白酒"
        assert rows["000001"] == "银行"
        assert rows["300999"] is None, "未命中映射的标的 industry 应保留原值（NULL）"
        assert any("不在 dim_stock" in w for w in report.warnings)
        assert any("未命中行业映射" in w for w in report.warnings)

    def test_empty_mapping_noop(self, conn, syncer, adapter) -> None:
        _seed_dim_stock(conn)
        adapter.set_response("industry_map", pd.DataFrame({"code": [], "industry": []}))
        report = syncer.sync_industry_map(adapter)
        assert report.rows_updated == 0
        assert any("为空" in w for w in report.warnings)

    def test_data_source_error(self, conn, syncer, adapter) -> None:
        _seed_dim_stock(conn)

        class FailingAdapter(SpyAdapter):
            def fetch_industry_map(self):
                raise DataSourceError("断连")

        report = syncer.sync_industry_map(FailingAdapter())
        assert report.rows_updated == 0
        assert any("数据源错误" in w for w in report.warnings)
        null_cnt = conn.execute("SELECT count(*) FROM dim_stock WHERE industry IS NULL").fetchone()[
            0
        ]
        assert null_cnt == 3, "数据源失败不应改动 dim_stock"


# ---------------------------------------------------------------------------
# 9. SHARE_STRUCTURE：股本结构全量同步 + 估值全量重算
# ---------------------------------------------------------------------------


def _make_share_structure() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "code": ["600519", "000001", "999999", "600519"],
            "change_date": pd.to_datetime(["2023-12-31", "2023-12-31", "2023-12-31", "2024-06-30"]),
            "total_shares": [1.256e9, 1.94e10, 100.0, 1.256e9],
            "total_a_shares": [1.256e9, 1.94e10, 100.0, 1.256e9],
            "listed_a_shares": [1.256e9, 1.94e10, 100.0, 1.256e9],
            "change_reason": ["定期报告", "定期报告", "非股票噪声", "定期报告"],
        }
    )


class TestShareStructureSync:
    def test_full_refresh_filters_unknown_codes(self, conn, syncer, adapter) -> None:
        _seed_dim_stock(conn)
        adapter.set_response("share_structure", _make_share_structure())
        report = syncer.sync_share_structure(adapter)
        assert report.rows_inserted == 3
        assert any("1 行不在 dim_stock" in w for w in report.warnings)
        rows = conn.execute(
            "SELECT count(*), count(DISTINCT code) FROM fact_share_structure"
        ).fetchone()
        assert rows == (3, 2)

    def test_idempotent_full_refresh(self, conn, syncer, adapter) -> None:
        _seed_dim_stock(conn)
        adapter.set_response("share_structure", _make_share_structure())
        syncer.sync_share_structure(adapter)
        report2 = syncer.sync_share_structure(adapter)
        assert report2.rows_inserted == 3
        assert conn.execute("SELECT count(*) FROM fact_share_structure").fetchone()[0] == 3

    def test_fetch_error_keeps_existing_data(self, conn, syncer, adapter) -> None:
        _seed_dim_stock(conn)
        adapter.set_response("share_structure", _make_share_structure())
        syncer.sync_share_structure(adapter)

        def _boom() -> pd.DataFrame:
            raise DataSourceError("datacenter 断连")

        adapter.fetch_share_structure = _boom  # type: ignore[method-assign]
        report = syncer.sync_share_structure(adapter)
        assert report.rows_inserted == 0
        assert any("数据源错误" in w for w in report.warnings)
        assert conn.execute("SELECT count(*) FROM fact_share_structure").fetchone()[0] == 3


class TestRecomputeValuationAll:
    def test_recompute_fills_market_cap(self, conn, syncer) -> None:
        conn.execute(
            "INSERT INTO dim_stock (code, market, name, is_hk_connect) VALUES"
            " ('600519', 'SH', '贵州茅台', FALSE), ('000001', 'SZ', '平安银行', FALSE)"
        )
        spy = SpyAdapter()
        spy.set_response("daily_bar:600519", _make_daily_bar("600519", date(2024, 1, 2), 3))
        spy.set_response("daily_bar:000001", _make_daily_bar("000001", date(2024, 1, 2), 3))
        syncer.sync(
            SyncConfig(
                codes=["600519", "000001"],
                start_date=date(2024, 1, 2),
                end_date=date(2024, 1, 4),
                adapter=spy,
                capabilities=[Capability.DAILY_BAR],
            )
        )
        # 同步链路已算过一轮估值，但库内此时无股本 → market_cap 全 NaN
        # （DuckDB DOUBLE 的 NaN 与 NULL 语义不同，isnan 需显式判）
        nan_cnt = conn.execute(
            "SELECT count(*) FROM fact_valuation WHERE market_cap IS NULL OR isnan(market_cap)"
        ).fetchone()[0]
        assert nan_cnt == 6

        # 补入股本快照后全量重算 → market_cap = close * 总股本
        conn.execute(
            "INSERT INTO fact_share_structure VALUES"
            " ('600519', DATE '2023-12-31', 1256197800, 1256197800, 1256197800, '定期报告'),"
            " ('000001', DATE '2023-12-31', 19405918198, 19405918198, 19405918198, '定期报告')"
        )
        n = syncer.recompute_valuation_all()
        assert n == 2
        rows = conn.execute(
            "SELECT code, market_cap FROM fact_valuation WHERE trade_date = DATE '2024-01-02'"
            " ORDER BY code"
        ).fetchall()
        by_code = dict(rows)
        assert by_code["600519"] == pytest.approx(100.0 * 1256197800)
        assert by_code["000001"] == pytest.approx(100.0 * 19405918198)


# ---------------------------------------------------------------------------
# 10. 估值增量重算：已有行不重写，缺失日期才补算（防每日全量 DELETE+重插 膨胀）
# ---------------------------------------------------------------------------


class TestValuationIncremental:
    def _prepare(self, conn: duckdb.DuckDBPyConnection, cfg_dict: dict) -> tuple[Any, Any]:
        """单标的 5 日 bar + 股本 → 首次 sync 产出估值行。"""
        conn.execute(
            "INSERT INTO dim_stock (code, market, name, is_hk_connect) VALUES"
            " ('600519', 'SH', '贵州茅台', FALSE)"
        )
        conn.execute(
            "INSERT INTO fact_share_structure VALUES"
            " ('600519', DATE '2023-12-31', 1256197800, 1256197800, 1256197800, '定期报告')"
        )
        spy = SpyAdapter()
        spy.set_response("daily_bar:600519", _make_daily_bar("600519", date(2024, 1, 2), 5))
        syncer = IncrementalSyncer(conn, cfg_dict)
        cfg = SyncConfig(
            codes=["600519"],
            start_date=date(2024, 1, 2),
            end_date=date(2024, 1, 8),
            adapter=spy,
            capabilities=[Capability.DAILY_BAR],
        )
        return syncer, cfg

    def test_incremental_backfills_only_missing(self, conn, cfg_dict) -> None:
        """再跑一次 sync（无新增交易日）：已有估值行不重写，被删的缺失日行恢复。"""
        syncer, cfg = self._prepare(conn, cfg_dict)
        syncer.sync(cfg)
        total = conn.execute("SELECT count(*) FROM fact_valuation").fetchone()[0]
        assert total == 5

        # 篡改已有行（模拟旧数据指纹）+ 删除另一已有行（制造缺失日）
        victim = conn.execute(
            "SELECT trade_date FROM fact_valuation WHERE code='600519' ORDER BY trade_date LIMIT 1"
        ).fetchone()[0]
        conn.execute(
            "UPDATE fact_valuation SET pe_ttm = 999.0 WHERE code='600519' AND trade_date = ?",
            [victim],
        )
        missing_date = conn.execute(
            "SELECT trade_date FROM fact_valuation WHERE code='600519' "
            "ORDER BY trade_date DESC LIMIT 1"
        ).fetchone()[0]
        conn.execute(
            "DELETE FROM fact_valuation WHERE code='600519' AND trade_date = ?", [missing_date]
        )

        syncer.sync(cfg)  # bar 无新增 → 增量只补缺失日期

        # 缺失日恢复，总行数不变
        assert conn.execute("SELECT count(*) FROM fact_valuation").fetchone()[0] == total
        # 已有行未被全量重写（篡改指纹保留）
        kept = conn.execute(
            "SELECT pe_ttm FROM fact_valuation WHERE code='600519' AND trade_date = ?", [victim]
        ).fetchone()[0]
        assert kept == 999.0, "增量模式下已有估值行不应被重写"

    def test_full_recompute_rewrites_existing(self, conn, cfg_dict) -> None:
        """recompute_valuation_all（full=True）强制重写已有行——篡改指纹被刷新。"""
        syncer, cfg = self._prepare(conn, cfg_dict)
        syncer.sync(cfg)
        victim = conn.execute(
            "SELECT trade_date FROM fact_valuation WHERE code='600519' ORDER BY trade_date LIMIT 1"
        ).fetchone()[0]
        conn.execute(
            "UPDATE fact_valuation SET pe_ttm = 999.0 WHERE code='600519' AND trade_date = ?",
            [victim],
        )

        syncer.recompute_valuation_all()

        kept = conn.execute(
            "SELECT pe_ttm FROM fact_valuation WHERE code='600519' AND trade_date = ?", [victim]
        ).fetchone()[0]
        assert kept != 999.0, "全量重算应强制刷新已有行"

    def test_new_bar_date_appends_without_rewriting(self, conn, cfg_dict) -> None:
        """新增交易日：只补新日期行，历史行不重写。"""
        syncer, cfg = self._prepare(conn, cfg_dict)
        syncer.sync(cfg)
        victim = conn.execute(
            "SELECT trade_date FROM fact_valuation WHERE code='600519' ORDER BY trade_date LIMIT 1"
        ).fetchone()[0]
        conn.execute(
            "UPDATE fact_valuation SET pe_ttm = 999.0 WHERE code='600519' AND trade_date = ?",
            [victim],
        )

        # 追加一个新交易日的 bar（ spy 追加响应后重跑更大区间）
        conn.execute(
            "INSERT INTO fact_daily_bar VALUES"
            " ('600519', DATE '2024-01-09', 100, 101, 99, 100, 1000, 100000, 1.0, 0.001)"
        )
        syncer.sync(cfg)

        assert conn.execute("SELECT count(*) FROM fact_valuation").fetchone()[0] == 6
        kept = conn.execute(
            "SELECT pe_ttm FROM fact_valuation WHERE code='600519' AND trade_date = ?", [victim]
        ).fetchone()[0]
        assert kept == 999.0, "新增交易日只应补算新日期，历史行不重写"
