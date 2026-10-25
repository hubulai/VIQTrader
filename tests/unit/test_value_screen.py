"""价值初筛单测（agents/value/screen.py）：漏斗纯函数 + 内存库端到端 + 幂等 + DAG 装配。"""

from __future__ import annotations

from datetime import date

import duckdb

from viqtrader.agents.value.screen import (
    consecutive_pass_years,
    consecutive_profit_years,
    passes_circle_of_competence,
    passes_quality,
    passes_valuation_gate,
    run_value_screen,
)
from viqtrader.orchestrator.jobs import build_daily_tasks

SCREEN_DATE = date(2026, 10, 17)  # 周六


# ---------------------------------------------------------------------------
# 纯函数判定
# ---------------------------------------------------------------------------


def test_consecutive_pass_years():
    assert consecutive_pass_years([0.2, 0.18, 0.15], 0.12) == 3
    assert consecutive_pass_years([0.2, 0.10, 0.15], 0.12) == 1  # 中间断即止
    assert consecutive_pass_years([None, 0.2], 0.12) == 0
    assert consecutive_pass_years([], 0.12) == 0


def test_consecutive_profit_years():
    assert consecutive_profit_years([1.0, 0.5, 0.3]) == 3
    assert consecutive_profit_years([1.0, -0.5, 0.3]) == 1
    assert consecutive_profit_years([1.0, None]) == 1  # None 断
    assert consecutive_profit_years([0.0]) == 0


def test_circle_of_competence():
    kw = dict(
        industry_whitelist=["食品饮料"],
        industry_blacklist=["房地产"],
        stock_blacklist=["600001"],
    )
    assert passes_circle_of_competence("食品饮料", "600519", **kw) is True
    assert passes_circle_of_competence("房地产", "000002", **kw) is False  # 黑名单
    assert passes_circle_of_competence("电子", "000001", **kw) is False  # 白名单外
    assert passes_circle_of_competence("食品饮料", "600001", **kw) is False  # 个股黑名单
    # 白名单空 = 不启用
    kw2 = dict(industry_whitelist=[], industry_blacklist=["房地产"], stock_blacklist=[])
    assert passes_circle_of_competence("电子", "000001", **kw2) is True


def test_quality_funnel():
    good = [0.2, 0.18, 0.15]
    assert (
        passes_quality(
            good,
            [5e8, 4e8, 3e8],
            [1e9, 0.9e9, 0.8e9],
            0.5,
            roe_min=0.12,
            sustain_years=3,
            min_profit_years=3,
            require_positive_fcf=True,
            max_debt_ratio=0.6,
        )
        is True
    )
    # ROE 一期不达标
    assert (
        passes_quality(
            [0.2, 0.11, 0.15],
            [5e8, 4e8, 3e8],
            [1e9, 0.9e9, 0.8e9],
            0.5,
            roe_min=0.12,
            sustain_years=3,
            min_profit_years=3,
            require_positive_fcf=True,
            max_debt_ratio=0.6,
        )
        is False
    )
    # 窗口数据不足（只有 2 期）
    assert (
        passes_quality(
            [0.2, 0.18],
            [5e8, 4e8],
            [1e9, 0.9e9],
            0.5,
            roe_min=0.12,
            sustain_years=3,
            min_profit_years=3,
            require_positive_fcf=True,
            max_debt_ratio=0.6,
        )
        is False
    )
    # FCF 一期为负（require_positive_fcf）
    assert (
        passes_quality(
            good,
            [5e8, -1e8, 3e8],
            [1e9, 0.9e9, 0.8e9],
            0.5,
            roe_min=0.12,
            sustain_years=3,
            min_profit_years=3,
            require_positive_fcf=True,
            max_debt_ratio=0.6,
        )
        is False
    )
    # FCF 缺失（None）宁缺勿错
    assert (
        passes_quality(
            good,
            [5e8, None, 3e8],
            [1e9, 0.9e9, 0.8e9],
            0.5,
            roe_min=0.12,
            sustain_years=3,
            min_profit_years=3,
            require_positive_fcf=True,
            max_debt_ratio=0.6,
        )
        is False
    )
    # 连续盈利不足 3 年（最近一期亏损）
    assert (
        passes_quality(
            good,
            [5e8, 4e8, 3e8],
            [-1e8, 0.9e9, 0.8e9],
            0.5,
            roe_min=0.12,
            sustain_years=3,
            min_profit_years=3,
            require_positive_fcf=True,
            max_debt_ratio=0.6,
        )
        is False
    )
    # 负债率超限
    assert (
        passes_quality(
            good,
            [5e8, 4e8, 3e8],
            [1e9, 0.9e9, 0.8e9],
            0.7,
            roe_min=0.12,
            sustain_years=3,
            min_profit_years=3,
            require_positive_fcf=True,
            max_debt_ratio=0.6,
        )
        is False
    )
    # 负债率缺失宁缺勿错
    assert (
        passes_quality(
            good,
            [5e8, 4e8, 3e8],
            [1e9, 0.9e9, 0.8e9],
            None,
            roe_min=0.12,
            sustain_years=3,
            min_profit_years=3,
            require_positive_fcf=True,
            max_debt_ratio=0.6,
        )
        is False
    )
    # 不要求 FCF 时负值放行
    assert (
        passes_quality(
            good,
            [-1e8, -1e8, -1e8],
            [1e9, 0.9e9, 0.8e9],
            0.5,
            roe_min=0.12,
            sustain_years=3,
            min_profit_years=3,
            require_positive_fcf=False,
            max_debt_ratio=0.6,
        )
        is True
    )


def test_valuation_gate():
    assert passes_valuation_gate(0.4, 0.5) is True
    assert passes_valuation_gate(0.5, 0.5) is True  # 边界：不高于
    assert passes_valuation_gate(0.6, 0.5) is False
    assert passes_valuation_gate(None, 0.5) is False  # 历史不足不过闸


# ---------------------------------------------------------------------------
# 内存库端到端
# ---------------------------------------------------------------------------


def _seed_universe(conn: duckdb.DuckDBPyConnection) -> None:
    """构造 5 只特征各异的股票：
    good  达标+便宜（应入选且过闸）
    rich  达标但 PE 分位高（入选但闸门未过）
    st    达标但 ST（硬剔除）
    bad   ROE 不达标（质量剔除）
    bl    达标但在个股黑名单（能力圈剔除）
    """
    stocks = [
        ("600001", "SH", "好公司", "食品饮料"),
        ("600002", "SH", "贵公司", "食品饮料"),
        ("600003", "SH", "ST假货", "食品饮料"),
        ("600004", "SZ", "差公司", "食品饮料"),
        ("600005", "SH", "黑名单", "食品饮料"),
    ]
    for code, market, name, industry in stocks:
        conn.execute(
            "INSERT INTO dim_stock (code, market, name, industry) VALUES (?,?,?,?)",
            [code, market, name, industry],
        )
        conn.execute(
            "INSERT INTO fact_daily_bar (code, trade_date, open, high, low, close, "
            "volume, amount, adj_factor) VALUES (?,?,?,?,?,?,?,?,1.0)",
            [code, "2026-10-16", 10, 10, 10, 10, 1000, 10000],
        )
        # 3 个年度 x 4 季度报告（fact_financial_version 流量列为**单季口径**，年度值须四季求和；
        # roe/debt_ratio 为 YTD/时点口径，只取自 12-31 行）
        for year, roe, fcf, np_, dr, rev in [
            (2023, 0.15, 3e8, 8e8, 0.4, 100.0),
            (2024, 0.18, 4e8, 9e8, 0.45, 110.0),
            (2025, 0.20, 5e8, 10e8, 0.5, 121.0),
        ]:
            for month, ann in (
                (3, f"{year}-04-25"),
                (6, f"{year}-08-20"),
                (9, f"{year}-10-25"),
                (12, f"{year + 1}-03-28"),
            ):
                day = "31" if month in (3, 12) else "30"
                period = f"{year}-{month:02d}-{day}"
                conn.execute(
                    "INSERT INTO fact_financial_version (code, report_period, ann_date, roe, fcf, "
                    "net_profit, debt_ratio, revenue) VALUES (?,?,?,?,?,?,?,?)",
                    [
                        code,
                        period,
                        ann,
                        roe if month == 12 else None,
                        fcf / 4,
                        np_ / 4,
                        dr if month == 12 else None,
                        rev / 4,
                    ],
                )
    # 差公司：最近年度 ROE 掉到 0.05
    conn.execute(
        "UPDATE fact_financial_version SET roe = 0.05 WHERE code = '600004' "
        "AND report_period = '2025-12-31'"
    )
    # 估值：good 分位 0.3 过闸；rich 分位 0.8 不过闸；其余可缺
    for code, pct in [
        ("600001", 0.3),
        ("600002", 0.8),
        ("600003", 0.4),
        ("600004", 0.4),
        ("600005", 0.4),
    ]:
        conn.execute(
            "INSERT INTO fact_valuation (code, trade_date, pe_ttm, pe_percentile) "
            "VALUES (?,?,15.0,?)",
            [code, "2026-10-16", pct],
        )


def test_run_value_screen_end_to_end(conn, config):
    _seed_universe(conn)
    config_data = {
        "circle_of_competence": {
            "industry_whitelist": [],
            "industry_blacklist": [],
            "stock_blacklist": ["600005"],
        },
        "value_track": {
            "screening": {
                "roe_min": 0.12,
                "roe_sustain_years": 3,
                "min_consecutive_profit_years": 3,
                "require_positive_fcf": True,
                "max_debt_ratio": 0.6,
                "pe_percentile_max": 0.5,
            }
        },
    }

    class _Cfg:
        def get(self, key, default=None):
            node: object = config_data
            for part in key.split("."):
                if not isinstance(node, dict) or part not in node:
                    return default
                node = node[part]
            return node

        def content_hash(self) -> str:
            return "testhash"

    report = run_value_screen(conn, _Cfg(), SCREEN_DATE)
    assert report.universe == 5
    assert report.after_circle == 4  # 600005 黑名单剔除
    assert report.after_hard == 3  # ST 剔除
    assert report.after_quality == 2  # 600004 ROE 不达标
    assert report.gate_passed == 1

    codes = [c.code for c in report.candidates]
    assert codes == ["600001", "600002"]  # 质量排序：两者 roe 序列相同 → fcf/代码定序

    good = report.candidates[0]
    assert good.roe_years == 3
    assert good.gate_passed is True
    assert good.fcf_all_positive is True
    assert good.revenue_cagr is not None and good.revenue_cagr > 0

    rich = report.candidates[1]
    assert rich.gate_passed is False

    # 落库断言
    rows = conn.execute(
        "SELECT code, gate_passed, config_hash FROM value_candidate "
        "WHERE screen_date = ? ORDER BY code",
        [SCREEN_DATE],
    ).fetchall()
    assert [r[0] for r in rows] == ["600001", "600002"]
    assert rows[0][1] is True and rows[1][1] is False
    assert rows[0][2]  # config_hash 非空


def test_run_value_screen_idempotent(conn, config):
    """同日重跑先删后插：行数不变、无主键冲突。"""
    _seed_universe(conn)
    r1 = run_value_screen(conn, config, SCREEN_DATE)
    r2 = run_value_screen(conn, config, SCREEN_DATE)
    assert r1.after_quality == r2.after_quality
    n = conn.execute(
        "SELECT count(*) FROM value_candidate WHERE screen_date = ?", [SCREEN_DATE]
    ).fetchone()[0]
    assert n == r2.after_quality


def test_run_value_screen_empty_market_safe(conn, config):
    """空库安全：universe=0，不落行不抛异常。"""
    report = run_value_screen(conn, config, SCREEN_DATE)
    assert report.universe == 0
    assert report.candidates == []
    assert conn.execute("SELECT count(*) FROM value_candidate").fetchone()[0] == 0


def test_run_value_screen_default_date_from_bar(conn, config):
    """缺省筛选日取 fact_daily_bar 最新交易日。"""
    _seed_universe(conn)
    report = run_value_screen(conn, config, None)
    assert report.screen_date == date(2026, 10, 16)


# ---------------------------------------------------------------------------
# DAG 装配
# ---------------------------------------------------------------------------


def test_build_daily_tasks_saturday(config):
    tasks = build_daily_tasks(config, SCREEN_DATE)  # 周六
    by = {t.name: t for t in tasks}
    assert "value_screen" in by
    assert by["value_screen"].placeholder is False
    assert by["value_screen"].depends_on == ("quality",)


def test_build_daily_tasks_weekday_no_screen(config):
    tasks = build_daily_tasks(config, date(2026, 10, 14))  # 周三
    assert "value_screen" not in [t.name for t in tasks]
