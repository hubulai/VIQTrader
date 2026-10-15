"""P3.1 事件驱动回测引擎：历史回放驱动器（回测/模拟盘同构）。

主循环（每个交易日 T）：
1. settle_end_of_day —— T+1 开盘前解冻（首日跳过）
2. process_dividend —— 除息日为 T 的现金分红按开盘前持仓入账（T 日到账可用近似）
3. refresh_market_status —— 复用 P2 预计算（板块/涨跌停/停牌/ST）
4. strategy.on_bar(ctx) —— 策略基于严格早于 T 的数据产 Intent（方案 A）
5. ExecutionService.submit_order —— 三阶段校验 + SimulatedGateway 撮合 + 记账
6. 逐日净值快照（按 T 日收盘价折 CNY）

引擎本身只做回放编排：规则、撮合、账本全部复用 P2 组件，零重写。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import duckdb

from viqtrader.engine.backtest.context import StrategyContext
from viqtrader.engine.backtest.result import (
    BacktestResult,
    DailyEquity,
    RejectedIntent,
)
from viqtrader.engine.backtest.strategy import Strategy
from viqtrader.engine.execution.gateway import SimulatedGateway
from viqtrader.engine.execution.service import ExecutionService
from viqtrader.engine.ledger.engine import LedgerEngine
from viqtrader.engine.market_status import refresh_market_status
from viqtrader.engine.rules.account import (
    CashSufficiencyRule,
    ModeIsolationRule,
    PositionLimitRule,
    QuotaRule,
)
from viqtrader.engine.rules.market import (
    HKConnectRule,
    LotSizeRule,
    PriceLimitRule,
    SuspensionRule,
    T1Rule,
    TradingDayRule,
)
from viqtrader.engine.types import to_decimal


def default_rules() -> list[Any]:
    """引擎层正式规则链：Phase 1 市场 6 条 + Phase 2 账户 4 条。

    与模拟盘/未来实盘同一组规则（环境同构）；
    Phase 3 风控规则（能力圈/安全边际/止损）后续接入，见任务卡 2.4 与设计文档 7.x。
    """
    return [
        T1Rule(),
        TradingDayRule(),
        SuspensionRule(),
        PriceLimitRule(),
        LotSizeRule(),
        HKConnectRule(),
        CashSufficiencyRule(),
        PositionLimitRule(),
        ModeIsolationRule(),
        QuotaRule(),
    ]


class BacktestEngine:
    """回测引擎：给定策略与区间，在历史数据上完整回放交易链路。"""

    def __init__(
        self,
        conn: duckdb.DuckDBPyConnection,
        config: Any,
        rules: list[Any] | None = None,
    ) -> None:
        self._conn = conn
        self._config = config
        self._ledger = LedgerEngine(conn, config)
        gateway = SimulatedGateway(conn, config)
        self._exec = ExecutionService(conn, config, rules or default_rules(), gateway)

    def run(
        self,
        strategy: Strategy,
        start: date,
        end: date,
        *,
        accounts: list[str] | None = None,
    ) -> BacktestResult:
        """回放 [start, end] 区间；accounts 为空时对 account 表全部账户记净值。

        Raises:
            ValueError: 区间内无任何交易日（行情数据缺失，fail-fast 而非静默空结果）。
        """
        track_accounts = accounts or self._all_accounts()
        days = self._trading_days(start, end)
        if not days:
            raise ValueError(f"[{start}, {end}] 区间内无交易日（fact_daily_bar 无数据）")

        result = BacktestResult(start=start, end=end, accounts=list(track_accounts))
        first = True
        for day in days:
            if first:
                first = False
            else:
                self._ledger.settle_end_of_day(day)  # T+1 开盘前解冻
            self._process_dividends(day)  # 除息日分红按开盘前持仓入账
            refresh_market_status(self._conn, self._config.as_dict(), day)

            ctx = StrategyContext(self._conn, self._ledger, day)
            for intent in strategy.on_bar(ctx):
                outcome = self._exec.submit_order(intent, day)
                if outcome.trade is not None:
                    # 内存 TradeLog 含派生字段 realized_pnl_cny（不落库，事后查表会丢）
                    result.trades.append(outcome.trade)

            for acc in track_accounts:
                cash = self._ledger.get_available_cash(acc)
                total = self._ledger.get_track_value(acc, day)
                result.equity_curve.append(
                    DailyEquity(
                        trade_date=day,
                        account_id=acc,
                        cash=cash,
                        market_value=total - cash,
                        total_value=total,
                    )
                )

        result.rejected = self._collect_intents(track_accounts, "REJECTED")
        result.unfilled = self._collect_intents(track_accounts, "PENDING")
        return result

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _all_accounts(self) -> list[str]:
        rows = self._conn.execute("SELECT account_id FROM account ORDER BY account_id").fetchall()
        return [r[0] for r in rows]

    def _trading_days(self, start: date, end: date) -> list[date]:
        """交易日序列：区间内行情并集（无独立日历表，数据驱动）。"""
        rows = self._conn.execute(
            """
            SELECT DISTINCT trade_date FROM fact_daily_bar
            WHERE trade_date BETWEEN ? AND ?
            ORDER BY trade_date
            """,
            [start, end],
        ).fetchall()
        return [r[0] for r in rows]

    def _process_dividends(self, day: date) -> None:
        """除息日为当日的现金分红入账；送股 v1 不支持（LedgerEngine 抛错，fail-fast）。"""
        rows = self._conn.execute(
            "SELECT code, dividend_per_share, dividend_type FROM fact_dividend WHERE ex_date = ?",
            [day],
        ).fetchall()
        for code, dps, dtype in rows:
            self._ledger.process_dividend(code, day, to_decimal(dps, Decimal("0")), dtype)

    def _collect_intents(self, accounts: list[str], status: str) -> list[RejectedIntent]:
        """按状态收集意图：REJECTED（规则拒绝）/ PENDING（未成交，原因记 rejection_reason）。"""
        placeholders = ", ".join("?" for _ in accounts)
        rows = self._conn.execute(
            f"""
            SELECT intent_id, CAST(submitted_at AS DATE), account_id, code, side, qty,
                   rejection_reason
            FROM order_intent
            WHERE account_id IN ({placeholders}) AND status = ?
            ORDER BY submitted_at, intent_id
            """,
            [*accounts, status],
        ).fetchall()
        return [
            RejectedIntent(
                intent_id=r[0],
                trade_date=r[1],
                account_id=r[2],
                code=r[3],
                side=r[4],
                qty=int(r[5]),
                rejection_reason=r[6] or "",
            )
            for r in rows
        ]
