"""复盘报告页服务层（P7 v1）：Brinson 归因定量层 + LLM 定性解读。

纪律：
- 不调 LLM，不 subprocess 调 review_portfolio.py
- 归因复用 attribution.load_brinson_input + compute_brinson（纯定量）
- 持仓/成交走直接 SQL
- LLM 定性解读：纯 SQL 读 agent_output.output_json，不 import agents 模块
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Any

import duckdb

from viqtrader.attribution.brinson import BrinsonResult, NewPositionItem, SegmentAttribution
from viqtrader.attribution.loader import BrinsonInputBundle, load_brinson_input, replay_holdings

# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SummaryCard:
    """总览卡片数据。"""

    account_id: str
    start: str
    end: str
    benchmark: str
    start_mv_cny: float  # 期初市值（含现金）
    end_mv_cny: float  # 期末市值（含现金）
    pnl_cny: float  # 区间绝对收益
    portfolio_return: float  # 组合区间收益（含现金段）
    benchmark_return: float | None  # 自算市值加权基准收益
    active_return: float | None  # Brinson 主动收益 = rp - rb
    index_return: float | None  # 沪深300区间收益
    index_excess: float | None  # rp - index_return


@dataclass(frozen=True, slots=True)
class BrinsonDisplay:
    """Brinson 归因展示层（聚合 + 段明细）。"""

    total_allocation: float
    total_selection: float
    total_interaction: float
    active_return: float
    segments: tuple[SegmentAttribution, ...]
    new_positions: tuple[NewPositionItem, ...]
    unavailable: bool = False
    unavailable_reason: str = ""


@dataclass(frozen=True, slots=True)
class AttributionCommentary:
    """LLM 归因定性解读（来自 agent_output.output_json）。"""

    summary: str
    allocation_comment: str
    selection_comment: str
    key_findings: tuple[str, ...]
    generated_at: str  # agent_run.finished_at


@dataclass(frozen=True, slots=True)
class PositionDiffItem:
    """期初 vs 期末持仓变动。"""

    code: str
    name: str
    industry: str
    start_qty: int
    end_qty: int
    status: str  # holding / opened / closed / reduced / added


@dataclass(frozen=True, slots=True)
class TradeSummaryRow:
    """区间成交聚合（按 code 分组）。"""

    code: str
    name: str
    buy_qty: int
    sell_qty: int
    buy_amount: float
    sell_amount: float
    fees: float


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def list_accounts(conn: duckdb.DuckDBPyConnection) -> list[str]:
    """列出所有子账本账户 ID。"""
    rows = conn.execute("SELECT account_id FROM account ORDER BY account_id").fetchall()
    return [str(r[0]) for r in rows]


def build_review(
    conn: duckdb.DuckDBPyConnection,
    account_id: str,
    start: date,
    end: date,
    *,
    benchmark_code: str = "000300",
) -> dict[str, Any]:
    """装配复盘报告全部区块。

    Returns:
        dict 含 summary / brinson / position_diff / trade_summary / notes。
    """
    # ---- Brinson 归因 ----
    bundle = load_brinson_input(conn, account_id, start, end, benchmark_code=benchmark_code)
    brinson_disp: BrinsonDisplay
    summary: SummaryCard
    notes: list[str] = list(bundle.notes) if bundle else []

    if bundle is None:
        brinson_disp = BrinsonDisplay(
            total_allocation=0.0,
            total_selection=0.0,
            total_interaction=0.0,
            active_return=0.0,
            segments=(),
            new_positions=(),
            unavailable=True,
            unavailable_reason="账本为空、期初总资产 ≤ 0 或基准 universe 缺市值/行情数据",
        )
        summary = SummaryCard(
            account_id=account_id,
            start=start.isoformat(),
            end=end.isoformat(),
            benchmark=benchmark_code,
            start_mv_cny=0.0,
            end_mv_cny=0.0,
            pnl_cny=0.0,
            portfolio_return=0.0,
            benchmark_return=None,
            active_return=None,
            index_return=None,
            index_excess=None,
        )
    else:
        result = _compute_safe(bundle)
        portfolio_return = result.portfolio_return if result else 0.0

        # 期初/期末市值（含现金）
        start_mv = _total_assets_start(conn, account_id, start)
        end_mv = _total_assets_end(conn, account_id, end)

        summary = SummaryCard(
            account_id=account_id,
            start=start.isoformat(),
            end=end.isoformat(),
            benchmark=benchmark_code,
            start_mv_cny=start_mv,
            end_mv_cny=end_mv,
            pnl_cny=end_mv - start_mv,
            portfolio_return=portfolio_return,
            benchmark_return=result.benchmark_return if result else None,
            active_return=result.active_return if result else None,
            index_return=result.index_return if result else None,
            index_excess=result.index_excess if result else None,
        )

        if result is None:
            brinson_disp = BrinsonDisplay(
                total_allocation=0.0,
                total_selection=0.0,
                total_interaction=0.0,
                active_return=0.0,
                segments=(),
                new_positions=bundle.new_positions,
                unavailable=True,
                unavailable_reason="期初无持仓行业段（纯现金组合无可归因对象）",
            )
        else:
            brinson_disp = BrinsonDisplay(
                total_allocation=result.total_allocation,
                total_selection=result.total_selection,
                total_interaction=result.total_interaction,
                active_return=result.active_return,
                segments=result.segments,
                new_positions=bundle.new_positions,
            )

        if bundle.dropped_codes:
            notes.append(f"期初无行情持仓剔除 {len(bundle.dropped_codes)} 只")

    # ---- 持仓变动 ----
    position_diff = _position_diff(conn, account_id, start)

    # ---- 成交聚合 ----
    trade_summary = _trade_summary(conn, account_id, start, end)

    # ---- LLM 归因定性解读（纯 SQL 读 agent_output，无记录则 None）----
    llm_commentary = _load_llm_attribution(conn, account_id, start, end)

    return {
        "summary": summary,
        "brinson": brinson_disp,
        "position_diff": position_diff,
        "trade_summary": trade_summary,
        "attribution_commentary": llm_commentary,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _compute_safe(bundle: BrinsonInputBundle) -> BrinsonResult | None:
    """包装 compute_brinson：权重边界异常（如期初无持仓）安全降级。"""
    from viqtrader.attribution.brinson import compute_brinson

    try:
        return compute_brinson(
            account_id=bundle.account_id,
            start=bundle.start,
            end=bundle.end,
            segments=bundle.segments,
            cash_weight=bundle.cash_weight,
            index_return=bundle.index_return,
            new_positions=bundle.new_positions,
        )
    except ValueError:
        # 权重校验失败（极端边界），页面展示 unavailable 而非崩溃
        return None


def _total_assets_start(conn: duckdb.DuckDBPyConnection, account_id: str, start: date) -> float:
    """期初总资产 = 现金 + 持仓市值（start as-of）。"""
    quota_row = conn.execute(
        "SELECT capital_quota FROM account WHERE account_id = ?", [account_id]
    ).fetchone()
    quota = float(quota_row[0]) if quota_row else 0.0
    cash_row = conn.execute(
        """
        SELECT COALESCE(SUM(amount_cny), 0) FROM cash_ledger
        WHERE account_id = ? AND trade_date < ?
        """,
        [account_id, start],
    ).fetchone()
    cash_start = quota + (float(cash_row[0]) if cash_row else 0.0)

    holdings = replay_holdings(conn, account_id, start)
    if not holdings:
        return cash_start

    # 与 loader._adj_close / _fx_rate 同口径
    fx_cache: dict[date, float] = {}

    def _adj(code: str, as_of: date) -> float | None:
        row = conn.execute(
            """
            SELECT close * COALESCE(adj_factor, 1.0) FROM fact_daily_bar
            WHERE code = ? AND trade_date <= ? AND close IS NOT NULL AND close > 0
            ORDER BY trade_date DESC LIMIT 1
            """,
            [code, as_of],
        ).fetchone()
        if not row:
            return None
        v = float(row[0])
        return v if v == v else None

    def _fx(as_of: date) -> float:
        if len(holdings) == 0:
            return 1.0
        # 有港股持仓才查汇率（简单判定：5 位代码）
        has_hk = any(len(c) == 5 for c in holdings)
        if not has_hk:
            return 1.0
        if as_of in fx_cache:
            return fx_cache[as_of]
        row = conn.execute(
            """
            SELECT rate FROM fact_fx_rate
            WHERE pair = 'HKD/CNY' AND trade_date <= ?
            ORDER BY trade_date DESC LIMIT 1
            """,
            [as_of],
        ).fetchone()
        rate = float(row[0]) if row else 1.0
        fx_cache[as_of] = rate
        return rate

    mv = 0.0
    for code, qty in holdings.items():
        adj = _adj(code, start)
        if adj is None:
            continue
        fx = _fx(start) if len(code) == 5 else 1.0
        mv += qty * adj * fx
    return cash_start + mv


def _total_assets_end(conn: duckdb.DuckDBPyConnection, account_id: str, end: date) -> float:
    """期末总资产 = 现金（cash_ledger 全量）+ 持仓市值（end as-of close）。"""
    quota_row = conn.execute(
        "SELECT capital_quota FROM account WHERE account_id = ?", [account_id]
    ).fetchone()
    quota = float(quota_row[0]) if quota_row else 0.0
    cash_row = conn.execute(
        "SELECT COALESCE(SUM(amount_cny), 0) FROM cash_ledger WHERE account_id = ?",
        [account_id],
    ).fetchone()
    cash_end = quota + (float(cash_row[0]) if cash_row else 0.0)

    # 期末持仓直接取 position 表
    rows = conn.execute(
        """
        SELECT p.code, p.qty,
               bar.close * COALESCE(bar.adj_factor, 1.0) AS adj_close
        FROM position p
        LEFT JOIN (
            SELECT code, close, adj_factor,
                   ROW_NUMBER() OVER (PARTITION BY code ORDER BY trade_date DESC) rn
            FROM fact_daily_bar WHERE trade_date <= ? AND close IS NOT NULL AND close > 0
        ) bar ON bar.code = p.code AND bar.rn = 1
        WHERE p.account_id = ?
        """,
        [end, account_id],
    ).fetchall()

    mv = 0.0
    for code, qty, adj_close in rows:
        if adj_close is None:
            continue
        fx = 1.0
        if len(str(code)) == 5:
            fx_row = conn.execute(
                """
                SELECT rate FROM fact_fx_rate
                WHERE pair = 'HKD/CNY' AND trade_date <= ?
                ORDER BY trade_date DESC LIMIT 1
                """,
                [end],
            ).fetchone()
            fx = float(fx_row[0]) if fx_row else 1.0
        mv += int(qty) * float(adj_close) * fx
    return cash_end + mv


def _position_diff(
    conn: duckdb.DuckDBPyConnection, account_id: str, start: date
) -> list[PositionDiffItem]:
    """期初持仓（replay_holdings）vs 期末持仓（position 表）diff。"""
    start_holdings = replay_holdings(conn, account_id, start)
    end_rows = conn.execute(
        "SELECT code, qty FROM position WHERE account_id = ?", [account_id]
    ).fetchall()
    end_holdings: dict[str, int] = {str(r[0]): int(r[1]) for r in end_rows}

    all_codes = sorted(set(start_holdings) | set(end_holdings))
    diffs: list[PositionDiffItem] = []

    names = _stock_info_map(conn, all_codes)

    for code in all_codes:
        sq = start_holdings.get(code, 0)
        eq = end_holdings.get(code, 0)
        if sq == 0 and eq > 0:
            status = "opened"
        elif sq > 0 and eq == 0:
            status = "closed"
        elif sq > 0 and eq > sq:
            status = "added"
        elif sq > 0 and eq < sq:
            status = "reduced"
        else:
            status = "holding"

        info = names.get(code, ("", ""))
        diffs.append(
            PositionDiffItem(
                code=code,
                name=info[0],
                industry=info[1],
                start_qty=sq,
                end_qty=eq,
                status=status,
            )
        )
    return diffs


def _trade_summary(
    conn: duckdb.DuckDBPyConnection, account_id: str, start: date, end: date
) -> list[TradeSummaryRow]:
    """区间成交按 code 分组聚合。"""
    rows = conn.execute(
        """
        SELECT t.code, COALESCE(d.name, '') AS name,
               sum(CASE WHEN t.side = 'BUY' THEN t.qty ELSE 0 END) AS buy_qty,
               sum(CASE WHEN t.side = 'SELL' THEN t.qty ELSE 0 END) AS sell_qty,
               sum(CASE WHEN t.side = 'BUY' THEN t.amount_cny ELSE 0 END) AS buy_amt,
               sum(CASE WHEN t.side = 'SELL' THEN t.amount_cny ELSE 0 END) AS sell_amt,
               sum(t.commission + t.stamp_tax + t.transfer_fee) AS fees
        FROM trade_log t
        LEFT JOIN dim_stock d ON d.code = t.code
        WHERE t.account_id = ? AND t.trade_date BETWEEN ? AND ?
        GROUP BY t.code, d.name
        ORDER BY (sum(CASE WHEN t.side = 'BUY' THEN t.amount_cny ELSE 0 END)
               + sum(CASE WHEN t.side = 'SELL' THEN t.amount_cny ELSE 0 END)) DESC
        """,
        [account_id, start, end],
    ).fetchall()
    return [
        TradeSummaryRow(
            code=str(r[0]),
            name=str(r[1]),
            buy_qty=int(r[2] or 0),
            sell_qty=int(r[3] or 0),
            buy_amount=float(r[4] or 0),
            sell_amount=float(r[5] or 0),
            fees=float(r[6] or 0),
        )
        for r in rows
    ]


def _stock_info_map(
    conn: duckdb.DuckDBPyConnection, codes: list[str]
) -> dict[str, tuple[str, str]]:
    """批量取 code → (name, industry) 映射。"""
    if not codes:
        return {}
    rows = conn.execute(
        f"""
        SELECT code, COALESCE(name, ''), COALESCE(industry, '')
        FROM dim_stock WHERE code IN ({",".join(["?" for _ in codes])})
        """,
        codes,
    ).fetchall()
    return {str(r[0]): (str(r[1]), str(r[2])) for r in rows}


def _load_llm_attribution(
    conn: duckdb.DuckDBPyConnection, account_id: str, start: date, end: date
) -> AttributionCommentary | None:
    """纯 SQL 读 agent_output：取最近一次 SUCCESS 的 attribution_review 输出。

    匹配键：agent_name='attribution_review' + input_snapshot 中的 account_id/start/end。
    返回 None 表示没有 LLM 解读（graceful degrade，不崩溃）。
    """
    try:
        row = conn.execute(
            """
            SELECT ao.output_json, CAST(ar.finished_at AS VARCHAR)
            FROM agent_output ao
            JOIN agent_run ar ON ao.run_id = ar.run_id
            WHERE ar.agent_name = 'attribution_review'
              AND ar.status = 'SUCCESS'
              AND json_extract_string(ar.input_snapshot, '$.account_id') = ?
              AND CAST(json_extract_string(ar.input_snapshot, '$.start') AS DATE) = ?
              AND CAST(json_extract_string(ar.input_snapshot, '$.end') AS DATE) = ?
            ORDER BY ar.finished_at DESC
            LIMIT 1
            """,
            [account_id, start, end],
        ).fetchone()
    except duckdb.Error:
        # 库内 agent_run 表不存在或缺字段（空库），安全降级
        return None

    if row is None:
        return None

    try:
        payload = json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return None

    # 契约已由 AttributionReviewContract 校验过，但 Web 层做最小字段校验
    summary = payload.get("summary", "")
    allocation = payload.get("allocation_comment", "")
    selection = payload.get("selection_comment", "")
    findings = payload.get("key_findings", [])

    if (
        not isinstance(summary, str)
        or not isinstance(allocation, str)
        or not isinstance(selection, str)
    ):
        return None
    if not isinstance(findings, list):
        return None

    return AttributionCommentary(
        summary=summary,
        allocation_comment=allocation,
        selection_comment=selection,
        key_findings=tuple(str(f) for f in findings),
        generated_at=str(row[1] or ""),
    )
