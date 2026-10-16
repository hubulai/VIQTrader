"""P4.7 学习上下文注入测试：纯函数打分/检索/格式化 + 内存库装配 + Agent 集成。

验证方式（任务卡）：空库不报错，有库时案例出现在 prompt。
全离线：LLM 一律 FakeProvider，数据库一律内存库 + 全迁移。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from viqtrader.agents.context import (
    CaseRow,
    ScorecardRow,
    build_learning_context,
    format_case,
    format_scorecard,
    render_learning_context,
    score_case,
    select_cases,
)
from viqtrader.agents.llm.fake import FakeProvider
from viqtrader.agents.value import run_dissent_agent, run_valuation_agent
from viqtrader.config.loader import Config


def _case(
    case_id: str,
    *,
    code: str | None = None,
    industry: str | None = None,
    thesis_type: str | None = None,
    failure_mode: str | None = None,
    title: str = "案例",
    postmortem: str | None = None,
    created_at: datetime = datetime(2026, 1, 1),
) -> CaseRow:
    return CaseRow(
        case_id=case_id,
        code=code,
        industry=industry,
        thesis_type=thesis_type,
        failure_mode=failure_mode,
        title=title,
        postmortem=postmortem,
        created_at=created_at,
    )


def _scorecard(**overrides: Any) -> ScorecardRow:
    base: dict[str, Any] = {
        "agent_name": "valuation",
        "decision_type": "valuation",
        "sample_size": 20,
        "hit_rate": 0.45,
        "error_mean": 0.123,
        "error_std": 0.081,
        "false_positive_rate": 0.05,
    }
    base.update(overrides)
    return ScorecardRow(**base)


# ---------- 纯函数：score_case ----------


def test_score_case_weights() -> None:
    case = _case("c1", code="600519", industry="白酒", thesis_type="高护城河溢价")
    assert score_case(case, code="600519") == 4
    assert score_case(case, industry="白酒") == 2
    assert score_case(case, thesis_type="高护城河溢价") == 1
    assert score_case(case, code="600519", industry="白酒", thesis_type="高护城河溢价") == 7


def test_score_case_no_match_and_none_query() -> None:
    case = _case("c1", code="600519", industry="白酒")
    assert score_case(case, code="000001", industry="地产") == 0
    assert score_case(case) == 0  # 查询维度全 None 不得分
    assert score_case(case, code=None, industry="白酒") == 2  # None 维度跳过


def test_score_case_null_fields_never_match() -> None:
    case = _case("c1", code=None, industry=None)  # 库中字段缺失
    assert score_case(case, code=None, industry=None) == 0


# ---------- 纯函数：select_cases ----------


def test_select_cases_orders_by_score_then_recency() -> None:
    old = datetime(2026, 1, 1)
    new = datetime(2026, 3, 1)
    cases = [
        _case("industry_only", industry="白酒", created_at=new),
        _case("code_old", code="600519", created_at=old),
        _case("code_new", code="600519", created_at=new),
        _case("irrelevant", code="000001", industry="地产"),
    ]
    hits = select_cases(cases, code="600519", industry="白酒", top_k=5)
    assert [c.case_id for c in hits] == ["code_new", "code_old", "industry_only"]
    # 无关案例被剔除，不硬塞凑数


def test_select_cases_top_k_truncates() -> None:
    cases = [_case(f"c{i}", code="600519", created_at=datetime(2026, 1, i + 1)) for i in range(6)]
    hits = select_cases(cases, code="600519", top_k=3)
    assert len(hits) == 3
    assert hits[0].case_id == "c5"  # 最新优先


def test_select_cases_all_zero_returns_empty() -> None:
    cases = [_case("c1", code="000001"), _case("c2", industry="地产")]
    assert select_cases(cases, code="600519", industry="白酒", top_k=5) == []


# ---------- 纯函数：格式化 ----------


def test_format_case_full_and_sparse() -> None:
    full = _case(
        "c1",
        code="600519",
        industry="白酒",
        thesis_type="高护城河溢价",
        failure_mode="过度乐观",
        title="茅台估值偏高案例",
        postmortem="实际增速不及假设",
        created_at=datetime(2026, 1, 15),
    )
    text = format_case(1, full)
    assert text.startswith("1. [2026-01-15 | 600519 | 白酒 | 高护城河溢价] 茅台估值偏高案例")
    assert "（失败模式：过度乐观）" in text
    assert "尸检结论：实际增速不及假设" in text

    sparse = _case("c2", title="只有标题")
    assert format_case(2, sparse) == "2. [2026-01-01 | - | - | -] 只有标题"


def test_format_scorecard_full() -> None:
    text = format_scorecard(_scorecard(), decision_type="valuation")
    assert text == (
        "近 20 次估值，命中率 45.0%，平均误差 +12.3%（正值=高估），" "误差波动 8.1%，误报率 5.0%。"
    )


def test_format_scorecard_none_fields_skipped() -> None:
    row = _scorecard(error_mean=None, error_std=None, false_positive_rate=None)
    assert format_scorecard(row, decision_type="valuation") == "近 20 次估值，命中率 45.0%。"
    assert format_scorecard(None, decision_type="valuation") == ""


def test_format_scorecard_dissent_has_no_overestimate_note() -> None:
    row = _scorecard(agent_name="dissent", decision_type="dissent", error_mean=0.1)
    text = format_scorecard(row, decision_type="dissent")
    assert "（正值=高估）" not in text
    assert "近 20 次反方校验" in text


def test_render_learning_context_empty_falls_back() -> None:
    text = render_learning_context([], None, decision_type="valuation")
    assert "暂无历史案例与评分卡记录" in text


def test_render_learning_context_sections() -> None:
    case = _case("c1", code="600519", title="茅台案例", postmortem="结局")
    only_cases = render_learning_context([case], None, decision_type="valuation")
    assert "相似历史案例" in only_cases and "茅台案例" in only_cases
    assert "评分卡" not in only_cases  # 无评分卡该节省略
    assert "不得作为 data_refs 引用" in only_cases  # 防递归污染标注

    only_scorecard = render_learning_context([], _scorecard(), decision_type="valuation")
    assert "评分卡（valuation）" in only_scorecard
    assert "相似历史案例" not in only_scorecard


# ---------- 内存库装配 ----------


def _seed_learning_data(conn) -> None:
    conn.execute(
        "INSERT INTO dim_stock (code, market, name, industry, list_date, is_hk_connect)"
        " VALUES ('600519', 'SH', '贵州茅台', '白酒', '2001-08-27', FALSE)"
    )
    conn.execute(
        """
        INSERT INTO case_library
            (case_id, code, industry, thesis_type, failure_mode, title, postmortem, created_at)
        VALUES
            ('case_stock', '600519', '白酒', '高护城河溢价', '过度乐观',
             '茅台高估值买入教训', '增速证伪，三年收益为负', '2026-01-15'),
            ('case_industry', '000858', '白酒', '困境反转', NULL,
             '五粮液反转误判', '渠道库存高估去化速度', '2026-02-01'),
            ('case_other', '601318', '保险', '低估值修复', NULL,
             '平安低估值陷阱', '地产敞口拖累', '2026-03-01')
        """
    )
    conn.execute(
        """
        INSERT INTO agent_scorecard
            (agent_name, decision_type, window_end, sample_size, hit_rate,
             error_mean, error_std, false_positive_rate)
        VALUES
            ('valuation', 'valuation', '2026-09-30', 20, 0.45, 0.123, 0.081, 0.05),
            ('valuation', 'valuation', '2026-06-30', 10, 0.30, 0.200, 0.100, 0.10)
        """
    )


def test_build_context_empty_db_degrades(conn, config: Config) -> None:
    text = build_learning_context(
        conn,
        config,
        agent_name="valuation",
        decision_type="valuation",
        code="600519",
        market="SH",
    )
    assert "暂无历史案例与评分卡记录" in text  # 空库不报错，降级文案


def test_build_context_cases_and_scorecard_appear(conn, config: Config) -> None:
    _seed_learning_data(conn)
    text = build_learning_context(
        conn,
        config,
        agent_name="valuation",
        decision_type="valuation",
        code="600519",
        market="SH",
    )
    assert "茅台高估值买入教训" in text  # 同标的第一
    assert "五粮液反转误判" in text  # 同行业（dim_stock 反查白酒）入选
    assert "平安低估值陷阱" not in text  # 无关案例剔除
    assert text.index("茅台高估值买入教训") < text.index("五粮液反转误判")  # 分值排序
    assert "近 20 次估值，命中率 45.0%" in text  # window_end 最新一行
    assert "近 10 次" not in text


def test_build_context_respects_top_k_config(conn, config: Config) -> None:
    for i in range(7):
        conn.execute(
            "INSERT INTO case_library (case_id, code, title, created_at) VALUES (?, ?, ?, ?)",
            [f"c{i}", "600519", f"案例{i}", f"2026-01-0{i + 1}"],
        )
    text = build_learning_context(
        conn, config, agent_name="dissent", decision_type="dissent", code="600519"
    )
    assert "案例6" in text and "案例2" in text  # top_k=5：最新 5 条
    assert "案例1" not in text and "案例0" not in text


# ---------- Agent 集成（FakeProvider 零网络） ----------


def _echo_provider(responses: list[str]) -> FakeProvider:
    iterator = iter(responses)
    return FakeProvider(lambda messages, model, params: next(iterator))


def test_valuation_agent_prompt_carries_learning_context(conn, config: Config) -> None:
    _seed_learning_data(conn)
    payload = json.dumps(
        {
            "code": "600519",
            "intrinsic_value_range": [1450.0, 1780.0],
            "current_price": 1520.0,
            "margin_of_safety": 0.05,
            "methods": [{"name": "DCF", "value": 1650.0, "assumptions": {"wacc": 0.09}}],
            "moat_rating": "strong",
            "moat_evidence": ["品牌"],
            "key_risks": ["批价"],
            "confidence": 0.7,
            "variant_perception": "差异",
            "data_refs": ["fact_financial@2024-12-31"],
        }
    )
    run_id, run = run_valuation_agent(
        conn, config, _echo_provider([payload]), code="600519", name="贵州茅台"
    )
    assert run.status == "SUCCESS"
    snapshot = conn.execute(
        "SELECT input_snapshot FROM agent_run WHERE run_id = ?", [run_id]
    ).fetchone()
    assert snapshot is not None
    learning = json.loads(snapshot[0])["learning_context"]  # 注入内容随快照可还原
    assert "茅台高估值买入教训" in learning
    assert "近 20 次估值" in learning


def test_dissent_agent_prompt_carries_learning_context(conn, config: Config) -> None:
    _seed_learning_data(conn)
    payload = json.dumps(
        {
            "code": "600519",
            "rebuttals": [{"target": "methods[0]", "attack": "假设乐观", "severity": "high"}],
            "fatal_issues": [],
            "verdict": "偏乐观",
            "needs_data": [],
        }
    )
    provider = _echo_provider([payload])
    run_id, run = run_dissent_agent(conn, config, provider, {"code": "600519"}, code="600519")
    assert run.status == "SUCCESS"
    prompt = provider.calls[0][0][-1].content
    assert "茅台高估值买入教训" in prompt  # 案例进反方 prompt（code 维度）
    snapshot = conn.execute(
        "SELECT input_snapshot FROM agent_run WHERE run_id = ?", [run_id]
    ).fetchone()
    assert snapshot is not None
    assert "茅台高估值买入教训" in json.loads(snapshot[0])["learning_context"]
