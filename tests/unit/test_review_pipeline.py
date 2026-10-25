"""P4.6 复盘 review pipeline 编排测试（内存库 + FakeProvider，全离线）。

覆盖：三段全链路成功落库、归因不可用时其余两段照常、归因失败不阻断 thesis、
单仓 thesis 失败隔离、无 thesis/非 active 跳过、无风控事件 empty、非法区间 fail-fast、
cassette 录制→重放结果一致且零网络。
"""

from __future__ import annotations

import json
import uuid
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import pytest

from viqtrader.agents.llm.cassette import CassetteProvider
from viqtrader.agents.llm.fake import FakeProvider
from viqtrader.agents.shared.review.pipeline import run_review_pipeline

START = date(2026, 1, 1)
END = date(2026, 1, 31)
D0 = "2025-12-31"
D1 = "2026-01-30"

ATTRIBUTION_OK = json.dumps(
    {
        "summary": "组合收益 1.9%，基准 7.5%，超额为负。",
        "allocation_comment": "现金权重过高，配置效应为负。",
        "selection_comment": "食品饮料段内选股与基准持平。",
        "key_findings": ["现金拖累显著"],
        "data_refs": ["Brinson 归因结果 segments"],
    },
    ensure_ascii=False,
)

RISK_OK = json.dumps(
    {
        "overall_risk": "warning",
        "items": [
            {
                "rule_key": "swing.max_positions",
                "interpretation": "波段持仓数达到上限。",
                "suggested_action": "新开仓前先压缩至 4 只以内。",
            }
        ],
        "summary": "区间 1 条 WARN。",
        "data_refs": ["风控事件 swing.max_positions"],
    },
    ensure_ascii=False,
)


def _thesis_ok(position_id: str) -> str:
    return json.dumps(
        {
            "position_id": position_id,
            "thesis_status": "intact",
            "changes": ["无显著变化"],
            "suggested_action": "hold",
            "reasoning": "核心假设未现裂痕，继续持有。",
            "data_refs": ["区间事实：2025 年报", "thesis 关键假设"],
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# 造数
# ---------------------------------------------------------------------------


def _stock(conn: duckdb.DuckDBPyConnection, code: str, market: str, industry: str) -> None:
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
        [code, trade_date, close, close + 0.1, close - 0.1, close, close * 1_000_000],
    )


def _mcap(conn: duckdb.DuckDBPyConnection, code: str, trade_date: str) -> None:
    conn.execute(
        "INSERT INTO fact_valuation (code, trade_date, market_cap) VALUES (?, ?, 1e9)",
        [code, trade_date],
    )


def _trade(
    conn: duckdb.DuckDBPyConnection,
    account_id: str,
    code: str,
    qty: int,
    price: float,
    trade_date: str,
) -> None:
    amount = qty * price
    conn.execute(
        """
        INSERT INTO trade_log (trade_id, account_id, mode, code, side, qty, price,
                               amount_cny, trade_date)
        VALUES (?, ?, 'value', ?, 'BUY', ?, ?, ?, ?)
        """,
        [str(uuid.uuid4()), account_id, code, qty, price, -amount, trade_date],
    )
    conn.execute(
        """
        INSERT INTO cash_ledger (entry_id, account_id, trade_date, change_type,
                                 amount_cny, balance_cny)
        VALUES (?, ?, ?, '买入', ?, 0)
        """,
        [str(uuid.uuid4()), account_id, trade_date, -amount],
    )


def _position(
    conn: duckdb.DuckDBPyConnection,
    position_id: str,
    account_id: str,
    code: str,
    qty: int,
    price: float,
    *,
    thesis_id: str | None,
) -> None:
    conn.execute(
        """
        INSERT INTO position (position_id, account_id, mode, code, qty, avg_cost,
                              avg_cost_cny, open_date, thesis_id)
        VALUES (?, ?, 'value', ?, ?, ?, ?, '2025-12-15', ?)
        """,
        [position_id, account_id, code, qty, price, price, thesis_id],
    )


def _thesis(
    conn: duckdb.DuckDBPyConnection,
    thesis_id: str,
    code: str,
    *,
    status: str = "active",
) -> None:
    conn.execute(
        """
        INSERT INTO thesis (thesis_id, code, mode, summary, assumptions, status)
        VALUES (?, ?, 'value', ?, ?, ?)
        """,
        [
            thesis_id,
            code,
            f"{code} 买入逻辑：护城河稳定",
            json.dumps(["毛利率不显著下滑"], ensure_ascii=False),
            status,
        ],
    )


def _facts(conn: duckdb.DuckDBPyConnection, code: str) -> None:
    """区间内披露一份财报 + 一条公告，验证 period_facts 装配。"""
    conn.execute(
        """
        INSERT INTO fact_financial_version (code, report_period, ann_date, revenue, net_profit,
                                    roe, gross_margin, debt_ratio, ocf)
        VALUES (?, '2025-12-31', '2026-01-20', 1.2e10, 3e9, 0.25, 0.91, 0.2, 2.8e9)
        """,
        [code],
    )
    conn.execute(
        """
        INSERT INTO fact_disclosure (code, ann_date, title, category, summary)
        VALUES (?, '2026-01-22', '2025年年度报告', '财报', '全年经营平稳')
        """,
        [code],
    )


def _risk_event(
    conn: duckdb.DuckDBPyConnection,
    account_id: str,
    *,
    day: str = "2026-01-10",
) -> None:
    conn.execute(
        """
        INSERT INTO risk_event (event_id, event_ts, account_id, mode, rule_key, code,
                                severity, decision, message, config_hash, code_version)
        VALUES (?, ?::TIMESTAMPTZ, ?, 'swing', 'swing.max_positions', '600001',
                'WARN', 'DENY', '波段持仓数已达上限 5 只', 'cfg-test', 'ver-test')
        """,
        [str(uuid.uuid4()), f"{day} 10:00:00+08:00", account_id],
    )


def _seed_universe(conn: duckdb.DuckDBPyConnection) -> None:
    """基准 universe：食品 600519 + 银行 600000，期初市值相等。"""
    _stock(conn, "600519", "SH", "食品饮料")
    _stock(conn, "600000", "SH", "银行")
    for code, close0, close1 in (("600519", 150.0, 165.0), ("600000", 10.0, 10.5)):
        _bar(conn, code, D0, close0)
        _bar(conn, code, D1, close1)
        _mcap(conn, code, D0)


def _seed_held_moutai(
    conn: duckdb.DuckDBPyConnection, *, thesis_id: str | None = "thesis_1"
) -> None:
    _trade(conn, "acc_value", "600519", 1000, 150.0, "2025-12-15")
    _position(conn, "pos_moutai", "acc_value", "600519", 1000, 150.0, thesis_id=thesis_id)
    if thesis_id:
        _thesis(conn, thesis_id, "600519")
    _facts(conn, "600519")


def _provider(
    *,
    attribution: str = ATTRIBUTION_OK,
    thesis_by_code: dict[str, str] | None = None,
    risk: str = RISK_OK,
) -> FakeProvider:
    thesis_by_code = thesis_by_code or {"600519": _thesis_ok("pos_moutai")}

    def respond(messages: Any, model: str, params: dict[str, Any]) -> str:
        head = messages[0].content
        if "组合归因解读 Agent" in head:
            return attribution
        if "持仓 thesis 复核 Agent" in head:
            for code, body in thesis_by_code.items():
                if code in head:
                    return body
            raise AssertionError(f"未预期的 thesis 复核标的: {head[:200]}")
        if "风控解读 Agent" in head:
            return risk
        raise AssertionError(f"未预期的 Agent 调用: {head[:200]}")

    return FakeProvider(respond)


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------


def test_full_pipeline_all_sections_ok(conn, config) -> None:
    _seed_universe(conn)
    _seed_held_moutai(conn)
    _risk_event(conn, "acc_value")
    provider = _provider()

    result = run_review_pipeline(
        conn, config, provider, account_id="acc_value", start=START, end=END
    )

    # 归因段
    assert result.attribution.status == "ok"
    assert result.attribution.agent_status == "SUCCESS"
    assert result.attribution.active_return is not None
    # thesis 段
    assert len(result.thesis_reviews) == 1
    item = result.thesis_reviews[0]
    assert item.status == "success"
    assert item.position_id == "pos_moutai"
    assert item.thesis_status == "intact"
    assert item.suggested_action == "hold"
    # 风控段
    assert result.risk.status == "ok"
    assert result.risk.event_count == 1

    # 三个 Agent 各一次成功落库
    names = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT agent_name FROM agent_run WHERE status = 'SUCCESS'"
        ).fetchall()
    }
    assert names == {"attribution_review", "thesis_review", "risk_interpret"}
    assert conn.execute("SELECT count(*) FROM agent_output").fetchone()[0] == 3

    # thesis prompt 装配了区间财报事实
    thesis_prompt = next(
        c[0][0].content for c in provider.calls if "持仓 thesis 复核 Agent" in c[0][0].content
    )
    assert "2025 年报" in thesis_prompt or "2025-12-31" in thesis_prompt
    assert "全年经营平稳" in thesis_prompt
    assert "区间收益" in thesis_prompt


def test_attribution_unavailable_skips_section_but_risk_runs(conn, config) -> None:
    """空账本（无基准行情时装配返回 None）→ 归因跳过；风控段独立仍执行。"""
    _risk_event(conn, "acc_swing")
    provider = _provider()

    result = run_review_pipeline(
        conn, config, provider, account_id="acc_swing", start=START, end=END
    )

    assert result.attribution.status == "unavailable"
    assert result.thesis_reviews == ()
    assert result.risk.status == "ok"
    called = ["组合归因解读 Agent" in c[0][0].content for c in provider.calls]
    assert not any(called)  # 归因 Agent 未被调用
    assert any("风控解读 Agent" in c[0][0].content for c in provider.calls)


def test_attribution_failure_does_not_block_thesis_and_risk(conn, config) -> None:
    _seed_universe(conn)
    _seed_held_moutai(conn)
    _risk_event(conn, "acc_value")
    provider = _provider(attribution="这不是JSON")  # DEGRADED（含一次重试）

    result = run_review_pipeline(
        conn, config, provider, account_id="acc_value", start=START, end=END
    )

    assert result.attribution.status == "failed"
    assert result.attribution.agent_status == "DEGRADED"
    assert result.attribution.active_return is not None  # 定量结果仍保留
    assert result.thesis_reviews[0].status == "success"
    assert result.risk.status == "ok"
    # 归因失败不产生 agent_output，其余两段各一条
    assert conn.execute("SELECT count(*) FROM agent_output").fetchone()[0] == 2


def test_single_position_thesis_failure_isolated(conn, config) -> None:
    """两仓：600519 thesis DEGRADED，600000 SUCCESS；互不影响。"""
    _seed_universe(conn)
    _seed_held_moutai(conn)
    _trade(conn, "acc_value", "600000", 10000, 10.0, "2025-12-16")
    _position(conn, "pos_bank", "acc_value", "600000", 10000, 10.0, thesis_id="thesis_2")
    _thesis(conn, "thesis_2", "600000")
    provider = _provider(thesis_by_code={"600519": "坏响应", "600000": _thesis_ok("pos_bank")})

    result = run_review_pipeline(
        conn, config, provider, account_id="acc_value", start=START, end=END
    )

    by_code = {i.code: i for i in result.thesis_reviews}
    assert by_code["600519"].status == "failed"
    assert by_code["600519"].run_id is not None  # 失败留痕
    assert by_code["600000"].status == "success"
    assert by_code["600000"].suggested_action == "hold"
    thesis_outputs = conn.execute(
        "SELECT count(*) FROM agent_output o JOIN agent_run r ON o.run_id = r.run_id "
        "WHERE r.agent_name = 'thesis_review'"
    ).fetchone()[0]
    assert thesis_outputs == 1


def test_position_without_thesis_is_skipped(conn, config) -> None:
    _seed_universe(conn)
    _seed_held_moutai(conn, thesis_id=None)
    provider = _provider()

    result = run_review_pipeline(
        conn, config, provider, account_id="acc_value", start=START, end=END
    )

    item = result.thesis_reviews[0]
    assert item.status == "skipped"
    assert "thesis" in (item.reason or "")
    assert not any("持仓 thesis 复核 Agent" in c[0][0].content for c in provider.calls)
    assert any("thesis 复核跳过 1/1" in n for n in result.notes)


def test_non_active_thesis_is_skipped(conn, config) -> None:
    _seed_universe(conn)
    _seed_held_moutai(conn)
    conn.execute("UPDATE thesis SET status = 'falsified' WHERE thesis_id = 'thesis_1'")
    provider = _provider()

    result = run_review_pipeline(
        conn, config, provider, account_id="acc_value", start=START, end=END
    )

    assert result.thesis_reviews[0].status == "skipped"
    assert "falsified" in (result.thesis_reviews[0].reason or "")


def test_no_risk_events_section_empty(conn, config) -> None:
    _seed_universe(conn)
    _seed_held_moutai(conn)
    provider = _provider()

    result = run_review_pipeline(
        conn, config, provider, account_id="acc_value", start=START, end=END
    )

    assert result.risk.status == "empty"
    assert result.risk.event_count == 0
    assert not any("风控解读 Agent" in c[0][0].content for c in provider.calls)


def test_bad_period_fails_fast(conn, config) -> None:
    provider = _provider()
    with pytest.raises(ValueError, match="复盘区间非法"):
        run_review_pipeline(
            conn,
            config,
            provider,
            account_id="acc_value",
            start=END,
            end=START,
        )
    assert provider.calls == []


def test_pipeline_replay_from_cassette(conn, config, tmp_path: Path) -> None:
    """录制一次完整复盘 → 重放（底层必炸）再跑，三段状态一致且零网络。"""
    _seed_universe(conn)
    _seed_held_moutai(conn)
    _risk_event(conn, "acc_value")
    recorder = CassetteProvider(
        _provider(), cassette_dir=tmp_path, agent="review_pipeline", case="e2e", mode="record"
    )
    first = run_review_pipeline(
        conn, config, recorder, account_id="acc_value", start=START, end=END
    )
    assert first.attribution.status == "ok"
    assert first.thesis_reviews[0].status == "success"
    assert first.risk.status == "ok"

    class Boom:
        def complete(self, messages: object, model: str, **params: object) -> object:
            raise AssertionError("重放模式不应触达底层 provider")

    replayer = CassetteProvider(
        Boom(),  # type: ignore[arg-type]
        cassette_dir=tmp_path,
        agent="review_pipeline",
        case="e2e",
        mode="replay",
    )
    second = run_review_pipeline(
        conn, config, replayer, account_id="acc_value", start=START, end=END
    )
    assert second.attribution.status == first.attribution.status
    assert second.attribution.active_return == first.attribution.active_return
    assert [i.status for i in second.thesis_reviews] == [i.status for i in first.thesis_reviews]
    assert [i.thesis_status for i in second.thesis_reviews] == [
        i.thesis_status for i in first.thesis_reviews
    ]
    assert second.risk.status == first.risk.status
    assert second.risk.event_count == first.risk.event_count
