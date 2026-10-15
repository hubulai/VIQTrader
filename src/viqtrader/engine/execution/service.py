"""P2 执行服务：Intent → 三阶段校验 → 网关撮合 → 账本记账 的编排入口。

任何订单意图的完整生命周期在此留痕：
1. order_intent 落库（PENDING）
2. 三阶段规则校验，全部结果写 rule_check_log
3. REJECTED → 更新状态 + rejection_reason，终止
4. WARN（软限制）→ 写 risk_event（severity=WARN, decision=ALERT），继续
5. 网关撮合；未成交保持 PENDING（挂单）
6. FILLED → LedgerEngine.execute_fill（扣款前重检），更新意图状态
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import duckdb

from viqtrader import __version__
from viqtrader.engine.execution.context import build_rule_context
from viqtrader.engine.execution.gateway import ExecutionGateway
from viqtrader.engine.fees import estimate_fees
from viqtrader.engine.ledger.engine import LedgerEngine
from viqtrader.engine.rules.base import TradingRuleEngine
from viqtrader.engine.types import (
    FillStatus,
    InsufficientCashAtFill,
    IntentStatus,
    OrderIntent,
    RuleCheckResult,
    TradeLog,
)


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """submit_order 的结果。"""

    intent_id: str
    status: IntentStatus  # PENDING / REJECTED / FILLED
    rejection_reason: str = ""
    trade: TradeLog | None = None
    warnings: tuple[RuleCheckResult, ...] = ()


class ExecutionService:
    """执行编排入口。回测/模拟盘共用，仅网关不同。"""

    def __init__(
        self,
        conn: duckdb.DuckDBPyConnection,
        config: Any,
        rules: list[Any],
        gateway: ExecutionGateway,
    ) -> None:
        self._conn = conn
        self._config = config
        self._rules = list(rules)
        self._rule_engine = TradingRuleEngine(rules)
        self._gateway = gateway
        self._ledger = LedgerEngine(conn, config)
        self._config_hash = str(config.content_hash())
        self._code_version = __version__

    def submit_order(self, intent: OrderIntent, trade_date: date) -> ExecutionResult:
        """提交订单意图，走完整校验-撮合-记账管线。"""
        ctx = build_rule_context(self._conn, self._config, intent, trade_date, ledger=self._ledger)
        self._insert_intent(intent)
        validation = self._rule_engine.validate(intent, ctx)
        self._log_rule_checks(intent.intent_id, validation.results)
        for warn in validation.warnings:
            self._log_risk_event(intent, warn)

        if not validation.passed:
            self._update_intent_status(
                intent.intent_id,
                IntentStatus.REJECTED,
                rejection_reason=validation.rejection_reason,
            )
            return ExecutionResult(
                intent_id=intent.intent_id,
                status=IntentStatus.REJECTED,
                rejection_reason=validation.rejection_reason,
                warnings=validation.warnings,
            )

        fill = self._gateway.fill(intent, trade_date)
        if fill.status != FillStatus.FILLED or fill.fill_price is None:
            # 未成交：保持 PENDING（挂单语义），原因记录在 rejection_reason 字段以供查询
            self._update_intent_status(
                intent.intent_id,
                IntentStatus.PENDING,
                rejection_reason=f"NOT_FILLED: {fill.message}",
            )
            return ExecutionResult(
                intent_id=intent.intent_id,
                status=IntentStatus.PENDING,
                rejection_reason=fill.message,
                warnings=validation.warnings,
            )

        fees = estimate_fees(
            ctx.config, intent.market, intent.side, intent.qty, fill.fill_price, ctx.fx_rate
        )
        try:
            trade = self._ledger.execute_fill(intent, fill.fill_price, fees, trade_date)
        except InsufficientCashAtFill as exc:
            reason = f"INSUFFICIENT_CASH_AT_FILL: {exc}"
            self._update_intent_status(
                intent.intent_id, IntentStatus.REJECTED, rejection_reason=reason
            )
            return ExecutionResult(
                intent_id=intent.intent_id,
                status=IntentStatus.REJECTED,
                rejection_reason=reason,
                warnings=validation.warnings,
            )

        self._update_intent_status(
            intent.intent_id,
            IntentStatus.FILLED,
            filled_price=fill.fill_price,
        )
        return ExecutionResult(
            intent_id=intent.intent_id,
            status=IntentStatus.FILLED,
            trade=trade,
            warnings=validation.warnings,
        )

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def _insert_intent(self, intent: OrderIntent) -> None:
        self._conn.execute(
            """
            INSERT INTO order_intent
                (intent_id, account_id, mode, code, market, side, order_type,
                 qty, price, stop_price, time_in_force, status, thesis_id,
                 strategy_id, submitted_at, config_hash, code_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                intent.intent_id,
                intent.account_id,
                intent.mode,
                intent.code,
                intent.market,
                str(intent.side),
                str(intent.order_type),
                intent.qty,
                float(intent.price) if intent.price is not None else None,
                float(intent.stop_price) if intent.stop_price is not None else None,
                str(intent.time_in_force),
                str(IntentStatus.PENDING),
                intent.thesis_id,
                intent.strategy_id,
                intent.submitted_at,
                self._config_hash,
                self._code_version,
            ],
        )

    def _update_intent_status(
        self,
        intent_id: str,
        status: IntentStatus,
        *,
        rejection_reason: str | None = None,
        filled_price: Decimal | None = None,
    ) -> None:
        self._conn.execute(
            """
            UPDATE order_intent
            SET status = ?, rejection_reason = ?,
                filled_at = CASE WHEN ? = 'FILLED' THEN now() ELSE filled_at END,
                filled_price = COALESCE(?, filled_price)
            WHERE intent_id = ?
            """,
            [
                str(status),
                rejection_reason,
                str(status),
                float(filled_price) if filled_price is not None else None,
                intent_id,
            ],
        )

    def _log_rule_checks(self, intent_id: str, results: tuple[RuleCheckResult, ...]) -> None:
        rows = []
        for r in results:
            rows.append(
                (
                    str(uuid.uuid4()),
                    intent_id,
                    self._phase_of(r.rule_id),
                    r.rule_id,
                    r.passed,
                    r.message,
                    self._config_hash,
                    self._code_version,
                )
            )
        if rows:
            self._conn.executemany(
                """
                INSERT INTO rule_check_log
                    (check_id, intent_id, phase, rule_type, passed, message,
                     config_hash, code_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def _log_risk_event(self, intent: OrderIntent, warn: RuleCheckResult) -> None:
        snapshot = json.dumps(asdict(intent), ensure_ascii=False, default=str)
        self._conn.execute(
            """
            INSERT INTO risk_event
                (event_id, account_id, mode, rule_key, code, severity, decision,
                 intent_snapshot, message, config_hash, code_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                str(uuid.uuid4()),
                intent.account_id,
                intent.mode,
                warn.rule_id,
                intent.code,
                "WARN",
                "ALERT",
                snapshot,
                warn.message,
                self._config_hash,
                self._code_version,
            ],
        )

    def _phase_of(self, rule_id: str) -> str:
        for rule in self._rules:
            if rule.rule_id == rule_id:
                return str(rule.phase)
        return "UNKNOWN"
