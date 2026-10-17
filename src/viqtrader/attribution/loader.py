"""P4.6 Brinson 归因装配层（账本 + 行情 + 维度 → compute_brinson 输入）。

职责边界：本模块只做数据装配（IO），归因数学见 brinson.py（纯函数）；
装配结果由 pipeline 交给 compute_brinson，本模块不产出归因结论。

口径（2026-10-17 定稿）：
- 归因对象：期初持仓（首笔 BUY < start）+ 期初现金；区间新建仓
  （首笔 BUY ∈ [start, end]）不参与分解，new_positions 单列；
  同一标的清仓后区间内重新买入，v1 仍按首笔 BUY 判定（重写口径待迭代）。
- 期初持仓：重放 trade_log（trade_date < start，BUY +qty / SELL -qty）；
  期初现金 = capital_quota + Σ cash_ledger.amount_cny（trade_date < start），
  与引擎 _current_cash 同口径加分日过滤。
- 价格口径：复权收盘 = close x COALESCE(adj_factor, 1)，as-of 取 ≤ 目标日
  最近交易日（backward 对齐，与估值前视防控同原则）；个股区间收益
  = adj_close(end) / adj_close(start) - 1；期末缺行情退化为最近可得以致收益偏 0。
- 组合段：行业 = dim_stock.industry，NULL/空串 → 「未分类」；段收益按
  期初市值（qty x adj_close_start x fx）加权；现金段 rp=0。
- 基准 universe：dim_stock 中 market IN ('SH','SZ','BJ') 且 as-of start
  market_cap > 0（非 NaN）且有行情的 A 股，按行业市值加权；缺失标的剔除
  计数入 notes。港股通标的 v1 不入基准 universe（汇率口径复杂、占比低）；
  组合持有的基准外行业段 wb=0 且 rb_i 约定 = rp_i（selection/interaction=0，
  效应全入 allocation），未持有行业段 rp_i 约定 = rb_i（brinson.py 已注明）。
- 剔除透明：期初无行情持仓剔除进 dropped_codes，剩余权重按调整后总资产归一。
- 指数口径超额：fact_index_bar 指定指数（默认 000300）as-of 区间收益；
  表空/缺数据 → None。
- 返回 None：账户无成交无持仓（真空账本）、期初总资产 ≤ 0、
  基准 universe 为空（当前真实库 market_cap 全缺即属此类，待数据源补齐），
  原因记日志。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

import duckdb

from viqtrader.attribution.brinson import NewPositionItem, SegmentInput

logger = logging.getLogger(__name__)

UNKNOWN_INDUSTRY = "未分类"
_A_SHARE_MARKETS = ("SH", "SZ", "BJ")


@dataclass(frozen=True, slots=True)
class BrinsonInputBundle:
    """装配结果：segments/cash_weight 可直接喂 compute_brinson。"""

    account_id: str
    start: date
    end: date
    segments: tuple[SegmentInput, ...]
    cash_weight: float
    index_return: float | None
    new_positions: tuple[NewPositionItem, ...]
    dropped_codes: tuple[str, ...]  # 期初无行情被剔除的持仓
    notes: tuple[str, ...] = ()  # 数据质量事项（透明度）


def load_brinson_input(
    conn: duckdb.DuckDBPyConnection,
    account_id: str,
    start: date,
    end: date,
    *,
    benchmark_code: str = "000300",
) -> BrinsonInputBundle | None:
    """装配 Brinson 归因输入（只读，不写库）。

    Raises:
        ValueError: 账户不存在或 start > end。
    """
    if start > end:
        raise ValueError(f"归因区间非法：start={start} > end={end}")
    quota_row = conn.execute(
        "SELECT capital_quota FROM account WHERE account_id = ?", [account_id]
    ).fetchone()
    if quota_row is None:
        raise ValueError(f"账户不存在: {account_id}")
    quota = float(quota_row[0])

    trades = conn.execute(
        """
        SELECT code, side, qty, trade_date FROM trade_log
        WHERE account_id = ? AND trade_date <= ?
        ORDER BY trade_date, trade_id
        """,
        [account_id, end],
    ).fetchall()
    has_position = conn.execute(
        "SELECT 1 FROM position WHERE account_id = ? LIMIT 1", [account_id]
    ).fetchone()
    if not trades and has_position is None:
        logger.info("账本为空，无可归因对象: %s", account_id)
        return None

    cash_row = conn.execute(
        """
        SELECT COALESCE(SUM(amount_cny), 0) FROM cash_ledger
        WHERE account_id = ? AND trade_date < ?
        """,
        [account_id, start],
    ).fetchone()
    cash_start = quota + (float(cash_row[0]) if cash_row else 0.0)

    # 首笔 BUY 判定新建仓（≤end 全量成交）；期初持仓复用公共重放助手
    first_buy: dict[str, date] = {}
    for code, side, _qty, trade_date in trades:
        if side == "BUY":
            first_buy.setdefault(code, trade_date)
    holdings = replay_holdings(conn, account_id, start)
    new_positions = tuple(
        NewPositionItem(code=c, open_date=first_buy[c].isoformat())
        for c in sorted(first_buy)
        if start <= first_buy[c] <= end
    )

    industry_map = _industry_map(conn)

    # ---- 组合端：期初持仓市值加权段收益 ----
    per_code: list[tuple[str, float, float]] = []  # (seg, mv_start, ret)
    dropped: list[str] = []
    total_mv = 0.0
    for code, qty in sorted(holdings.items()):
        p0 = _adj_close(conn, code, start)
        p1 = _adj_close(conn, code, end)
        if p0 is None or p0 <= 0 or p1 is None:
            dropped.append(code)
            continue
        mv = qty * p0 * _fx_rate(conn, code, start)
        seg = industry_map.get(code) or UNKNOWN_INDUSTRY
        per_code.append((seg, mv, p1 / p0 - 1.0))
        total_mv += mv

    total_assets = cash_start + total_mv
    if total_assets <= 0:
        logger.info("期初总资产 ≤ 0，无可归因对象: %s", account_id)
        return None

    seg_agg: dict[str, list[float]] = {}  # seg -> [mv_sum, mv*ret_sum]
    for seg, mv, ret in per_code:
        agg = seg_agg.setdefault(seg, [0.0, 0.0])
        agg[0] += mv
        agg[1] += mv * ret
    portfolio_seg = {
        seg: (mv_sum / total_assets, ret_sum / mv_sum) for seg, (mv_sum, ret_sum) in seg_agg.items()
    }
    cash_weight = cash_start / total_assets

    # ---- 基准端：全市场 A 股市值加权 ----
    bench_seg, bench_dropped = _benchmark_segments(conn, start, end)
    if bench_seg is None:
        logger.info("基准 universe 为空（market_cap/行情缺失）: %s", account_id)
        return None

    segments: list[SegmentInput] = []
    for seg in sorted(set(portfolio_seg) | set(bench_seg)):
        wp, rp = portfolio_seg.get(seg, (0.0, 0.0))
        wb, rb = bench_seg.get(seg, (0.0, 0.0))
        if seg not in portfolio_seg:
            rp = rb  # 未持有段约定：rp 填 rb（selection=0）
        if seg not in bench_seg:
            rb = rp  # 基准外段约定（如港股行业）：rb 填 rp（效应全入 allocation）
        segments.append(SegmentInput(seg, wp, rp, wb, rb))

    notes: list[str] = []
    if dropped:
        notes.append(f"期初无行情剔除 {len(dropped)} 只: {','.join(dropped)}")
    if bench_dropped:
        notes.append(f"基准 universe 剔除 {bench_dropped} 只（市值/行情缺失）")

    return BrinsonInputBundle(
        account_id=account_id,
        start=start,
        end=end,
        segments=tuple(segments),
        cash_weight=cash_weight,
        index_return=_index_return(conn, benchmark_code, start, end),
        new_positions=new_positions,
        dropped_codes=tuple(dropped),
        notes=tuple(notes),
    )


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def replay_holdings(
    conn: duckdb.DuckDBPyConnection, account_id: str, as_of: date
) -> dict[str, int]:
    """重放 as_of 之前（trade_date < as_of）成交得到期初持仓 {code: qty}。

    与引擎成交记账同口径：BUY +qty / SELL -qty，净持仓 ≤ 0 剔除。
    review pipeline 的逐持仓 thesis 复核复用本助手，保证与归因期初口径一致。
    """
    rows = conn.execute(
        """
        SELECT code, side, qty FROM trade_log
        WHERE account_id = ? AND trade_date < ?
        ORDER BY trade_date, trade_id
        """,
        [account_id, as_of],
    ).fetchall()
    holdings: dict[str, int] = {}
    for code, side, qty in rows:
        holdings[code] = holdings.get(code, 0) + (qty if side == "BUY" else -qty)
    return {c: q for c, q in holdings.items() if q > 0}


def _industry_map(conn: duckdb.DuckDBPyConnection) -> dict[str, str | None]:
    rows = conn.execute("SELECT code, industry FROM dim_stock").fetchall()
    return {r[0]: r[1] for r in rows}


def _adj_close(conn: duckdb.DuckDBPyConnection, code: str, as_of: date) -> float | None:
    """as-of 复权收盘（backward 对齐）；NaN/无数据返回 None。"""
    row = conn.execute(
        """
        SELECT close * COALESCE(adj_factor, 1.0) FROM fact_daily_bar
        WHERE code = ? AND trade_date <= ? AND close IS NOT NULL
        ORDER BY trade_date DESC LIMIT 1
        """,
        [code, as_of],
    ).fetchone()
    if not row:
        return None
    value = float(row[0])
    return value if value == value else None  # NaN 过滤（NaN != NaN）


def _fx_rate(conn: duckdb.DuckDBPyConnection, code: str, as_of: date) -> float:
    """A 股恒 1；港股取 ≤as_of 最近 HKD/CNY（无数据容忍为 1，与账本一致）。"""
    if len(code) != 5:
        return 1.0
    row = conn.execute(
        """
        SELECT rate FROM fact_fx_rate
        WHERE pair = 'HKD/CNY' AND trade_date <= ?
        ORDER BY trade_date DESC LIMIT 1
        """,
        [as_of],
    ).fetchone()
    return float(row[0]) if row else 1.0


def _benchmark_segments(
    conn: duckdb.DuckDBPyConnection, start: date, end: date
) -> tuple[dict[str, tuple[float, float]] | None, int]:
    """全市场 A 股按行业市值加权：((seg -> (wb, rb)), 剔除只数)；universe 空返回 (None, n)。

    单条 SQL 取每标的 as-of start 市值 + as-of start/end 复权收盘，
    避免逐标的查询（全市场 5k+ 只）。剔除只数 = universe 中缺市值/行情的标数，
    供 notes 透明展示。
    """
    rows = conn.execute(
        f"""
        WITH mcap AS (
            SELECT code, market_cap FROM (
                SELECT code, market_cap,
                       ROW_NUMBER() OVER (PARTITION BY code ORDER BY trade_date DESC) rn
                FROM fact_valuation
                WHERE trade_date <= ? AND market_cap IS NOT NULL
                      AND NOT isnan(market_cap) AND market_cap > 0
            ) WHERE rn = 1
        ), p0 AS (
            SELECT code, adj_close FROM (
                SELECT code, close * COALESCE(adj_factor, 1.0) AS adj_close,
                       ROW_NUMBER() OVER (PARTITION BY code ORDER BY trade_date DESC) rn
                FROM fact_daily_bar
                WHERE trade_date <= ? AND close IS NOT NULL AND close > 0
            ) WHERE rn = 1
        ), p1 AS (
            SELECT code, adj_close FROM (
                SELECT code, close * COALESCE(adj_factor, 1.0) AS adj_close,
                       ROW_NUMBER() OVER (PARTITION BY code ORDER BY trade_date DESC) rn
                FROM fact_daily_bar
                WHERE trade_date <= ? AND close IS NOT NULL AND close > 0
            ) WHERE rn = 1
        )
        SELECT COALESCE(s.industry, '{UNKNOWN_INDUSTRY}') AS seg,
               m.market_cap, p0.adj_close, p1.adj_close
        FROM dim_stock s
        JOIN mcap m ON m.code = s.code
        JOIN p0 ON p0.code = s.code
        JOIN p1 ON p1.code = s.code
        WHERE s.market IN {_A_SHARE_MARKETS}
          AND NOT isnan(p0.adj_close) AND p0.adj_close > 0
          AND NOT isnan(p1.adj_close)
        """,
        [start, start, end],
    ).fetchall()
    count_row = conn.execute(
        f"SELECT count(*) FROM dim_stock WHERE market IN {_A_SHARE_MARKETS}"
    ).fetchone()
    dropped = (int(count_row[0]) if count_row else 0) - len(rows)
    if not rows:
        return None, dropped
    seg_mcap: dict[str, float] = {}
    seg_ret_w: dict[str, float] = {}
    total_mcap = 0.0
    for seg, mcap, p0, p1 in rows:
        mcap = float(mcap)
        ret = float(p1) / float(p0) - 1.0
        seg_mcap[seg] = seg_mcap.get(seg, 0.0) + mcap
        seg_ret_w[seg] = seg_ret_w.get(seg, 0.0) + mcap * ret
        total_mcap += mcap
    if total_mcap <= 0:
        return None, dropped
    return (
        {seg: (mc / total_mcap, seg_ret_w[seg] / mc) for seg, mc in seg_mcap.items()},
        dropped,
    )


def _index_return(
    conn: duckdb.DuckDBPyConnection, code: str, start: date, end: date
) -> float | None:
    """指数 as-of 区间收益；表空/缺数据返回 None。"""

    def _close(as_of: date) -> float | None:
        row = conn.execute(
            """
            SELECT close FROM fact_index_bar
            WHERE code = ? AND trade_date <= ?
            ORDER BY trade_date DESC LIMIT 1
            """,
            [code, as_of],
        ).fetchone()
        return float(row[0]) if row else None

    p0, p1 = _close(start), _close(end)
    if p0 is None or p1 is None or p0 <= 0:
        return None
    return p1 / p0 - 1.0


__all__ = [
    "UNKNOWN_INDUSTRY",
    "BrinsonInputBundle",
    "load_brinson_input",
]
