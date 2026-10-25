"""持仓止损扫描（P5.1 衍生欠账，激活 DAG risk_scan 占位）。

价值轨：基本面两档止损的 **watch 档定量检测**（watch_events 配置驱动）；
exit_events（暴雷/证伪）属人工/LLM 判断不适用规则扫描；**价格下跌本身永不触发
价值轨卖出**（设计基线）。roe 代理 ROIC：QMT 财务 5 表无 roic 字段、有息负债
拆分缺失致自算退化，roe 为落库指标中盈利能力最接近的代理；后续 AkShare 乐咕源
补 ROIC 后切换口径（配置键保留 roic_below_min_2y 事件名）。

波段轨：价格止损（对成本线）/ 回撤止损（对持仓期间峰值）/ 持有期上限三事件。

只写 risk_event（severity=WARN / decision=ALERT）+ WARN 告警，不自动下单
（v1 模拟盘人工确认卖出）。幂等：event_id 确定性生成（rule_key|account|code|
open_date），已存在即跳过。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

import duckdb

from viqtrader import __version__
from viqtrader.config.loader import Config
from viqtrader.data.financials import ReportedFinancial, reported_financials

_VALUE_EVENT_ROE = "roic_below_min_2y"
_VALUE_EVENT_FCF = "fcf_negative_2y"
_VALUE_EVENT_DIVIDEND = "dividend_cut"


@dataclass(frozen=True, slots=True)
class RiskEvent:
    """单条扫描触发的事件（落库前载体）。"""

    rule_key: str
    account_id: str
    mode: str
    code: str
    position_id: str
    message: str


@dataclass(frozen=True, slots=True)
class RiskScanReport:
    """止损扫描结果。

    events 仅含**新触发**（尚未落库）的事件——已存在事件不重复报告不重复告警，
    以 suppressed 计数呈现。
    """

    today: date
    scanned: int
    events: list[RiskEvent] = field(default_factory=list)
    suppressed: int = 0
    skipped_no_market: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 纯函数判定
# ---------------------------------------------------------------------------


def is_below_min_consecutive(values: Sequence[float | None], floor: float) -> bool:
    """最近 N 期指标全部低于下限（任一缺失 → 宁缺勿错不触发）。"""
    if not values:
        return False
    return all(v is not None and v < floor for v in values)


def is_fcf_negative_consecutive(values: Sequence[float | None]) -> bool:
    """最近 N 期自由现金流全部为负（任一缺失 → 不触发）。"""
    if not values:
        return False
    return all(v is not None and v < 0 for v in values)


def is_dividend_cut(current_total: float | None, prior_total: float | None) -> bool:
    """当年每股分红总额 < 上年（任一年无记录 → 不触发，避免中途误报）。"""
    if current_total is None or prior_total is None:
        return False
    return current_total < prior_total


def is_price_stop_loss(close: float | None, avg_cost: float, stop_pct: float) -> bool:
    """价格止损：最新收盘 ≤ 成本 * (1 + stop_pct)，stop_pct 为负数（如 -0.08）。"""
    if close is None:
        return False
    return close <= avg_cost * (1 + stop_pct)


def is_max_drawdown(close: float | None, peak: float | None, dd_pct: float) -> bool:
    """回撤止损：最新收盘 ≤ 持仓期间峰值 * (1 + dd_pct)，dd_pct 为负数（如 -0.15）。"""
    if close is None or peak is None or peak <= 0:
        return False
    return close <= peak * (1 + dd_pct)


def is_over_holding(open_date: date, today: date, max_days: int) -> bool:
    """持有期上限：严格大于才计超期（与 P3.4 波段口径一致）。"""
    return (today - open_date).days > max_days


# ---------------------------------------------------------------------------
# 扫描装配
# ---------------------------------------------------------------------------


def scan_position_risk(
    conn: duckdb.DuckDBPyConnection,
    config: Config,
    today: date,
) -> RiskScanReport:
    """扫描全部持仓（qty>0）的风控事件并落 risk_event。幂等可每日重跑。"""
    watch_events = [str(e) for e in config.get("value_track.stop_loss.watch_events", [])]
    roe_min = float(config.get("value_track.stop_loss.roe_min", 0.12))
    price_stop = float(config.get("swing_track.risk.price_stop_loss", -0.08))
    max_dd = float(config.get("swing_track.risk.max_drawdown", -0.15))
    max_hold = int(config.get("swing_track.risk.max_holding_days", 20))
    config_hash = str(config.content_hash())

    rows = conn.execute(
        """
        SELECT p.position_id, p.account_id, p.mode, p.code, p.avg_cost, p.open_date,
               b.close AS latest_close
        FROM position p
        LEFT JOIN fact_daily_bar b
          ON b.code = p.code
         AND b.trade_date = (SELECT max(trade_date) FROM fact_daily_bar)
        WHERE p.qty > 0
        ORDER BY p.account_id, p.code
        """
    ).fetchall()

    events: list[RiskEvent] = []
    skipped: list[str] = []

    for r in rows:
        position_id, account_id, mode, code, avg_cost, open_date, latest_close = r

        if latest_close is None:
            skipped.append(position_id)
            continue

        if mode == "value":
            events.extend(
                _scan_value(
                    conn,
                    config_hash,
                    watch_events,
                    roe_min,
                    today,
                    position_id,
                    account_id,
                    code,
                )
            )
        elif mode == "swing":
            events.extend(
                _scan_swing(
                    conn,
                    config_hash,
                    price_stop,
                    max_dd,
                    max_hold,
                    position_id,
                    account_id,
                    code,
                    float(avg_cost),
                    open_date,
                    float(latest_close),
                    today,
                )
            )

    # 幂等分拣：已存在事件不重复落库不重复告警，仅计数
    new_events: list[RiskEvent] = []
    suppressed = 0
    for e in events:
        if conn.execute("SELECT 1 FROM risk_event WHERE event_id = ?", [_event_id(e)]).fetchone():
            suppressed += 1
        else:
            new_events.append(e)

    _persist_events(conn, new_events, config_hash)
    return RiskScanReport(
        today=today,
        scanned=len(rows),
        events=new_events,
        suppressed=suppressed,
        skipped_no_market=skipped,
    )


def _annual_financials(
    conn: duckdb.DuckDBPyConnection, code: str, today: date, n: int = 2
) -> list[ReportedFinancial]:
    """最近 n 个年度报告（report_period 为 12-31，ann_date ≤ today 防前视）。

    fact_financial_version 流量列（fcf）为**单季口径**，经 reported_financials 按 as-of 取
    可见版本并还原为年度累计（12-31 期即全年四季求和）；roe 为 Q4 行的 YTD 原值，即年度值。
    """
    rows = reported_financials(conn, code, ann_before=today)
    return [r for r in rows if r.report_period.month == 12][:n]


def _scan_value(
    conn: duckdb.DuckDBPyConnection,
    config_hash: str,
    watch_events: list[str],
    roe_min: float,
    today: date,
    position_id: str,
    account_id: str,
    code: str,
) -> list[RiskEvent]:
    """价值轨 watch 档三事件（配置 watch_events 驱动，仅判定不决定卖出）。"""
    events: list[RiskEvent] = []

    roes: list[float | None] = []
    fcfs: list[float | None] = []
    if _VALUE_EVENT_ROE in watch_events or _VALUE_EVENT_FCF in watch_events:
        annuals = _annual_financials(conn, code, today)
        roes = [r.roe for r in annuals]
        fcfs = [r.fcf for r in annuals]

    if _VALUE_EVENT_ROE in watch_events and is_below_min_consecutive(roes, roe_min):
        events.append(
            RiskEvent(
                rule_key=f"value.{_VALUE_EVENT_ROE}",
                account_id=account_id,
                mode="value",
                code=code,
                position_id=position_id,
                message=(
                    f"连续 2 个年度 ROE（ROIC 代理）低于 {roe_min:.0%}："
                    f"{[None if v is None else round(v, 4) for v in roes]}，建议 thesis 复核"
                ),
            )
        )

    if _VALUE_EVENT_FCF in watch_events and is_fcf_negative_consecutive(fcfs):
        events.append(
            RiskEvent(
                rule_key=f"value.{_VALUE_EVENT_FCF}",
                account_id=account_id,
                mode="value",
                code=code,
                position_id=position_id,
                message=f"连续 2 个年度 FCF（全年累计）为负：{fcfs}，建议 thesis 复核",
            )
        )

    if _VALUE_EVENT_DIVIDEND in watch_events:
        dividend_row = conn.execute(
            """
            SELECT
                sum(CASE WHEN year(ex_date) = ? THEN dividend_per_share END),
                sum(CASE WHEN year(ex_date) = ? THEN dividend_per_share END)
            FROM fact_dividend
            WHERE code = ? AND dividend_type = '现金'
              AND year(ex_date) IN (?, ?)
            """,
            [today.year, today.year - 1, code, today.year, today.year - 1],
        ).fetchone()
        cur, prior = dividend_row if dividend_row else (None, None)
        if is_dividend_cut(cur, prior):
            events.append(
                RiskEvent(
                    rule_key=f"value.{_VALUE_EVENT_DIVIDEND}",
                    account_id=account_id,
                    mode="value",
                    code=code,
                    position_id=position_id,
                    message=(f"当年每股分红 {cur:.4f} 低于上年 {prior:.4f}，建议 thesis 复核"),
                )
            )

    return events


def _scan_swing(
    conn: duckdb.DuckDBPyConnection,
    config_hash: str,
    price_stop: float,
    max_dd: float,
    max_hold: int,
    position_id: str,
    account_id: str,
    code: str,
    avg_cost: float,
    open_date: date,
    latest_close: float,
    today: date,
) -> list[RiskEvent]:
    """波段轨三事件：价格止损 / 回撤止损 / 持有期上限。"""
    events: list[RiskEvent] = []

    if is_price_stop_loss(latest_close, avg_cost, price_stop):
        events.append(
            RiskEvent(
                rule_key="swing.price_stop_loss",
                account_id=account_id,
                mode="swing",
                code=code,
                position_id=position_id,
                message=(
                    f"最新价 {latest_close:.2f} 跌破止损线 "
                    f"{avg_cost * (1 + price_stop):.2f}（成本 {avg_cost:.2f}，{price_stop:.0%}）"
                ),
            )
        )

    # 回撤止损：peak = 开仓以来最高收盘（对峰值口径，与对成本的固定止损互补）
    peak_row = conn.execute(
        "SELECT max(close) FROM fact_daily_bar WHERE code = ? AND trade_date >= ?",
        [code, open_date],
    ).fetchone()
    peak = float(peak_row[0]) if peak_row and peak_row[0] is not None else None
    if is_max_drawdown(latest_close, peak, max_dd):
        events.append(
            RiskEvent(
                rule_key="swing.max_drawdown",
                account_id=account_id,
                mode="swing",
                code=code,
                position_id=position_id,
                message=(f"持仓期间峰值 {peak:.2f} 回撤至 {latest_close:.2f}（超 {max_dd:.0%}）"),
            )
        )

    if is_over_holding(open_date, today, max_hold):
        events.append(
            RiskEvent(
                rule_key="swing.max_holding_days",
                account_id=account_id,
                mode="swing",
                code=code,
                position_id=position_id,
                message=(
                    f"持有 {(today - open_date).days} 天超上限 {max_hold} 天（开仓 {open_date}）"
                ),
            )
        )

    return events


def _event_id(e: RiskEvent) -> str:
    """确定性主键：同持仓同规则只告警一次，直到仓位重建（open_date 变化）。"""
    return f"{e.rule_key}|{e.account_id}|{e.code}|{e.position_id}"


def _persist_events(
    conn: duckdb.DuckDBPyConnection, events: list[RiskEvent], config_hash: str
) -> None:
    """写入新事件，显式 commit 不关闭共享连接。"""
    if not events:
        return
    for e in events:
        conn.execute(
            """
            INSERT INTO risk_event
            (event_id, account_id, mode, rule_key, code, severity, decision,
             intent_snapshot, message, config_hash, code_version)
            VALUES (?, ?, ?, ?, ?, 'WARN', 'ALERT', NULL, ?, ?, ?)
            """,
            [
                _event_id(e),
                e.account_id,
                e.mode,
                e.rule_key,
                e.code,
                e.message,
                config_hash,
                str(__version__),
            ],
        )
    conn.commit()


__all__ = [
    "RiskEvent",
    "RiskScanReport",
    "is_below_min_consecutive",
    "is_dividend_cut",
    "is_fcf_negative_consecutive",
    "is_max_drawdown",
    "is_over_holding",
    "is_price_stop_loss",
    "scan_position_risk",
]
