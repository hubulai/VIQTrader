"""总览页数据装配（P6）：平台运行状态（live_state.json）+ 全账户卡片。

只读：数据库 read_only 连接 + outputs/ 状态文件，零写入。
市值口径：A 股 qty 乘最新收盘价；港股通持仓按最新 HKD/CNY 汇率折算
（length(code)=5 判 HK），无汇率数据时按原币计入并在页面标注。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import duckdb

MAIN_ACCOUNTS = ("acc_value", "acc_swing")

_ACCOUNT_CARDS_SQL = """
WITH latest_bar AS (
    SELECT code, close FROM fact_daily_bar
    WHERE trade_date = (SELECT max(trade_date) FROM fact_daily_bar)
),
latest_fx AS (
    SELECT rate FROM fact_fx_rate WHERE pair = 'HKD/CNY'
    ORDER BY trade_date DESC LIMIT 1
),
latest_cash AS (
    SELECT account_id, balance_cny,
           row_number() OVER (
               PARTITION BY account_id ORDER BY trade_date DESC, entry_id DESC
           ) AS rn
    FROM cash_ledger
)
SELECT a.account_id, a.mode, a.capital_quota, a.target_ratio,
       lc.balance_cny,
       sum(CASE
               WHEN lb.close IS NULL THEN NULL
               WHEN length(p.code) = 5 THEN p.qty * lb.close * COALESCE(fx.rate, 1)
               ELSE p.qty * lb.close
           END) AS market_value,
       count(p.position_id) AS position_count,
       max(CASE WHEN length(p.code) = 5 AND fx.rate IS NULL THEN 1 ELSE 0 END) AS hk_no_fx
FROM account a
LEFT JOIN position p ON p.account_id = a.account_id
LEFT JOIN latest_bar lb ON lb.code = p.code
LEFT JOIN latest_cash lc ON lc.account_id = a.account_id AND lc.rn = 1
LEFT JOIN latest_fx fx ON true
GROUP BY a.account_id, a.mode, a.capital_quota, a.target_ratio, lc.balance_cny
ORDER BY a.account_id
"""


@dataclass(frozen=True, slots=True)
class AccountCard:
    """单账户展示卡。"""

    account_id: str
    mode: str
    capital_quota: float
    target_ratio: float
    cash_cny: float | None
    market_value_cny: float | None
    position_count: int
    is_main: bool
    hk_no_fx: bool  # 有 HK 持仓但无汇率数据（市值低估，页面标注）


@dataclass(frozen=True, slots=True)
class LiveState:
    """live_state.json 视图（P5.4 契约：引擎写、控制台读）。"""

    last_run_id: str
    run_date: str
    finished_at: str
    exit_code: int
    failed_tasks: list[str]
    alert_counts: dict[str, int]


def load_live_state(outputs: Path) -> LiveState | None:
    """读取最近运行指针；文件缺失/损坏返回 None（页面降级为「暂无运行记录」）。"""
    path = outputs / "live_state.json"
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return LiveState(
        last_run_id=str(raw.get("last_run_id", "")),
        run_date=str(raw.get("run_date", "")),
        finished_at=str(raw.get("finished_at", "")),
        exit_code=int(raw.get("exit_code", 0)),
        failed_tasks=[str(t) for t in raw.get("failed_tasks", [])],
        alert_counts={str(k): int(v) for k, v in dict(raw.get("alert_counts", {})).items()},
    )


def list_account_cards(conn: duckdb.DuckDBPyConnection) -> list[AccountCard]:
    """全账户列表：主账户（acc_value/acc_swing）置顶，其余按 id 排序。"""
    rows = conn.execute(_ACCOUNT_CARDS_SQL).fetchall()
    cards = [
        AccountCard(
            account_id=str(r[0]),
            mode=str(r[1]),
            capital_quota=float(r[2]),
            target_ratio=float(r[3]),
            cash_cny=float(r[4]) if r[4] is not None else None,
            market_value_cny=float(r[5]) if r[5] is not None else None,
            position_count=int(r[6]),
            is_main=str(r[0]) in MAIN_ACCOUNTS,
            hk_no_fx=bool(r[7]),
        )
        for r in rows
    ]
    cards.sort(key=lambda c: (not c.is_main, c.account_id))
    return cards
