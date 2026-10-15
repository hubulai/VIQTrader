"""P2 双轨账本引擎（设计文档第五、七节）。

核心纪律：
- FILLED 才扣款；扣款前重检资金充足性（INSUFFICIENT_CASH_AT_FILL 兜底，已确认决策）
- A 股 T+1：BUY 后 frozen_qty += qty，日终结算清零；港股通 T+0 不冻结
- 双轨隔离：同标的两轨各一行持仓，禁止合并；所有查询带 account_id
- 资金全程 Decimal，写入 DuckDB 转 DOUBLE
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import duckdb

from viqtrader import __version__
from viqtrader.engine.types import (
    FeeBreakdown,
    InsufficientCashAtFill,
    OrderIntent,
    OrderSide,
    TradeLog,
    to_decimal,
)

_A_SHARE_MARKETS = frozenset({"SH", "SZ", "BJ"})
_D0 = Decimal("0")


class LedgerEngine:
    """账本引擎：成交记账、T+1 结算、分红入账。"""

    def __init__(self, conn: duckdb.DuckDBPyConnection, config: Any) -> None:
        self._conn = conn
        self._config = config
        self._config_hash = str(config.content_hash())
        self._code_version = __version__

    # ------------------------------------------------------------------
    # 成交记账
    # ------------------------------------------------------------------

    def execute_fill(
        self,
        intent: OrderIntent,
        fill_price: Decimal,
        fees: FeeBreakdown,
        trade_date: date,
    ) -> TradeLog:
        """成交入账。BUY 扣款前重检资金；SELL 扣减前重检可用持仓。

        Raises:
            InsufficientCashAtFill: BUY 扣款时资金不足（兜底路径，设计文档 5.2）。
            ValueError: SELL 可用持仓不足或无持仓。
        """
        fx = self._fx_rate(intent.code, trade_date)
        turnover_cny = fill_price * intent.qty * fx

        conn = self._conn
        conn.execute("BEGIN TRANSACTION")
        try:
            realized_pnl = _D0
            if intent.side == OrderSide.BUY:
                cash = self._current_cash(intent.account_id)
                needed = turnover_cny + fees.total
                if needed > cash:
                    raise InsufficientCashAtFill(
                        f"FILLED 扣款重检失败：需要 {needed} > 可用 {cash}"
                        f"（intent_id={intent.intent_id}）"
                    )
                self._apply_buy(intent, fill_price, fx, trade_date)
                self._append_cash(intent.account_id, trade_date, "买入", -needed, cash - needed)
            else:
                avg_cost_cny = self._apply_sell(intent)
                cash = self._current_cash(intent.account_id)
                net = turnover_cny - fees.total
                self._append_cash(intent.account_id, trade_date, "卖出", net, cash + net)
                # 已实现盈亏 = (卖出价 x fx - 人民币口径成本) x qty - 费用（汇兑损益归因层处理）
                realized_pnl = (fill_price * fx - avg_cost_cny) * intent.qty - fees.total
            trade = self._write_trade_log(
                intent, fill_price, fees, fx, trade_date, realized_pnl_cny=realized_pnl
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return trade

    def _apply_buy(
        self, intent: OrderIntent, fill_price: Decimal, fx: Decimal, trade_date: date
    ) -> None:
        """BUY：持仓 upsert（加权成本）+ A 股 T+1 冻结。"""
        conn = self._conn
        row = conn.execute(
            """
            SELECT position_id, qty, avg_cost, avg_cost_cny, COALESCE(frozen_qty, 0)
            FROM position WHERE account_id = ? AND code = ?
            """,
            [intent.account_id, intent.code],
        ).fetchone()

        freeze_qty = intent.qty if intent.market in _A_SHARE_MARKETS else 0
        if row is None:
            position_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO position
                    (position_id, account_id, mode, code, qty, avg_cost, avg_cost_cny,
                     open_date, thesis_id, frozen_qty)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    position_id,
                    intent.account_id,
                    intent.mode,
                    intent.code,
                    intent.qty,
                    float(fill_price),
                    float(fill_price * fx),
                    trade_date,
                    intent.thesis_id,
                    freeze_qty,
                ],
            )
        else:
            position_id, qty, avg_cost, avg_cost_cny, frozen = row
            new_qty = int(qty) + intent.qty
            new_avg = (Decimal(str(avg_cost)) * qty + fill_price * intent.qty) / new_qty
            new_avg_cny = (
                Decimal(str(avg_cost_cny)) * qty + fill_price * fx * intent.qty
            ) / new_qty
            conn.execute(
                """
                UPDATE position SET qty = ?, avg_cost = ?, avg_cost_cny = ?,
                       frozen_qty = ? WHERE position_id = ?
                """,
                [
                    new_qty,
                    float(new_avg),
                    float(new_avg_cny),
                    int(frozen) + freeze_qty,
                    position_id,
                ],
            )

    def _apply_sell(self, intent: OrderIntent) -> Decimal:
        """SELL：可用持仓重检 + 减持（清仓删除行）。返回人民币口径成本（算 realized_pnl）。"""
        conn = self._conn
        row = conn.execute(
            """
            SELECT position_id, qty, COALESCE(frozen_qty, 0), avg_cost_cny
            FROM position WHERE account_id = ? AND code = ?
            """,
            [intent.account_id, intent.code],
        ).fetchone()
        if row is None:
            raise ValueError(f"SELL 无持仓: account={intent.account_id} code={intent.code}")
        position_id, qty, frozen, avg_cost_cny = row
        if intent.qty > int(qty) - int(frozen):
            raise ValueError(
                f"SELL 可用持仓不足: 可用 {int(qty) - int(frozen)} < {intent.qty}"
                f"（intent_id={intent.intent_id}）"
            )

        remaining = int(qty) - intent.qty
        if remaining > 0:
            conn.execute(
                "UPDATE position SET qty = ? WHERE position_id = ?",
                [remaining, position_id],
            )
        else:
            conn.execute("DELETE FROM position WHERE position_id = ?", [position_id])
        return Decimal(str(avg_cost_cny))

    def _write_trade_log(
        self,
        intent: OrderIntent,
        fill_price: Decimal,
        fees: FeeBreakdown,
        fx: Decimal,
        trade_date: date,
        *,
        realized_pnl_cny: Decimal = _D0,
    ) -> TradeLog:
        """写 trade_log（含 intent_id 关联），返回内存 TradeLog。"""
        conn = self._conn
        trade_id = str(uuid.uuid4())
        turnover_cny = fill_price * intent.qty * fx
        amount_cny = (
            -(turnover_cny + fees.total)
            if intent.side == OrderSide.BUY
            else turnover_cny - fees.total
        )
        conn.execute(
            """
            INSERT INTO trade_log
                (trade_id, account_id, mode, code, side, qty, price,
                 commission, stamp_tax, transfer_fee, fx_rate, amount_cny,
                 trade_date, intent_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                trade_id,
                intent.account_id,
                intent.mode,
                intent.code,
                str(intent.side),
                intent.qty,
                float(fill_price),
                float(fees.commission),
                float(fees.stamp_tax),
                float(fees.transfer_fee),
                float(fx),
                float(amount_cny),
                trade_date,
                intent.intent_id,
            ],
        )
        return TradeLog(
            trade_id=trade_id,
            account_id=intent.account_id,
            mode=intent.mode,
            code=intent.code,
            side=str(intent.side),
            qty=intent.qty,
            price=fill_price,
            commission=fees.commission,
            stamp_tax=fees.stamp_tax,
            transfer_fee=fees.transfer_fee,
            fx_rate=fx,
            amount_cny=amount_cny,
            trade_date=trade_date,
            intent_id=intent.intent_id,
            realized_pnl_cny=realized_pnl_cny,
        )

    # ------------------------------------------------------------------
    # T+1 日终结算
    # ------------------------------------------------------------------

    def settle_end_of_day(self, trade_date: date) -> None:
        """日终解冻：清零全部 frozen_qty（回测在每个新交易日 bar 开始前同样调用）。"""
        self._conn.execute("UPDATE position SET frozen_qty = 0 WHERE COALESCE(frozen_qty, 0) > 0")

    # ------------------------------------------------------------------
    # 分红入账
    # ------------------------------------------------------------------

    def process_dividend(
        self,
        code: str,
        ex_date: date,
        dps: Decimal,
        dividend_type: str,
    ) -> list[Decimal]:
        """按持仓行逐账户入账现金分红；同标的两轨各自入各自的账。

        - A 股：amount = qty x dps（CNY）
        - 港股通：amount = qty x dps x (1 - tax) x fx（税后折 CNY，统一 0.20 已确认）
        - 送股 v1 暂不支持（设计文档 7.4 TODO）

        Returns:
            每个持仓行（账户）的入账金额（CNY），按 position 查询顺序。
        """
        if dividend_type != "现金":
            raise NotImplementedError(f"v1 仅支持现金分红，收到: {dividend_type}")

        is_hk = len(code) == 5
        conn = self._conn
        rows = conn.execute(
            "SELECT account_id, qty FROM position WHERE code = ? AND qty > 0",
            [code],
        ).fetchall()
        amounts: list[Decimal] = []
        for account_id, qty in rows:
            if is_hk:
                tax_rate = to_decimal(
                    (self._config.as_dict().get("hk_connect", {}) or {}).get(
                        "dividend_tax_h_share", 0.20
                    ),
                    Decimal("0.20"),
                )
                fx = self._fx_rate(code, ex_date)
                amount = dps * qty * (Decimal("1") - tax_rate) * fx
            else:
                amount = dps * qty
            cash = self._current_cash(account_id)
            conn.execute("BEGIN TRANSACTION")
            try:
                self._append_cash(account_id, ex_date, "分红", amount, cash + amount)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            amounts.append(amount)
        return amounts

    # ------------------------------------------------------------------
    # 资金视图
    # ------------------------------------------------------------------

    def get_available_cash(self, account_id: str) -> Decimal:
        """本轨可用现金 = 初始配额 + 全部流水净额。"""
        return self._current_cash(account_id)

    def get_track_value(self, account_id: str, as_of: date) -> Decimal:
        """单轨总价值 = 可用现金 + 持仓市值（按最近收盘折 CNY）。"""
        cash = self._current_cash(account_id)
        rows = self._conn.execute(
            "SELECT code, qty FROM position WHERE account_id = ? AND qty > 0",
            [account_id],
        ).fetchall()
        market_value = _D0
        for code, qty in rows:
            close = self._latest_close(code, as_of)
            if close is None:
                continue
            market_value += close * qty * self._fx_rate(code, as_of)
        return cash + market_value

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _current_cash(self, account_id: str) -> Decimal:
        quota = self._conn.execute(
            "SELECT capital_quota FROM account WHERE account_id = ?", [account_id]
        ).fetchone()
        if quota is None:
            raise ValueError(f"账户不存在: {account_id}")
        flowed = self._conn.execute(
            "SELECT COALESCE(SUM(amount_cny), 0) FROM cash_ledger WHERE account_id = ?",
            [account_id],
        ).fetchone()
        if flowed is None:
            return Decimal(str(quota[0]))
        return Decimal(str(quota[0])) + Decimal(str(flowed[0]))

    def _append_cash(
        self,
        account_id: str,
        trade_date: date,
        change_type: str,
        amount: Decimal,
        balance: Decimal,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO cash_ledger (entry_id, account_id, trade_date, change_type,
                                     amount_cny, balance_cny)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                str(uuid.uuid4()),
                account_id,
                trade_date,
                change_type,
                float(amount),
                float(balance),
            ],
        )

    def _latest_close(self, code: str, as_of: date) -> Decimal | None:
        row = self._conn.execute(
            """
            SELECT close FROM fact_daily_bar
            WHERE code = ? AND trade_date <= ? AND close IS NOT NULL
            ORDER BY trade_date DESC LIMIT 1
            """,
            [code, as_of],
        ).fetchone()
        return to_decimal(row[0]) if row else None

    def _fx_rate(self, code: str, as_of: date) -> Decimal:
        """原币→CNY：A 股恒 1；港股取 ≤as_of 最近 HKD/CNY 汇率（无数据时容忍为 1）。"""
        if len(code) != 5:
            return Decimal("1")
        pair = str(self._config.as_dict().get("data", {}).get("fx_pair", "HKD/CNY"))
        row = self._conn.execute(
            """
            SELECT rate FROM fact_fx_rate
            WHERE pair = ? AND trade_date <= ?
            ORDER BY trade_date DESC LIMIT 1
            """,
            [pair, as_of],
        ).fetchone()
        return to_decimal(row[0], Decimal("1")) if row else Decimal("1")
