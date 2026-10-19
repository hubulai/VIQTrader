"""order_request 消费器（P6 执行侧，拍板③，6.3 人工确认闸唯一消费方）。

链路：扫描 order_request.pending → 逐条构造 OrderIntent →
ExecutionService.submit_order（三阶段校验 + DryRun 网关 + 账本，全链留痕）→
回写 request 终态。

一次性消费语义（幂等锚点 = request_id，pending 以外状态不再触碰）：
- FILLED → status='executed' + intent_id + executed_at，note=成交价；
- REJECTED（规则否决 / 成交时资金不足）→ status='rejected' + intent_id，note=原因；
- NOT_FILLED（限价劣于参考价 / 无行情）→ status='rejected'，note=NOT_FILLED 原因。
  dry-run 为即时成交语义：未成交即作废，用户改价后重新确认，
  避免同一 request 重复产生 order_intent。

账户初始化：按 config `accounts` 节（value/swing → acc_value/acc_swing）
幂等建账户配额，已存在跳过。可用现金口径 = capital_quota + Σ cash_ledger
（见 engine/ledger/engine.py `_current_cash`），即配额本身已是初始资金，
故不得再写「初始入金」流水，否则额度被重复计账。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import duckdb

from viqtrader.engine.backtest.engine import default_rules
from viqtrader.engine.execution.gateway import DryRunGateway
from viqtrader.engine.execution.service import ExecutionService
from viqtrader.engine.market_status import refresh_market_status
from viqtrader.engine.types import IntentStatus, OrderIntent, OrderSide, OrderType


@dataclass(frozen=True, slots=True)
class ConsumeOutcome:
    request_id: str
    code: str
    side: str
    qty: int
    result: str  # executed / rejected
    detail: str  # FILLED@成交价 / 拒绝原因


@dataclass(frozen=True, slots=True)
class ConsumeReport:
    trade_date: date
    outcomes: tuple[ConsumeOutcome, ...]
    accounts_initialized: tuple[str, ...]

    @property
    def executed(self) -> int:
        return sum(1 for o in self.outcomes if o.result == "executed")

    @property
    def rejected(self) -> int:
        return sum(1 for o in self.outcomes if o.result == "rejected")


def ensure_accounts(conn: duckdb.DuckDBPyConnection, config: Any) -> list[str]:
    """按 config accounts 节幂等初始化模拟盘账户配额。返回本次新建 account_id 列表。

    只写 account 行（capital_quota 即初始资金）；可用现金另有口径
    = capital_quota + Σ cash_ledger，故此处不写初始入金流水。
    """
    accounts = config.get("accounts", {}) or {}
    created: list[str] = []
    for key, spec in accounts.items():
        if not isinstance(spec, dict):
            continue
        account_id = f"acc_{key}"
        exists = conn.execute("SELECT 1 FROM account WHERE account_id = ?", [account_id]).fetchone()
        if exists:
            continue
        quota = float(spec.get("initial_quota_cny", 0))
        ratio = float(spec.get("target_ratio", 0))
        conn.execute(
            "INSERT INTO account (account_id, mode, base_currency, capital_quota, target_ratio) "
            "VALUES (?, ?, 'CNY', ?, ?)",
            [account_id, str(key), quota, ratio],
        )
        created.append(account_id)
    return created


def latest_trade_date(conn: duckdb.DuckDBPyConnection) -> date | None:
    """库内最新交易日（成交参考日：DryRun 网关按当日 open 成交）。"""
    row = conn.execute("SELECT max(trade_date) FROM fact_daily_bar").fetchone()
    if row is None or row[0] is None:
        return None
    value = row[0]
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def consume_order_requests(
    conn: duckdb.DuckDBPyConnection, config: Any, trade_date: date
) -> ConsumeReport:
    """消费全部 pending 请求（按提交时间升序），逐条走完整执行管线并回写终态。"""
    created = ensure_accounts(conn, config)
    rows = conn.execute(
        """
        SELECT request_id, account_id, mode, code, market, side, order_type,
               qty, price, thesis_id
        FROM order_request WHERE status = 'pending'
        ORDER BY created_at, request_id
        """
    ).fetchall()
    # 规则引擎依赖 market_status 快照（停牌/涨跌停/港股通名单）：
    # 只刷新本次待成交标的（全市场逐只查 5000+ 标的既慢又无必要），空队列跳过
    if rows:
        refresh_market_status(
            conn, config.as_dict(), trade_date, codes=sorted({str(r[3]) for r in rows})
        )
    svc = ExecutionService(conn, config, default_rules(), DryRunGateway(conn, config))
    outcomes: list[ConsumeOutcome] = []
    for r in rows:
        request_id = str(r[0])
        intent = OrderIntent(
            intent_id=str(uuid.uuid4()),
            account_id=str(r[1]),
            mode=str(r[2]),
            code=str(r[3]),
            market=str(r[4]),
            side=OrderSide(str(r[5])),
            order_type=OrderType(str(r[6])),
            qty=int(r[7]),
            price=Decimal(str(r[8])) if r[8] is not None else None,
            thesis_id=str(r[9]) if r[9] is not None else None,
        )
        result = svc.submit_order(intent, trade_date)
        if result.status == IntentStatus.FILLED:
            final_status = "executed"
            note = f"FILLED@{result.trade.price}" if result.trade else "FILLED"
        else:
            final_status = "rejected"
            note = result.rejection_reason
        conn.execute(
            "UPDATE order_request SET status = ?, intent_id = ?, executed_at = now(), "
            "note = ? WHERE request_id = ?",
            [final_status, intent.intent_id, note, request_id],
        )
        outcomes.append(
            ConsumeOutcome(
                request_id, intent.code, str(intent.side), intent.qty, final_status, note or ""
            )
        )
    return ConsumeReport(trade_date, tuple(outcomes), tuple(created))
