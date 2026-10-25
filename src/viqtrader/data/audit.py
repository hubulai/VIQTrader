"""P7 audit_db：库况体检（全量质检，只读离线）。

与 P1.6 ``data/quality.py``（逐 code 单表轻量质检）互补：本模块做**全库截面**体检，
只读 DuckDB，不触网、不写库。v1 范围限定三块：

- **financial**（fact_financial 快照 + fact_financial_version 多版本表）：标的覆盖 /
  期数分布 / 非空率 / 前视计数 / 公告日滞后分布 / 口径一致性；
- **valuation**（fact_valuation）：标的覆盖 / 非空率；
- **勾稽**（fact_valuation x fact_daily_bar x fact_share_structure）：估值行↔行情行匹配、
  市值口径勾稽（market_cap ≈ close x as-of 总股本）。

外加**随机抽样对账**（确定性种子，打印样本行供人工核对）。

三态判定 ``OK`` / ``WARN`` / ``FAIL``：

- ``OK``   = 指标满足阈值；
- ``WARN`` = 数据不足以判定（空库 / 样本不足 / 分母为 0），显式降级不抛异常；
- ``FAIL`` = 明确违反阈值，或检查执行期异常。

阈值全部外置 config ``audit:`` 节（见 config/default.yaml）。CLI：``scripts/audit_db.py``。
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

import duckdb

from viqtrader.config.loader import Config

from .financials import QUARTER_MONTHS

__all__ = [
    "AuditFinding",
    "AuditReport",
    "AuditSample",
    "AuditState",
    "audit_database",
    "caliber_balance_violations",
    "flow_monotonic_count",
    "judge_ann_lag",
    "judge_caliber",
    "judge_coverage",
    "judge_flow_caliber",
    "judge_lookahead",
    "judge_market_cap",
    "judge_null_rates",
    "judge_orphan",
    "judge_periods",
]

AuditState = Literal["OK", "WARN", "FAIL"]

_STATE_RANK: dict[AuditState, int] = {"OK": 0, "WARN": 1, "FAIL": 2}

_FIN_CRITICAL_NULL_COLS: tuple[str, ...] = (
    "revenue",
    "net_profit",
    "total_assets",
    "total_equity",
    "bps",
)
# roa：QMT 无 ROA 字段（适配器留空，恒 NULL），属已知设计而非数据缺陷，故不纳入非空率检查
_FIN_SPARSE_NULL_COLS: tuple[str, ...] = ("roe", "fcf")
_VAL_CRITICAL_NULL_COLS: tuple[str, ...] = ("pe_ttm", "pb")
# ps_ttm/market_cap 依赖股本快照、dv_ratio 依赖分红、pe_percentile 依赖历史长度 → 稀疏列
_VAL_SPARSE_NULL_COLS: tuple[str, ...] = (
    "ps_ttm",
    "market_cap",
    "dv_ratio",
    "pe_percentile",
)

# 抽样对账每 code 取最近一期的列
_SAMPLE_FIN_COLS: tuple[str, ...] = (
    "report_period",
    "ann_date",
    "revenue",
    "net_profit",
    "roe",
    "fcf",
    "bps",
)
_SAMPLE_VAL_COLS: tuple[str, ...] = (
    "trade_date",
    "pe_ttm",
    "pb",
    "ps_ttm",
    "dv_ratio",
    "market_cap",
    "pe_percentile",
)


# ---------------------------------------------------------------------------
# 结果结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AuditFinding:
    """单条体检项结论。``section`` 为区块名（financial/valuation/reconciliation）。"""

    section: str
    check: str
    state: AuditState
    message: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AuditSample:
    """随机抽样对账行：某 code 的最近一期财务 + 最近一期估值，供人工核对。"""

    code: str
    financial: dict[str, Any]
    valuation: dict[str, Any]


@dataclass(slots=True)
class AuditReport:
    """体检报告：findings 为全部体检项，samples 为抽样对账行。"""

    findings: list[AuditFinding]
    samples: list[AuditSample]

    def _count(self, state: AuditState) -> int:
        return sum(1 for f in self.findings if f.state == state)

    @property
    def ok_count(self) -> int:
        return self._count("OK")

    @property
    def warn_count(self) -> int:
        return self._count("WARN")

    @property
    def fail_count(self) -> int:
        return self._count("FAIL")

    @property
    def worst_state(self) -> AuditState:
        if not self.findings:
            return "WARN"
        return max((f.state for f in self.findings), key=lambda s: _STATE_RANK[s])

    def by_section(self) -> dict[str, list[AuditFinding]]:
        grouped: dict[str, list[AuditFinding]] = defaultdict(list)
        for finding in self.findings:
            grouped[finding.section].append(finding)
        return dict(grouped)

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": {
                "ok": self.ok_count,
                "warn": self.warn_count,
                "fail": self.fail_count,
                "worst": self.worst_state,
            },
            "findings": [
                {
                    "section": f.section,
                    "check": f.check,
                    "state": f.state,
                    "message": f.message,
                    "detail": f.detail,
                }
                for f in self.findings
            ],
            "samples": [
                {
                    "code": s.code,
                    "financial": {k: _jsonable(v) for k, v in s.financial.items()},
                    "valuation": {k: _jsonable(v) for k, v in s.valuation.items()},
                }
                for s in self.samples
            ],
        }


# ---------------------------------------------------------------------------
# 纯判定函数（阈值 → 三态；全部可单测，零 DB 依赖）
# ---------------------------------------------------------------------------


def judge_coverage(covered: int, universe: int, *, min_ratio: float) -> tuple[AuditState, str]:
    """标的覆盖：covered / universe ≥ min_ratio 判 OK，否则 FAIL；universe 空判 WARN。"""
    if universe <= 0:
        return "WARN", "universe 为空（dim_stock 无 SH/SZ/BJ 标的），无法判定覆盖率"
    ratio = covered / universe
    if ratio >= min_ratio:
        return "OK", f"覆盖率 {ratio:.1%}（{covered}/{universe} ≥ {min_ratio:.0%}）"
    return "FAIL", f"覆盖率 {ratio:.1%}（{covered}/{universe} < {min_ratio:.0%}）"


def judge_periods(periods: Sequence[date], *, min_quarters: int) -> tuple[AuditState, str]:
    """期数分布：报告期须落在 3/6/9/12 月；唯一期数 < min_quarters 判 WARN（历史不足）。"""
    if not periods:
        return "WARN", "无报告期数据"
    unique = sorted(set(periods))
    off_calendar = [p for p in unique if p.month not in QUARTER_MONTHS]
    if off_calendar:
        shown = "，".join(p.isoformat() for p in off_calendar[:5])
        return "FAIL", f"存在非季末报告期 {len(off_calendar)} 个（如 {shown}）"
    if len(unique) < min_quarters:
        return "WARN", f"报告期种类仅 {len(unique)} 期（< {min_quarters}），历史长度不足以判定"
    return "OK", f"报告期 {len(unique)} 期（{unique[0].isoformat()} ~ {unique[-1].isoformat()}）"


def judge_null_rates(
    rates: Mapping[str, float], *, max_rate: float, label: str
) -> tuple[AuditState, str]:
    """非空率：任一列空值率 > max_rate 判 FAIL；无数据判 WARN。"""
    if not rates:
        return "WARN", f"{label} 无数据，无法判定非空率"
    offenders = {c: r for c, r in rates.items() if r > max_rate}
    if offenders:
        detail = "，".join(f"{c} {r:.1%}" for c, r in sorted(offenders.items()))
        return "FAIL", f"{label} 空值率超阈值 {max_rate:.0%}：{detail}"
    worst = max(rates.values())
    return "OK", f"{label} 空值率最高 {worst:.1%} ≤ {max_rate:.0%}"


def judge_lookahead(count: int, *, max_rows: int) -> tuple[AuditState, str]:
    """前视计数：ann_date < report_period 行数 > max_rows 判 FAIL。"""
    if count <= max_rows:
        return "OK", f"前视行数 {count} ≤ 上限 {max_rows}"
    return "FAIL", f"前视行数 {count} > 上限 {max_rows}（ann_date < report_period 硬约束违反）"


def judge_ann_lag(
    median_days: float | None,
    late_ratio: float | None,
    *,
    min_median: float,
    max_median: float,
    late_threshold_days: int,
    max_late_ratio: float,
) -> tuple[AuditState, str]:
    """公告日滞后分布：中位数须落在 [min_median, max_median]，超期行占比 ≤ max_late_ratio。

    口径：fact_financial 快照的 ann_date = 该报告期首次披露日（版本表取 min），
    重述版本不计入，故该指标反映「首次披露的及时性」。构建于多版本表之上的旧实现
    曾把重述日当公告日，2010-2019 中位滞后约 390 天。
    """
    if median_days is None:
        return "WARN", "无有效财报行（ann_date 非空），无法评估公告日滞后分布"
    detail = (
        f"公告日滞后中位 {median_days:.0f} 天，滞后 > {late_threshold_days} 天占比 "
        f"{(late_ratio or 0.0):.1%}"
    )
    if not (min_median <= median_days <= max_median):
        return (
            "FAIL",
            f"{detail}（中位超出 [{min_median:.0f}, {max_median:.0f}] 天，疑似公告日口径错误）",
        )
    if late_ratio is not None and late_ratio > max_late_ratio:
        return (
            "WARN",
            f"{detail}（超期占比 > {max_late_ratio:.0%}，存在大量滞后披露/重述版本）",
        )
    return "OK", detail


def judge_caliber(
    comparable: int, violations: int, *, max_violation_ratio: float
) -> tuple[AuditState, str]:
    """口径一致性：偏差行占比 ≤ max_violation_ratio 判 OK；无可用样本判 WARN。"""
    if comparable <= 0:
        return "WARN", "无可用样本判定口径一致性（需同年累计可比对行）"
    ratio = violations / comparable
    if ratio <= max_violation_ratio:
        return (
            "OK",
            f"口径偏差 {ratio:.1%}（{violations}/{comparable} ≤ {max_violation_ratio:.0%}）",
        )
    return (
        "FAIL",
        f"口径偏差 {ratio:.1%}（{violations}/{comparable} > {max_violation_ratio:.0%}）",
    )


def judge_orphan(orphan: int, total: int, *, max_ratio: float) -> tuple[AuditState, str]:
    """勾稽匹配：估值行在行情表无对应的占比 ≤ max_ratio 判 OK；表空判 WARN。"""
    if total <= 0:
        return "WARN", "fact_valuation 为空，无法判定勾稽匹配"
    ratio = orphan / total
    if ratio <= max_ratio:
        return "OK", f"孤立估值行 {orphan}/{total}（{ratio:.2%} ≤ {max_ratio:.0%}）"
    return (
        "FAIL",
        f"孤立估值行 {orphan}/{total}（{ratio:.2%} > {max_ratio:.0%}）：估值行在行情表无对应",
    )


def judge_market_cap(devs: Sequence[float], *, rel_tol: float) -> tuple[AuditState, str]:
    """市值勾稽：|market_cap - close x 总股本| / 基准 ≤ rel_tol 判 OK；无样本判 WARN。"""
    if not devs:
        return "WARN", "无样本判定市值勾稽（缺股本或行情对齐）"
    worst = max(devs)
    if worst <= rel_tol:
        return "OK", f"市值偏差最大 {worst:.2%} ≤ {rel_tol:.0%}（样本 {len(devs)}）"
    return (
        "FAIL",
        f"市值偏差最大 {worst:.2%} > {rel_tol:.0%}（样本 {len(devs)}）"
        "：market_cap 与 close x 总股本 不符",
    )


def judge_flow_caliber(
    years: int, monotonic: int, *, max_ratio: float, min_years: int
) -> tuple[AuditState, str]:
    """流量口径漂移检测：单季口径下营收严格递增的 code-year 占比应远低于 max_ratio。

    单季口径（正例，实测约 23%）营收逐季严格递增属偶然；若占比逼近 100%，说明落库
    疑似把「年初至今累计」当成单季（口径漂移）。样本不足 min_years 判 WARN。
    """
    if years < min_years:
        return "WARN", f"完整四季度 code-year 仅 {years} 个（< {min_years}），不足以判定流量口径"
    ratio = monotonic / years
    if ratio > max_ratio:
        return (
            "FAIL",
            f"营收单季严格递增占比 {ratio:.1%}（{monotonic}/{years} > {max_ratio:.0%}）"
            "：疑似累计口径落库",
        )
    return "OK", f"营收单季严格递增占比 {ratio:.1%}（{monotonic}/{years} ≤ {max_ratio:.0%}）"


def caliber_balance_violations(
    records: Sequence[tuple[str, date, float | None, float | None, float | None]],
    *,
    rel_tol: float,
) -> tuple[int, int]:
    """口径一致性（资产负债表恒等式）：返回 ``(comparable, violations)``。

    records 每行 = (code, report_period, total_assets, total_equity, debt_ratio)。

    口径契约（见 ``data/financials.py``）：``debt_ratio`` 为 YTD 原值、``total_assets`` /
    ``total_equity`` 为期末时点值，三者须满足恒等式 ``debt_ratio ≈ (总资产 - 总权益) / 总资产``。

    注：**不**交叉验证 ``net_margin`` / ``roe`` —— QMT ``PershareIndex.inc_net_profit_rate``
    与 ``equity_roe`` 实测不满足「YTD 净利/营收」「YTD 净利/权益」关系（违规率 > 45%），
    属字段本身非该口径，非数据缺陷，故不作为勾稽依据。

    分母为 0 或值缺失的行跳过；相对偏差 > ``rel_tol`` 计违规。
    """
    comparable = 0
    violations = 0
    for _code, _period, ta, te, dr in records:
        if dr is None or ta is None or te is None or ta == 0:
            continue
        comparable += 1
        if abs((ta - te) / ta - dr) > rel_tol * max(abs(dr), 1e-9):
            violations += 1
    return comparable, violations


def flow_monotonic_count(
    records: Sequence[tuple[str, date, float | None]],
) -> tuple[int, int]:
    """流量口径抽查：返回 ``(完整四季度 code-year 数, 营收严格递增的 code-year 数)``。

    records 每行 = (code, report_period, revenue)。
    """
    grouped: dict[tuple[str, int], dict[int, float | None]] = defaultdict(dict)
    for code, period, rev in records:
        grouped[(code, period.year)][period.month] = rev

    years = 0
    monotonic = 0
    for months in grouped.values():
        if set(months) != set(QUARTER_MONTHS):
            continue
        seq = [months[m] for m in QUARTER_MONTHS]
        if any(v is None for v in seq):
            continue
        years += 1
        if all(seq[i] < seq[i + 1] for i in range(len(seq) - 1)):  # type: ignore[operator]
            monotonic += 1
    return years, monotonic


# ---------------------------------------------------------------------------
# 配置读取（Config | dict 双形态，兼容单测注入）
# ---------------------------------------------------------------------------


def _get(config: Config | dict[str, Any], dotted: str, default: Any) -> Any:
    if isinstance(config, Config):
        return config.get(dotted, default)
    node: Any = config
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def _threshold(config: Config | dict[str, Any], key: str, default: float) -> float:
    return float(_get(config, f"audit.{key}", default))


def _clean(value: Any) -> Any:
    """NaN / NaT → None，其余原样（供 JSON 序列化与打印）。"""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def _jsonable(value: Any) -> Any:
    """date → ISO 字符串，其余原样（JSON 序列化边界）。"""
    if isinstance(value, date):
        return value.isoformat()
    return value


# ---------------------------------------------------------------------------
# 装配：DuckDB 查询
# ---------------------------------------------------------------------------


def _scalar(conn: duckdb.DuckDBPyConnection, sql: str, params: list[Any] | None = None) -> int:
    row = conn.execute(sql, params or []).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _null_rates(
    conn: duckdb.DuckDBPyConnection, table: str, cols: Sequence[str]
) -> dict[str, float]:
    """各列空值率（NULL 或 NaN 均计为空）。"""
    total = _scalar(conn, f"SELECT count(*) FROM {table}")
    if total <= 0:
        return {}
    rates: dict[str, float] = {}
    for col in cols:
        nulls = _scalar(
            conn,
            f"SELECT count(*) FROM {table} WHERE {col} IS NULL OR isnan({col})",
        )
        rates[col] = nulls / total
    return rates


def _pick_sample_codes(conn: duckdb.DuckDBPyConnection, size: int, seed: int) -> list[str]:
    """确定性随机抽样：以 fact_financial 的 code 全量为池，random.Random(seed) 取样。"""
    rows = conn.execute("SELECT DISTINCT code FROM fact_financial ORDER BY code").fetchall()
    codes = [str(r[0]) for r in rows]
    if size <= 0:
        return []
    if len(codes) <= size:
        return codes
    return sorted(random.Random(seed).sample(codes, size))


def _ann_lag_stats(
    conn: duckdb.DuckDBPyConnection, *, late_days: int
) -> tuple[float | None, float | None, int]:
    """快照表公告日滞后：中位天数、滞后 > late_days 的行占比、样本行数。

    上市前报告期（report_period < 上市日）来自招股书披露，公告日天然远晚于报告期
    （实测滞后可达 1800 天），不属于披露及时性问题，故按上市日排除。
    上市日取 `dim_stock.list_date`，缺失时退化为该标的首个交易日（QMT 名单实测
    list_date 全空，首根日线即上市日代理）；两者皆缺失则保留该行。
    """
    base = (
        "FROM fact_financial f "
        "LEFT JOIN dim_stock s ON s.code = f.code "
        "LEFT JOIN (SELECT code, min(trade_date) AS first_bar FROM fact_daily_bar GROUP BY 1) b "
        "ON b.code = f.code "
        "WHERE f.ann_date IS NOT NULL "
        "AND (COALESCE(s.list_date, b.first_bar) IS NULL "
        "OR f.report_period >= COALESCE(s.list_date, b.first_bar))"
    )
    total = _scalar(conn, f"SELECT count(*) {base}")
    if total <= 0:
        return None, None, 0
    median_days = _scalar(conn, f"SELECT median(f.ann_date - f.report_period) {base}")
    late = _scalar(
        conn,
        f"SELECT count(*) {base} AND f.ann_date - f.report_period > {late_days}",
    )
    return (
        float(median_days) if median_days is not None else None,
        late / total,
        total,
    )


def _section_financial(
    conn: duckdb.DuckDBPyConnection,
    config: Config | dict[str, Any],
    sample_codes: Sequence[str],
) -> list[AuditFinding]:
    """fact_financial 区块：覆盖 / 期数 / 非空率 / 前视 / 口径一致性。"""
    findings: list[AuditFinding] = []

    universe = _scalar(conn, "SELECT count(*) FROM dim_stock WHERE market IN ('SH','SZ','BJ')")
    covered = _scalar(
        conn,
        "SELECT count(DISTINCT f.code) FROM fact_financial f "
        "JOIN dim_stock d ON d.code = f.code WHERE d.market IN ('SH','SZ','BJ')",
    )
    state, msg = judge_coverage(
        covered, universe, min_ratio=_threshold(config, "coverage_min_ratio", 0.95)
    )
    findings.append(
        AuditFinding(
            "financial", "coverage", state, msg, {"covered": covered, "universe": universe}
        )
    )

    period_rows = conn.execute(
        "SELECT report_period FROM fact_financial WHERE report_period IS NOT NULL"
    ).fetchall()
    periods = [r[0] for r in period_rows]
    state, msg = judge_periods(
        periods, min_quarters=int(_threshold(config, "period_min_quarters", 8))
    )
    findings.append(
        AuditFinding("financial", "periods", state, msg, {"distinct_periods": len(set(periods))})
    )

    crit = _null_rates(conn, "fact_financial", _FIN_CRITICAL_NULL_COLS)
    state, msg = judge_null_rates(
        crit, max_rate=_threshold(config, "null_rate_max", 0.30), label="核心列"
    )
    findings.append(AuditFinding("financial", "null_rate.core", state, msg, dict(crit)))

    sparse = _null_rates(conn, "fact_financial", _FIN_SPARSE_NULL_COLS)
    state, msg = judge_null_rates(
        sparse, max_rate=_threshold(config, "null_rate_max_sparse", 0.95), label="稀疏列"
    )
    findings.append(AuditFinding("financial", "null_rate.sparse", state, msg, dict(sparse)))

    lookahead = _scalar(
        conn,
        "SELECT count(*) FROM fact_financial_version WHERE ann_date < report_period",
    )
    null_ann = _scalar(conn, "SELECT count(*) FROM fact_financial_version WHERE ann_date IS NULL")
    state, msg = judge_lookahead(
        lookahead, max_rows=int(_threshold(config, "lookahead_max_rows", 0))
    )
    findings.append(
        AuditFinding(
            "financial",
            "lookahead",
            state,
            msg,
            {"lookahead_rows": lookahead, "null_ann": null_ann},
        )
    )

    late_days = int(_threshold(config, "ann_lag_days", 300))
    median_days, late_ratio, lag_rows = _ann_lag_stats(conn, late_days=late_days)
    state, msg = judge_ann_lag(
        median_days,
        late_ratio,
        min_median=float(_threshold(config, "ann_lag_median_min_days", 20)),
        max_median=float(_threshold(config, "ann_lag_median_max_days", 130)),
        late_threshold_days=late_days,
        max_late_ratio=float(_threshold(config, "ann_lag_late_ratio_max", 0.05)),
    )
    findings.append(
        AuditFinding(
            "financial",
            "ann_lag",
            state,
            msg,
            {
                "rows": lag_rows,
                "median_days": median_days,
                "late_ratio": late_ratio,
                "late_threshold_days": late_days,
            },
        )
    )

    records = _caliber_records(conn, sample_codes)
    comparable, violations = caliber_balance_violations(
        records, rel_tol=_threshold(config, "caliber_rel_tol", 0.05)
    )
    state, msg = judge_caliber(
        comparable,
        violations,
        max_violation_ratio=_threshold(config, "caliber_balance_max_violation_ratio", 0.02),
    )
    findings.append(
        AuditFinding(
            "financial",
            "caliber.balance",
            state,
            msg,
            {
                "comparable": comparable,
                "violations": violations,
                "sample_codes": len(sample_codes),
            },
        )
    )

    years, monotonic = _flow_monotonic(conn, sample_codes)
    state, msg = judge_flow_caliber(
        years,
        monotonic,
        max_ratio=_threshold(config, "caliber_monotonic_max_ratio", 0.90),
        min_years=int(_threshold(config, "caliber_monotonic_min_years", 50)),
    )
    findings.append(
        AuditFinding(
            "financial",
            "caliber.flow",
            state,
            msg,
            {"code_years": years, "monotonic": monotonic},
        )
    )
    return findings


def _caliber_records(
    conn: duckdb.DuckDBPyConnection, sample_codes: Sequence[str]
) -> list[tuple[str, date, float | None, float | None, float | None]]:
    """抽样 code 季末财务行 → (code, report_period, total_assets, total_equity, debt_ratio)。"""
    if not sample_codes:
        return []
    records: list[tuple[str, date, float | None, float | None, float | None]] = []
    for code in sample_codes:
        rows = conn.execute(
            "SELECT report_period, total_assets, total_equity, debt_ratio "
            "FROM fact_financial "
            "WHERE code = ? AND month(report_period) IN (3, 6, 9, 12) "
            "ORDER BY report_period",
            [code],
        ).fetchall()
        for period, ta, te, dr in rows:
            records.append((code, period, ta, te, dr))
    return records


def _flow_monotonic(
    conn: duckdb.DuckDBPyConnection, sample_codes: Sequence[str]
) -> tuple[int, int]:
    """抽样 code 季末营收 → (完整四季度 code-year 数, 营收严格递增的 code-year 数)。"""
    if not sample_codes:
        return 0, 0
    records: list[tuple[str, date, float | None]] = []
    for code in sample_codes:
        rows = conn.execute(
            "SELECT report_period, revenue FROM fact_financial "
            "WHERE code = ? AND month(report_period) IN (3, 6, 9, 12) "
            "ORDER BY report_period",
            [code],
        ).fetchall()
        for period, rev in rows:
            records.append((code, period, rev))
    return flow_monotonic_count(records)


def _section_valuation(
    conn: duckdb.DuckDBPyConnection, config: Config | dict[str, Any]
) -> list[AuditFinding]:
    """fact_valuation 区块：覆盖 / 非空率。"""
    findings: list[AuditFinding] = []

    universe = _scalar(conn, "SELECT count(*) FROM dim_stock WHERE market IN ('SH','SZ','BJ')")
    covered = _scalar(
        conn,
        "SELECT count(DISTINCT v.code) FROM fact_valuation v "
        "JOIN dim_stock d ON d.code = v.code WHERE d.market IN ('SH','SZ','BJ')",
    )
    state, msg = judge_coverage(
        covered, universe, min_ratio=_threshold(config, "coverage_min_ratio", 0.95)
    )
    findings.append(
        AuditFinding(
            "valuation", "coverage", state, msg, {"covered": covered, "universe": universe}
        )
    )

    crit = _null_rates(conn, "fact_valuation", _VAL_CRITICAL_NULL_COLS)
    state, msg = judge_null_rates(
        crit, max_rate=_threshold(config, "null_rate_max", 0.30), label="核心列"
    )
    findings.append(AuditFinding("valuation", "null_rate.core", state, msg, dict(crit)))

    sparse = _null_rates(conn, "fact_valuation", _VAL_SPARSE_NULL_COLS)
    state, msg = judge_null_rates(
        sparse, max_rate=_threshold(config, "null_rate_max_sparse", 0.95), label="稀疏列"
    )
    findings.append(AuditFinding("valuation", "null_rate.sparse", state, msg, dict(sparse)))
    return findings


def _section_reconciliation(
    conn: duckdb.DuckDBPyConnection,
    config: Config | dict[str, Any],
    sample_codes: Sequence[str],
) -> list[AuditFinding]:
    """勾稽区块：估值行↔行情行匹配 + 市值口径勾稽。"""
    findings: list[AuditFinding] = []

    total = _scalar(conn, "SELECT count(*) FROM fact_valuation")
    orphan = _scalar(
        conn,
        "SELECT count(*) FROM fact_valuation v "
        "LEFT JOIN fact_daily_bar b ON b.code = v.code AND b.trade_date = v.trade_date "
        "WHERE b.code IS NULL",
    )
    state, msg = judge_orphan(
        orphan, total, max_ratio=_threshold(config, "valuation_orphan_max_ratio", 0.02)
    )
    findings.append(
        AuditFinding(
            "reconciliation", "valuation_bar_match", state, msg, {"orphan": orphan, "total": total}
        )
    )

    devs = _market_cap_deviations(conn, sample_codes)
    state, msg = judge_market_cap(devs, rel_tol=_threshold(config, "market_cap_rel_tol", 0.05))
    findings.append(
        AuditFinding(
            "reconciliation",
            "market_cap",
            state,
            msg,
            {"samples": len(devs), "max_dev": max(devs) if devs else None},
        )
    )
    return findings


def _market_cap_deviations(
    conn: duckdb.DuckDBPyConnection, sample_codes: Sequence[str]
) -> list[float]:
    """抽样 code 最近一期估值行：|market_cap - close x as-of 总股本| / 基准。"""
    devs: list[float] = []
    for code in sample_codes:
        row = conn.execute(
            "SELECT trade_date, market_cap FROM fact_valuation "
            "WHERE code = ? AND market_cap IS NOT NULL AND NOT isnan(market_cap) "
            "ORDER BY trade_date DESC LIMIT 1",
            [code],
        ).fetchone()
        if row is None:
            continue
        trade_date, market_cap = row
        close_row = conn.execute(
            "SELECT close FROM fact_daily_bar WHERE code = ? AND trade_date = ?",
            [code, trade_date],
        ).fetchone()
        shares_row = conn.execute(
            "SELECT total_shares FROM fact_share_structure "
            "WHERE code = ? AND change_date <= ? AND total_shares IS NOT NULL "
            "ORDER BY change_date DESC LIMIT 1",
            [code, trade_date],
        ).fetchone()
        if close_row is None or shares_row is None:
            continue
        close, shares = close_row[0], shares_row[0]
        if close in (None, 0) or shares in (None, 0):
            continue
        expected = float(close) * float(shares)
        if expected == 0:
            continue
        devs.append(abs(float(market_cap) - expected) / abs(expected))
    return devs


def _collect_samples(
    conn: duckdb.DuckDBPyConnection, sample_codes: Sequence[str]
) -> list[AuditSample]:
    """抽样对账行：逐 code 取最近一期财务 + 最近一期估值。"""
    samples: list[AuditSample] = []
    for code in sample_codes:
        fin_row = conn.execute(
            "SELECT " + ", ".join(_SAMPLE_FIN_COLS) + " FROM fact_financial "
            "WHERE code = ? ORDER BY report_period DESC LIMIT 1",
            [code],
        ).fetchone()
        val_row = conn.execute(
            "SELECT " + ", ".join(_SAMPLE_VAL_COLS) + " FROM fact_valuation "
            "WHERE code = ? ORDER BY trade_date DESC LIMIT 1",
            [code],
        ).fetchone()
        samples.append(
            AuditSample(
                code=code,
                financial=(
                    {k: _clean(v) for k, v in zip(_SAMPLE_FIN_COLS, fin_row, strict=False)}
                    if fin_row
                    else {}
                ),
                valuation=(
                    {k: _clean(v) for k, v in zip(_SAMPLE_VAL_COLS, val_row, strict=False)}
                    if val_row
                    else {}
                ),
            )
        )
    return samples


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def audit_database(
    conn: duckdb.DuckDBPyConnection,
    config: Config | dict[str, Any],
    *,
    sample_size: int | None = None,
    seed: int | None = None,
) -> AuditReport:
    """全库体检入口（只读，空库安全不抛异常）。

    参数:
        conn: 已打开的 DuckDB 连接（CLI 侧以 read_only=True 打开）。
        config: Config 或等同 dict，读 ``audit.*`` 阈值。
        sample_size: 抽样条数覆盖（None 取配置 ``audit.sample_size``）。
        seed: 抽样随机种子覆盖（None 取配置 ``audit.sample_seed``）。

    返回:
        AuditReport；任一区块执行期异常收敛为 FAIL finding，不向上抛出。
    """
    size = sample_size if sample_size is not None else int(_threshold(config, "sample_size", 10))
    rng_seed = seed if seed is not None else int(_threshold(config, "sample_seed", 42))

    findings: list[AuditFinding] = []

    try:
        sample_codes = _pick_sample_codes(conn, size, rng_seed)
    except Exception as exc:
        return AuditReport(
            findings=[
                AuditFinding(
                    "financial",
                    "__sample__",
                    "FAIL",
                    f"抽样取池失败：{exc!r}",
                )
            ],
            samples=[],
        )

    sections: list[tuple[str, Any]] = [
        ("financial", lambda: _section_financial(conn, config, sample_codes)),
        ("valuation", lambda: _section_valuation(conn, config)),
        ("reconciliation", lambda: _section_reconciliation(conn, config, sample_codes)),
    ]
    for name, runner in sections:
        try:
            findings.extend(runner())
        except Exception as exc:
            findings.append(AuditFinding(name, "__error__", "FAIL", f"检查异常：{exc!r}"))

    samples: list[AuditSample] = []
    try:
        samples = _collect_samples(conn, sample_codes)
    except Exception:
        samples = []

    return AuditReport(findings=findings, samples=samples)
