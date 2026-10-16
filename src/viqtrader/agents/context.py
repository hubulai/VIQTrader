"""学习上下文注入（设计方案第十一节，P4.7）：所有 Agent 的统一自我纠偏入口。

每个 Agent 在 render prompt 前调用 `build_learning_context`，注入两类学习上下文：
1. `case_library` 检索的 top-K 相似历史案例（含尸检结局）——确定性纯函数打分，
   不引入向量检索（个人平台样本量为几十条级，确定性可回放优先）；
2. 该 Agent 自己的最新评分卡摘要（多维正交指标分开呈现，不合成单一分数）。

红线遵守：
- 只读不写——案例库/评分卡的数据生产归 learning 回填任务（P5.3），本模块对空库
  优雅降级为固定文案（明确告知 LLM 是有意为空，而非数据遗漏）；
- 注入文本统一标注「非输入数据，不得作为 data_refs 引用」，防递归污染；
- 注入内容随 input_snapshot 落库（由调用方负责），进 prompt 的内容必须可还原。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import duckdb

from viqtrader.config.loader import Config

# 相似度分值权重：同标的 > 同行业 > 同 thesis 类型（确定性、可单测、可回放）
_SCORE_CODE = 4
_SCORE_INDUSTRY = 2
_SCORE_THESIS_TYPE = 1

_CASE_SCAN_LIMIT = 200  # 全库扫描封顶（个人平台年积累几十条，200 足够覆盖）
_DEFAULT_TOP_K = 5

_EMPTY_CONTEXT = "## 学习上下文\n（暂无历史案例与评分卡记录，按常规流程分析即可）"

_HEADER = "## 学习上下文（历史经验，供自我纠偏；" "非输入数据，不得作为 data_refs 引用）"

# decision_type 的中文展示名（评分卡摘要用）；未登记类型回退原值
_DECISION_TYPE_ZH = {"valuation": "估值", "dissent": "反方校验"}


@dataclass(frozen=True, slots=True)
class CaseRow:
    """`case_library` 表行（检索与渲染用子集）。"""

    case_id: str
    code: str | None
    industry: str | None
    thesis_type: str | None
    failure_mode: str | None
    title: str
    postmortem: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ScorecardRow:
    """`agent_scorecard` 表行（摘要渲染用子集，metrics_json 首版不解析）。"""

    agent_name: str
    decision_type: str
    sample_size: int
    hit_rate: float | None
    error_mean: float | None
    error_std: float | None
    false_positive_rate: float | None


# ---------------- 纯函数：打分 / 检索 / 格式化（零 DB 依赖，直接单测） ----------------


def score_case(
    case: CaseRow,
    *,
    code: str | None = None,
    industry: str | None = None,
    thesis_type: str | None = None,
) -> int:
    """案例与查询情境的相似度分值。查询维度为 None 时不参与打分。"""
    score = 0
    if code is not None and case.code == code:
        score += _SCORE_CODE
    if industry is not None and case.industry == industry:
        score += _SCORE_INDUSTRY
    if thesis_type is not None and case.thesis_type == thesis_type:
        score += _SCORE_THESIS_TYPE
    return score


def select_cases(
    cases: list[CaseRow],
    *,
    code: str | None = None,
    industry: str | None = None,
    thesis_type: str | None = None,
    top_k: int = _DEFAULT_TOP_K,
) -> list[CaseRow]:
    """top-K 检索：仅保留得分 >0 者，按（分值降序, 创建时间降序）截断。

    全库 0 分时返回空列表——不硬塞无关案例，守住「相似情境」语义。
    """
    scored = [
        (score_case(c, code=code, industry=industry, thesis_type=thesis_type), c) for c in cases
    ]
    hits = [(s, c) for s, c in scored if s > 0]
    hits.sort(key=lambda pair: (pair[0], pair[1].created_at), reverse=True)
    return [c for _, c in hits[: max(0, top_k)]]


def format_case(index: int, case: CaseRow) -> str:
    """单条案例渲染为两行 Markdown（标题行 + 尸检结论）。"""
    date_str = case.created_at.date().isoformat()
    slots = " | ".join([date_str, case.code or "-", case.industry or "-", case.thesis_type or "-"])
    title = case.title
    if case.failure_mode:
        title += f"（失败模式：{case.failure_mode}）"
    lines = [f"{index}. [{slots}] {title}"]
    if case.postmortem:
        lines.append(f"   尸检结论：{case.postmortem}")
    return "\n".join(lines)


def format_scorecard(row: ScorecardRow | None, *, decision_type: str) -> str:
    """评分卡摘要：多维正交指标分开呈现，None 字段跳过；无记录返回空串。"""
    if row is None:
        return ""
    zh = _DECISION_TYPE_ZH.get(decision_type, decision_type)
    parts = [f"近 {row.sample_size} 次{zh}"]
    if row.hit_rate is not None:
        parts.append(f"命中率 {row.hit_rate:.1%}")
    if row.error_mean is not None:
        note = "（正值=高估）" if decision_type == "valuation" else ""
        parts.append(f"平均误差 {row.error_mean:+.1%}{note}")
    if row.error_std is not None:
        parts.append(f"误差波动 {row.error_std:.1%}")
    if row.false_positive_rate is not None:
        parts.append(f"误报率 {row.false_positive_rate:.1%}")
    return "，".join(parts) + "。"


def render_learning_context(
    cases: list[CaseRow],
    scorecard: ScorecardRow | None,
    *,
    decision_type: str,
) -> str:
    """组合案例与评分卡为注入文本块；两者皆空时返回空库降级文案。"""
    if not cases and scorecard is None:
        return _EMPTY_CONTEXT
    sections = [_HEADER]
    if cases:
        lines = [format_case(i + 1, c) for i, c in enumerate(cases)]
        sections.append("### 相似历史案例（含结局）\n" + "\n".join(lines))
    summary = format_scorecard(scorecard, decision_type=decision_type)
    if summary:
        sections.append(f"### 我的近期评分卡（{decision_type}）\n{summary}")
    return "\n".join(sections)


# ---------------- DB 装配（唯一 IO 边界） ----------------


def _load_industry(conn: duckdb.DuckDBPyConnection, code: str, market: str) -> str | None:
    row = conn.execute(
        "SELECT industry FROM dim_stock WHERE code = ? AND market = ? LIMIT 1",
        [code, market],
    ).fetchone()
    return None if row is None or row[0] is None else str(row[0])


def _load_cases(conn: duckdb.DuckDBPyConnection, *, limit: int) -> list[CaseRow]:
    rows = conn.execute(
        """
        SELECT case_id, code, industry, thesis_type, failure_mode, title,
               postmortem, CAST(created_at AS TIMESTAMP)  -- 去时区，免 pytz 依赖
        FROM case_library ORDER BY created_at DESC LIMIT ?
        """,
        [limit],
    ).fetchall()
    return [
        CaseRow(
            case_id=str(r[0]),
            code=r[1],
            industry=r[2],
            thesis_type=r[3],
            failure_mode=r[4],
            title=str(r[5]),
            postmortem=r[6],
            created_at=r[7],
        )
        for r in rows
    ]


def _load_scorecard(
    conn: duckdb.DuckDBPyConnection, *, agent_name: str, decision_type: str
) -> ScorecardRow | None:
    row = conn.execute(
        """
        SELECT agent_name, decision_type, sample_size, hit_rate, error_mean,
               error_std, false_positive_rate
        FROM agent_scorecard
        WHERE agent_name = ? AND decision_type = ?
        ORDER BY window_end DESC LIMIT 1
        """,
        [agent_name, decision_type],
    ).fetchone()
    if row is None:
        return None
    return ScorecardRow(
        agent_name=str(row[0]),
        decision_type=str(row[1]),
        sample_size=int(row[2]),
        hit_rate=row[3],
        error_mean=row[4],
        error_std=row[5],
        false_positive_rate=row[6],
    )


def build_learning_context(
    conn: duckdb.DuckDBPyConnection,
    config: Config,
    *,
    agent_name: str,
    decision_type: str,
    code: str | None = None,
    market: str | None = None,
    thesis_type: str | None = None,
) -> str:
    """装配某 Agent 的学习上下文注入文本（所有 Agent 的统一入口）。

    - 给了 code + market 时反查 `dim_stock.industry` 补充行业检索维度；
    - top_k 读 `learning.case_retrieval_top_k`（缺省 5）；
    - 空库 / 无相似案例且无评分卡 → 返回固定降级文案，不抛错。
    """
    top_k_raw: Any = config.get("learning.case_retrieval_top_k", _DEFAULT_TOP_K)
    top_k = int(top_k_raw) if top_k_raw is not None else _DEFAULT_TOP_K

    industry: str | None = None
    if code is not None and market is not None:
        industry = _load_industry(conn, code, market)

    cases = select_cases(
        _load_cases(conn, limit=_CASE_SCAN_LIMIT),
        code=code,
        industry=industry,
        thesis_type=thesis_type,
        top_k=top_k,
    )
    scorecard = _load_scorecard(conn, agent_name=agent_name, decision_type=decision_type)
    return render_learning_context(cases, scorecard, decision_type=decision_type)


__all__ = [
    "CaseRow",
    "ScorecardRow",
    "build_learning_context",
    "format_case",
    "format_scorecard",
    "render_learning_context",
    "score_case",
    "select_cases",
]
