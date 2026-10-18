"""价值初筛（P4.4 补全三件套：初筛/估值/反方中的初筛，零 LLM）。

排除法漏斗，与波段初筛（swing/screen.py）同构：
  第 0 步 能力圈一票否决（行业白/黑名单 + 个股黑名单，圈外不进入任何后续研究）；
  第 1 步 硬剔除（ST / 非 SH·SZ / 最新交易日无 bar=停牌退市）；
  第 2 步 质量漏斗（年度报告按 ann_date ≤ 筛选日防前视，宁缺勿错）：
          roe 连续达标记 sustain_years 年、连续盈利 min_profit_years 年、
          require_positive_fcf 时窗口 fcf 全正、最新年度负债率上限；
  第 3 步 估值闸门（排序不键）：pe_percentile ≤ 阈值，缺失（历史不足）不过闸；
  第 4 步 质量排序：roe 均值主键、fcf 均值并列键，全量入表不截断。

质量口径：roe 代理 ROIC（QMT 财务 5 表无 roic 字段，与止损扫描 engine/risk 同口径；
config 的 roic_min/roic_sustain_years 保留不消费，ROIC 数据源补齐后切换）。

候选池是每周快照（迁移 0010 value_candidate），不做状态机：人工挑候选 →
投委会（run_committee.py）→ 狩猎清单（watchlist.py add）。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

import duckdb

from viqtrader.config.loader import Config

# 年度报告行：(code, roe, fcf, net_profit, debt_ratio, revenue)
type _AnnualRow = tuple[str, float | None, float | None, float | None, float | None, float | None]

__all__ = [
    "ValueCandidate",
    "ValueScreenReport",
    "consecutive_pass_years",
    "consecutive_profit_years",
    "passes_circle_of_competence",
    "passes_quality",
    "passes_valuation_gate",
    "run_value_screen",
]


@dataclass(frozen=True, slots=True)
class ValueCandidate:
    """单只候选：质量与估值快照（落库前载体）。"""

    code: str
    market: str
    name: str
    industry: str | None
    roe_years: int
    roe_mean: float
    profit_years: int
    fcf_mean: float | None
    fcf_all_positive: bool
    debt_ratio_latest: float | None
    revenue_cagr: float | None
    pe_ttm: float | None
    pb: float | None
    dv_ratio: float | None
    pe_percentile: float | None
    gate_passed: bool
    quality_score: float


@dataclass(frozen=True, slots=True)
class ValueScreenReport:
    """初筛漏斗各级存活统计。"""

    screen_date: date
    universe: int
    after_circle: int
    after_hard: int
    after_quality: int
    gate_passed: int
    candidates: list[ValueCandidate] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 纯函数判定
# ---------------------------------------------------------------------------


def consecutive_pass_years(values: list[float | None], floor: float) -> int:
    """从最新一期往回数连续达标的年数（None 或不达标即断）。"""
    n = 0
    for v in values:
        if v is None or v < floor:
            break
        n += 1
    return n


def consecutive_profit_years(net_profits: list[float | None]) -> int:
    """从最新一期往回数连续盈利（>0）的年数（None 视为断）。"""
    n = 0
    for v in net_profits:
        if v is None or v <= 0:
            break
        n += 1
    return n


def passes_circle_of_competence(
    industry: str | None,
    code: str,
    *,
    industry_whitelist: list[str],
    industry_blacklist: list[str],
    stock_blacklist: list[str],
) -> bool:
    """能力圈负面清单：黑名单一票否决；白名单非空时仅留白名单（空 = 不启用）。"""
    if code in stock_blacklist:
        return False
    if industry is None:
        # 无行业标注（北交所债券等非股票已在市场层过滤；此处兜底宁缺勿错）
        return not industry_whitelist and not industry_blacklist
    if industry in industry_blacklist:
        return False
    # 白名单非空 = 启用白名单：圈外行业一律否决
    return not industry_whitelist or industry in industry_whitelist


def passes_quality(
    roes: list[float | None],
    fcfs: list[float | None],
    net_profits: list[float | None],
    debt_ratio_latest: float | None,
    *,
    roe_min: float,
    sustain_years: int,
    min_profit_years: int,
    require_positive_fcf: bool,
    max_debt_ratio: float | None,
) -> bool:
    """质量漏斗（序列按报告期降序，最新在前；窗口数据不足宁缺勿错）。

    roe：窗口 sustain_years 年全部达标；
    fcf：require_positive_fcf 时窗口全部 > 0（缺失不通过）；
    盈利：连续盈利年数 ≥ min_profit_years；
    负债率：最新年度 ≤ max_debt_ratio（缺失不通过）。
    """
    if len(roes) < sustain_years or any(v is None or v < roe_min for v in roes[:sustain_years]):
        return False
    if consecutive_profit_years(net_profits) < min_profit_years:
        return False
    # FCF：要求为正时窗口全部 > 0，任一缺失宁缺勿错
    if require_positive_fcf and not (
        len(fcfs) >= sustain_years and all(v is not None and v > 0 for v in fcfs[:sustain_years])
    ):
        return False
    # 负债率：缺失宁缺勿错
    return not (
        max_debt_ratio is not None
        and (debt_ratio_latest is None or debt_ratio_latest > max_debt_ratio)
    )


def passes_valuation_gate(pe_percentile: float | None, max_pct: float) -> bool:
    """估值闸门：自身历史分位不高于阈值；缺失（上市历史不足）不过闸。"""
    if pe_percentile is None:
        return False
    return pe_percentile <= max_pct


# ---------------------------------------------------------------------------
# 扫描装配
# ---------------------------------------------------------------------------


def run_value_screen(
    conn: duckdb.DuckDBPyConnection,
    config: Config,
    screen_date: date | None = None,
) -> ValueScreenReport:
    """全市场排除法漏斗，结果快照写 value_candidate（同日先删后插幂等）。

    screen_date 缺省为最新交易日；SQL 一次拉全市场年度报告内存分组，
    避免 5577 次逐只查询。
    """
    if screen_date is None:
        row = conn.execute("SELECT max(trade_date) FROM fact_daily_bar").fetchone()
        screen_date = row[0] if row and row[0] else date.today()

    # 行情/估值截面取「≤ 筛选日的最新交易日」——初筛日常为周末，最新 bar 在周五
    bar_day_row = conn.execute(
        "SELECT max(trade_date) FROM fact_daily_bar WHERE trade_date <= ?",
        [screen_date],
    ).fetchone()
    bar_day = bar_day_row[0] if bar_day_row and bar_day_row[0] else screen_date

    screening = config.get("value_track.screening", {}) or {}
    roe_min = float(screening.get("roe_min", 0.12))
    sustain_years = int(screening.get("roe_sustain_years", 3))
    min_profit_years = int(screening.get("min_consecutive_profit_years", 3))
    require_positive_fcf = bool(screening.get("require_positive_fcf", True))
    max_debt_ratio = screening.get("max_debt_ratio")
    max_debt_ratio = float(max_debt_ratio) if max_debt_ratio is not None else None
    pe_max = float(screening.get("pe_percentile_max", 0.5))

    circle = config.get("circle_of_competence", {}) or {}
    whitelist = [str(x) for x in circle.get("industry_whitelist", []) or []]
    blacklist = [str(x) for x in circle.get("industry_blacklist", []) or []]
    stock_blacklist = [str(x) for x in circle.get("stock_blacklist", []) or []]

    # --- 元数据 + 最新估值截面（与最新交易日对齐）---
    meta_rows = conn.execute(
        """
        SELECT d.code, d.market, d.name, d.industry,
               v.pe_ttm, v.pb, v.dv_ratio, v.pe_percentile
        FROM dim_stock d
        JOIN fact_daily_bar b
          ON b.code = d.code AND b.trade_date = ?
        LEFT JOIN fact_valuation v
          ON v.code = d.code AND v.trade_date = ?
        WHERE d.market IN ('SH', 'SZ')
        """,
        [bar_day, bar_day],
    ).fetchall()
    universe = len(meta_rows)

    # --- 年度报告（ann_date ≤ 筛选日防前视），按 code 分组、报告期降序 ---
    fin_rows = conn.execute(
        """
        SELECT code, roe, fcf, net_profit, debt_ratio, revenue
        FROM fact_financial
        WHERE month(report_period) = 12 AND ann_date <= ?
        ORDER BY code, report_period DESC
        """,
        [screen_date],
    ).fetchall()
    fin_by_code: dict[str, list[_AnnualRow]] = defaultdict(list)
    for r in fin_rows:
        fin_by_code[str(r[0])].append(r)

    candidates: list[ValueCandidate] = []
    after_circle = 0
    after_hard = 0

    for code, market, name, industry, pe, pb, dv, pe_pct in meta_rows:
        if not passes_circle_of_competence(
            industry,
            code,
            industry_whitelist=whitelist,
            industry_blacklist=blacklist,
            stock_blacklist=stock_blacklist,
        ):
            continue
        after_circle += 1

        if "ST" in (name or "").upper():
            continue
        after_hard += 1

        fin = fin_by_code.get(code, [])
        roes = [r[1] for r in fin]
        fcfs = [r[2] for r in fin]
        nps = [r[3] for r in fin]
        debt_latest = fin[0][4] if fin else None

        if not passes_quality(
            roes,
            fcfs,
            nps,
            debt_latest,
            roe_min=roe_min,
            sustain_years=sustain_years,
            min_profit_years=min_profit_years,
            require_positive_fcf=require_positive_fcf,
            max_debt_ratio=max_debt_ratio,
        ):
            continue

        window = fin[:sustain_years]
        roe_mean = sum(v for v in (r[1] for r in window) if v is not None) / len(window)
        fcf_vals = [r[2] for r in window]
        fcf_all_pos = bool(fcf_vals) and all(v is not None and v > 0 for v in fcf_vals)
        fcf_mean = (
            sum(v for v in fcf_vals if v is not None) / len(fcf_vals)
            if any(v is not None for v in fcf_vals)
            else None
        )
        revenue_cagr = _revenue_cagr([r[5] for r in window])

        gate = passes_valuation_gate(pe_pct, pe_max)
        candidates.append(
            ValueCandidate(
                code=code,
                market=market,
                name=name,
                industry=industry,
                roe_years=consecutive_pass_years(roes, roe_min),
                roe_mean=roe_mean,
                profit_years=consecutive_profit_years(nps),
                fcf_mean=fcf_mean,
                fcf_all_positive=fcf_all_pos,
                debt_ratio_latest=debt_latest,
                revenue_cagr=revenue_cagr,
                pe_ttm=pe,
                pb=pb,
                dv_ratio=dv,
                pe_percentile=pe_pct,
                gate_passed=gate,
                quality_score=roe_mean,
            )
        )

    # 质量排序：roe 均值主键、fcf 均值并列键（None 排后）、代码稳定序
    candidates.sort(key=lambda c: (-c.quality_score, -(c.fcf_mean or float("-inf")), c.code))
    gate_passed_n = sum(1 for c in candidates if c.gate_passed)

    _persist(conn, screen_date, candidates, str(config.content_hash()))
    return ValueScreenReport(
        screen_date=screen_date,
        universe=universe,
        after_circle=after_circle,
        after_hard=after_hard,
        after_quality=len(candidates),
        gate_passed=gate_passed_n,
        candidates=candidates,
    )


def _revenue_cagr(revenues: list[float | None]) -> float | None:
    """窗口营收复合增速。传入为报告期**降序**（最新在前）：末年=首元素、首年=尾元素；
    单年窗口或任一端缺失 → None。"""
    if len(revenues) < 2:
        return None
    last, first = revenues[0], revenues[-1]
    if first is None or last is None or first <= 0 or last <= 0:
        return None
    # 显式 float 转换：mypy 对 ** 幂运算的联合类型推断会放大为 Any
    ratio = float(last) / float(first)
    return float(ratio ** (1 / (len(revenues) - 1)) - 1)


def _persist(
    conn: duckdb.DuckDBPyConnection,
    screen_date: date,
    candidates: list[ValueCandidate],
    config_hash: str,
) -> None:
    """同日先删后插幂等；config_hash=Config.content_hash()（与 risk_event 同口径）。"""
    try:
        conn.execute("DELETE FROM value_candidate WHERE screen_date = ?", [screen_date])
        for c in candidates:
            conn.execute(
                """
                INSERT INTO value_candidate
                (screen_date, code, market, name, industry,
                 roe_years, roe_mean, profit_years, fcf_mean, fcf_all_positive,
                 debt_ratio_latest, revenue_cagr,
                 pe_ttm, pb, dv_ratio, pe_percentile, gate_passed,
                 quality_score, config_hash)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    screen_date,
                    c.code,
                    c.market,
                    c.name,
                    c.industry,
                    c.roe_years,
                    c.roe_mean,
                    c.profit_years,
                    c.fcf_mean,
                    c.fcf_all_positive,
                    c.debt_ratio_latest,
                    c.revenue_cagr,
                    c.pe_ttm,
                    c.pb,
                    c.dv_ratio,
                    c.pe_percentile,
                    c.gate_passed,
                    c.quality_score,
                    config_hash,
                ],
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
