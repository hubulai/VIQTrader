"""P4.4 投委会 Agent 测试：输入装配、端到端契约运行、DEGRADED 中止、会话落库、盒带重放。

全离线：LLM 一律 FakeProvider，数据库一律内存库 + 全迁移。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path
from typing import Any

import duckdb
import pytest

from viqtrader.agents.llm.base import Message
from viqtrader.agents.llm.cassette import CassetteProvider
from viqtrader.agents.llm.fake import FakeProvider
from viqtrader.agents.shared.db_lease import LeaseFn, hold
from viqtrader.agents.value import (
    load_valuation_inputs,
    run_committee,
    run_dissent_agent,
    run_valuation_agent,
)
from viqtrader.config.loader import Config
from viqtrader.store.migrate import migrate

_VALID_VALUATION: dict[str, Any] = {
    "code": "600519",
    "intrinsic_value_range": [1450.0, 1780.0],
    "current_price": 1520.0,
    "margin_of_safety": 0.05,
    "methods": [
        {"name": "DCF", "value": 1650.0, "assumptions": {"wacc": 0.09, "g": 0.03}},
        {"name": "股息贴现", "value": 1580.0, "assumptions": {"payout": 0.52}},
    ],
    "moat_rating": "strong",
    "moat_evidence": ["品牌定价权", "毛利率长期 > 90%"],
    "key_risks": ["消费降级", "批价下行"],
    "confidence": 0.72,
    "variant_perception": "我认为批价下行影响小于市场预期",
    "data_refs": ["fact_financial@2024-12-31", "fact_valuation@2026-10-15"],
}

_VALID_DISSENT: dict[str, Any] = {
    "code": "600519",
    "rebuttals": [
        {
            "target": "methods[0].assumptions.wacc",
            "attack": "9% 低于行业资本成本",
            "severity": "high",
        },
        {"target": "moat_evidence[0]", "attack": "直销占比下滑削弱定价权", "severity": "medium"},
    ],
    "fatal_issues": [],
    "verdict": "估值偏乐观，需补充渠道库存数据",
    "needs_data": ["渠道库存", "直销占比"],
}


def _seed_maotai(conn) -> None:
    """种入茅台的财务/估值/分红/公告与一份已解析研报。"""
    conn.execute(
        """
        INSERT INTO fact_financial_version
            (code, report_period, ann_date, revenue, net_profit, roe, gross_margin,
             ocf, fcf, eps, bps, debt_ratio)
        VALUES
            ('600519', '2024-12-31', '2025-04-02', 1.74e11, 8.63e10, 0.34, 0.917,
             9.2e10, 6.5e10, 68.64, 190.0, 0.20),
            ('600519', '2024-09-30', '2024-10-25', 1.2e11, 6.0e10, 0.26, 0.915,
             6.1e10, 4.4e10, 47.79, 180.0, 0.21)
        """
    )
    conn.execute(
        """
        INSERT INTO fact_valuation
            (code, trade_date, pe_ttm, pb, ps_ttm, dv_ratio, market_cap, pe_percentile)
        VALUES
            ('600519', '2026-10-15', 21.5, 7.8, 14.2, 3.2, 2.1e12, 0.35),
            ('000858', '2026-10-15', 15.2, 3.1, 4.0, 4.5, 5.0e11, 0.28)
        """
    )
    conn.execute(
        "INSERT INTO fact_dividend (code, ex_date, pay_date, dividend_per_share, dividend_type)"
        " VALUES ('600519', '2025-06-26', NULL, 276.24, '现金')"
    )
    conn.execute(
        "INSERT INTO fact_disclosure (code, ann_date, title, category, summary)"
        " VALUES ('600519', '2025-04-02', '2024年年度报告', '财报', '营收净利双增，分红率提升')"
    )
    conn.execute(
        "INSERT INTO document (doc_id, file_path, content_hash, doc_type, code, industry,"
        " org, publish_date, parse_status)"
        " VALUES ('doc1', 'x.pdf', 'h1', '个股研报', '600519', '白酒',"
        " '某券商', '2025-04-10', 'parsed')"
    )
    conn.execute(
        "INSERT INTO document_analysis (analysis_id, doc_id, result_json, model, prompt_version)"
        ' VALUES (\'a1\', \'doc1\', \'{"摘要": "盈利预测上调", "核心假设": ["提价"]}\','
        " 'm', 'doc.v1')"
    )


def _lease(conn: duckdb.DuckDBPyConnection) -> LeaseFn:
    """内存库租约：沿用 fixture 连接（内存库无法重连），不关闭。"""
    return lambda: hold(conn)


def _routed_provider(valuation_text: str, dissent_text: str) -> FakeProvider:
    """按 prompt 全文路由：估值 prompt 含「深度估值」，反方 prompt 含「反方 Agent」。

    匹配全文而非最后一条——重试轮次里最后一条是错误提示模板。
    """

    def responder(messages: Sequence[Message], model: str, params: dict[str, Any]) -> str:
        text = "\n".join(m.content for m in messages)
        if "深度估值" in text:
            return valuation_text
        if "反方 Agent" in text:
            return dissent_text
        raise AssertionError(f"未预期的 prompt: {text[:60]}")

    return FakeProvider(responder)


# ---------- 输入装配 ----------


def test_load_valuation_inputs_shape(conn, config: Config) -> None:
    _seed_maotai(conn)
    bundle = load_valuation_inputs(conn, "600519", name="贵州茅台", peer_codes=("000858",))

    assert bundle["code"] == "600519"
    assert isinstance(bundle["financials"], list)
    assert bundle["financials"][0]["report_period"] == "2024-12-31"  # 按报告期倒序
    assert bundle["financials"][0]["ann_date"] == "2025-04-02"  # 公告日随行（point-in-time）
    assert bundle["valuation"]["pe_ttm"] == 21.5
    assert bundle["dividends"][0]["dividend_per_share"] == 276.24
    assert bundle["peers"] == [{"code": "000858", "pe_ttm": 15.2, "pb": 3.1, "dv_ratio": 4.5}]
    consensus = bundle["consensus"]
    assert "某券商" in consensus and "提价" in consensus  # 机构与核心假设随行


def test_load_valuation_inputs_empty_db(conn) -> None:
    bundle = load_valuation_inputs(conn, "999999", name="不存在")
    assert bundle["financials"] == "无财务数据"
    assert bundle["valuation"] == "无估值数据"
    assert bundle["consensus"] == "无外部研报输入"


# ---------- 估值 / 反方 Agent ----------


def test_valuation_agent_success_writes_audit(conn, config: Config) -> None:
    _seed_maotai(conn)
    provider = _routed_provider(json.dumps(_VALID_VALUATION), "{}")
    run_id, run = run_valuation_agent(
        conn, config, provider, code="600519", name="贵州茅台", peer_codes=("000858",)
    )

    assert run.status == "SUCCESS"
    assert run.validation.parsed is not None
    audit = conn.execute(
        "SELECT agent_name, prompt_version, input_snapshot FROM agent_run WHERE run_id = ?",
        [run_id],
    ).fetchone()
    assert audit is not None
    assert audit[0] == "valuation"
    assert audit[1] == "valuation.v2"
    assert json.loads(audit[2])["code"] == "600519"  # 输入快照可回溯
    n_output = conn.execute(
        "SELECT COUNT(*) FROM agent_output WHERE run_id = ?", [run_id]
    ).fetchone()
    assert n_output is not None and n_output[0] == 1


def test_valuation_agent_prompt_carries_data(conn, config: Config) -> None:
    _seed_maotai(conn)
    provider = _routed_provider(json.dumps(_VALID_VALUATION), "{}")
    run_valuation_agent(conn, config, provider, code="600519", name="贵州茅台")

    prompt = provider.calls[0][0][-1].content
    assert "600519" in prompt and "贵州茅台" in prompt
    assert "21.5" in prompt  # 最新估值截面
    assert "提价" in prompt  # 研报共识进入输入
    assert '"code": str' in prompt  # 输出契约提示随行


def test_dissent_agent_receives_valuation_payload(conn, config: Config) -> None:
    _seed_maotai(conn)
    provider = _routed_provider("{}", json.dumps(_VALID_DISSENT))
    run_id, run = run_dissent_agent(conn, config, provider, _VALID_VALUATION, code="600519")

    assert run.status == "SUCCESS"
    assert run.validation.parsed is not None
    prompt = provider.calls[0][0][-1].content
    assert "0.05" in prompt  # 估值方安全边际进入反方输入
    audit = conn.execute(
        "SELECT agent_name, prompt_version FROM agent_run WHERE run_id = ?", [run_id]
    ).fetchone()
    assert audit == ("dissent", "dissent.v2")


# ---------- 投委会编排 ----------


def test_committee_success_writes_session(conn, config: Config) -> None:
    _seed_maotai(conn)
    provider = _routed_provider(json.dumps(_VALID_VALUATION), json.dumps(_VALID_DISSENT))
    summary = run_committee(
        _lease(conn), config, provider, code="600519", name="贵州茅台", peer_codes=("000858",)
    )

    assert not summary.aborted
    assert summary.valuation_status == "SUCCESS"
    assert summary.dissent_status == "SUCCESS"
    assert summary.valuation_output_id is not None
    assert summary.dissent_output_id is not None

    session = conn.execute(
        """
        SELECT valuation_output_id, dissent_output_id, final_decision, decided_by, notes
        FROM committee_session WHERE session_id = ?
        """,
        [summary.session_id],
    ).fetchone()
    assert session is not None
    assert session[0] == summary.valuation_output_id
    assert session[1] == summary.dissent_output_id
    assert session[2] is None and session[3] is None  # 最终决策留人工
    assert "待人工拍板" in session[4]
    assert "72%" in session[4] and "2 条" in session[4]  # 摘要含双方要点


def test_committee_aborts_when_valuation_degraded(conn, config: Config) -> None:
    _seed_maotai(conn)
    dissent_seen: list[str] = []

    def responder(messages: Sequence[Message], model: str, params: dict[str, Any]) -> str:
        text = "\n".join(m.content for m in messages)
        if "反方 Agent" in text:
            dissent_seen.append(text)
            return json.dumps(_VALID_DISSENT)
        return "这不是 JSON"

    summary = run_committee(
        _lease(conn), config, FakeProvider(responder), code="600519", name="贵州茅台"
    )

    assert summary.aborted
    assert summary.valuation_status == "DEGRADED"
    assert summary.valuation_output_id is None
    assert not dissent_seen  # 反方未被调用
    session = conn.execute(
        "SELECT valuation_output_id, notes FROM committee_session WHERE session_id = ?",
        [summary.session_id],
    ).fetchone()
    assert session is not None
    assert session[0] is None
    assert "中止" in session[1]


def test_committee_keeps_valuation_when_dissent_degraded(conn, config: Config) -> None:
    _seed_maotai(conn)
    provider = _routed_provider(json.dumps(_VALID_VALUATION), "完全不是 JSON")
    summary = run_committee(_lease(conn), config, provider, code="600519", name="贵州茅台")

    assert summary.aborted
    assert summary.valuation_status == "SUCCESS"
    assert summary.dissent_status == "DEGRADED"
    assert summary.valuation_output_id is not None
    assert summary.dissent_output_id is None


def test_committee_replay_from_cassette(conn, config: Config, tmp_path: Path) -> None:
    """录制一次完整投委会 → 换重放 provider（底层会抛错）重跑，结果一致且零网络。"""
    _seed_maotai(conn)
    recorder = CassetteProvider(
        _routed_provider(json.dumps(_VALID_VALUATION), json.dumps(_VALID_DISSENT)),
        cassette_dir=tmp_path,
        agent="committee",
        case="e2e",
        mode="record",
    )
    first = run_committee(_lease(conn), config, recorder, code="600519", name="贵州茅台")
    assert not first.aborted

    class Boom:
        def complete(self, messages: object, model: str, **params: object) -> object:
            raise AssertionError("重放模式不应触达底层 provider")

    replayer = CassetteProvider(
        Boom(),  # type: ignore[arg-type]
        cassette_dir=tmp_path,
        agent="committee",
        case="e2e",
        mode="replay",
    )
    second = run_committee(_lease(conn), config, replayer, code="600519", name="贵州茅台")
    assert second.aborted == first.aborted
    assert second.valuation_status == first.valuation_status
    assert second.dissent_status == first.dissent_status
    n_sessions = conn.execute("SELECT COUNT(*) FROM committee_session").fetchone()
    assert n_sessions is not None and n_sessions[0] == 2


def test_llm_phase_holds_no_db_lock(config: Config, tmp_path: Path) -> None:
    """LLM 调用期间不得持有 DuckDB 连接：另一条只读连接必须能打开同一库。

    回归用例：投委会曾整段持锁，深度模型 600 秒调用期间 Web 控制台持续 503。
    """
    db_path = tmp_path / "committee.duckdb"
    seed = duckdb.connect(str(db_path))
    migrate(seed)
    seed.close()

    probed: list[str] = []

    def responder(messages: Sequence[Message], model: str, params: dict[str, Any]) -> str:
        text = "\n".join(m.content for m in messages)
        # 模拟 Web 控制台：独立只读连接。若仍被写连接占锁，此处会抛 IOException
        with duckdb.connect(str(db_path), read_only=True) as probe:
            probe.execute("SELECT COUNT(*) FROM committee_session").fetchone()
        is_valuation = "深度估值" in text
        probed.append("估值" if is_valuation else "反方")
        return json.dumps(_VALID_VALUATION) if is_valuation else json.dumps(_VALID_DISSENT)

    summary = run_committee(
        lambda: closing(duckdb.connect(str(db_path))),
        config,
        FakeProvider(responder),
        code="600519",
        name="贵州茅台",
    )

    assert not summary.aborted
    assert probed == ["估值", "反方"]  # 两次 LLM 调用期间均可独立读库
    with closing(duckdb.connect(str(db_path), read_only=True)) as conn:
        n_sessions = conn.execute("SELECT COUNT(*) FROM committee_session").fetchone()
    assert n_sessions is not None and n_sessions[0] == 1


@pytest.mark.parametrize("missing", ["financials", "valuation"])
def test_prompt_renders_missing_data_gracefully(conn, config: Config, missing: str) -> None:
    """缺财务或缺估值时 prompt 仍可渲染（如实标注无数据），Agent 可运行。"""
    if missing == "valuation":
        _seed_maotai(conn)
        conn.execute("DELETE FROM fact_valuation WHERE code = '600519'")
    provider = _routed_provider(json.dumps(_VALID_VALUATION), "{}")
    run_id, run = run_valuation_agent(conn, config, provider, code="600519", name="贵州茅台")
    assert run.status == "SUCCESS"
    prompt = provider.calls[0][0][-1].content
    if missing == "valuation":
        assert "无估值数据" in prompt
    del run_id
