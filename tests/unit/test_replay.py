"""P5.5 回放验证单测：合成历史决策 → diff 报告。"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import duckdb
import pytest

from viqtrader.engine.backtest.engine import default_rules
from viqtrader.engine.execution.context import build_rule_context
from viqtrader.engine.replay import (
    FIXED,
    MESSAGE_ONLY,
    REGRESSION,
    SAME,
    SKIPPED,
    RuleDiff,
    run_replay,
)
from viqtrader.engine.rules.base import TradingRule, TradingRuleEngine
from viqtrader.engine.types import (
    OrderIntent,
    OrderSide,
    OrderType,
    RuleCheckResult,
    RuleContext,
    RulePhase,
)

TRADE_DATE = date(2026, 8, 10)
PREV_DATE = date(2026, 8, 3)
CODE = "600519"
CODE2 = "000001"


def _seed_stock(conn: duckdb.DuckDBPyConnection, code: str) -> None:
    conn.execute(
        "INSERT INTO dim_stock (code, market, name, industry, board) "
        "VALUES (?, 'SH', ?, '白酒', 'MAIN')",
        [code, f"测试{code}"],
    )


def _seed_bar(conn: duckdb.DuckDBPyConnection, code: str, trade_date: date, px: float) -> None:
    conn.execute(
        """
        INSERT INTO fact_daily_bar
            (code, trade_date, open, high, low, close, volume, amount, adj_factor)
        VALUES (?, ?, ?, ?, ?, ?, 1000000, ?, 1.0)
        """,
        [code, trade_date, px, px, px, px, px * 1_000_000],
    )


def _seed_market_status(conn: duckdb.DuckDBPyConnection, code: str, trade_date: date) -> None:
    conn.execute(
        """
        INSERT INTO market_status
            (code, trade_date, board, is_suspended, is_st,
             up_limit_price, down_limit_price, is_hk_connect)
        VALUES (?, ?, 'MAIN', FALSE, FALSE, NULL, NULL, FALSE)
        """,
        [code, trade_date],
    )


def _seed_trade(
    conn: duckdb.DuckDBPyConnection, trade_id: str, code: str, side: str, qty: int, trade_date: date
) -> None:
    conn.execute(
        """
        INSERT INTO trade_log
            (trade_id, account_id, mode, code, side, qty, price, amount_cny, trade_date)
        VALUES (?, 'acc_value', 'value', ?, ?, ?, 10.0, ?, ?)
        """,
        [trade_id, code, side, qty, qty * 10.0, trade_date],
    )


def _seed_position(conn: duckdb.DuckDBPyConnection, code: str, qty: int, open_date: date) -> None:
    """历史 position 快照（回放侧不使用，仅用于让「当时决策」留痕通过校验）。"""
    conn.execute(
        """
        INSERT INTO position
            (position_id, account_id, mode, code, qty,
             avg_cost, avg_cost_cny, open_date, frozen_qty)
        VALUES (?, 'acc_value', 'value', ?, ?, 10.0, 10.0, ?, 0)
        """,
        [f"pos-{code}", code, qty, open_date],
    )


def _record_intent(
    conn: duckdb.DuckDBPyConnection,
    config: Any,
    *,
    intent_id: str,
    code: str,
    trade_date: date,
    side: str = "BUY",
    qty: int = 100,
    status: str = "FILLED",
) -> None:
    """按「当时引擎跑一遍」的方式写入 order_intent + rule_check_log，模拟历史留痕。"""
    intent = OrderIntent(
        intent_id=intent_id,
        account_id="acc_value",
        mode="value",
        code=code,
        market="SH",
        side=OrderSide(side),
        order_type=OrderType.MARKET,
        qty=qty,
        price=None,
        submitted_at=datetime(trade_date.year, trade_date.month, trade_date.day, 10, 0),
    )
    ctx = build_rule_context(conn, config, intent, trade_date, ledger=None)
    validation = TradingRuleEngine(default_rules()).validate(intent, ctx)

    conn.execute(
        """
        INSERT INTO order_intent
            (intent_id, account_id, mode, code, market, side, order_type, qty, price,
             stop_price, time_in_force, thesis_id, strategy_id, status, rejection_reason,
             submitted_at, config_hash, code_version)
        VALUES (?, 'acc_value', 'value', ?, 'SH', ?, 'MARKET', ?, NULL, NULL, 'GTC',
                NULL, NULL, ?, NULL, ?, 'hash', '0.1.0')
        """,
        [
            intent_id,
            code,
            side,
            qty,
            status,
            datetime(trade_date.year, trade_date.month, trade_date.day, 10, 0),
        ],
    )

    phase_by_rule = {r.rule_id: r.phase for r in default_rules()}
    for idx, r in enumerate(validation.results):
        conn.execute(
            """
            INSERT INTO rule_check_log
                (check_id, intent_id, phase, rule_type, passed, message, config_hash, code_version)
            VALUES (?, ?, ?, ?, ?, ?, 'hash', '0.1.0')
            """,
            [
                f"{intent_id}-{idx}",
                intent_id,
                str(phase_by_rule.get(r.rule_id, "")),
                r.rule_id,
                r.passed,
                r.message,
            ],
        )


class DenyRule(TradingRule):
    """测试用规则：拒绝指定标的（模拟引擎改动引入的回归）。"""

    rule_id = "TEST_DENY"
    phase = RulePhase.MARKET

    def check(self, intent: OrderIntent, *, ctx: RuleContext) -> RuleCheckResult:
        if intent.code == CODE:
            return RuleCheckResult(
                passed=False,
                rule_id=self.rule_id,
                message="测试规则：禁止买入 " + CODE,
                severity="BLOCK",
            )
        return RuleCheckResult(passed=True, rule_id=self.rule_id)


class TestReplay:
    def test_baseline_no_diff(self, conn: duckdb.DuckDBPyConnection, config: Any) -> None:
        """历史留痕即当前规则产物 → 全部 SAME。"""
        _seed_stock(conn, CODE)
        _seed_bar(conn, CODE, PREV_DATE, 10.0)
        _seed_bar(conn, CODE, TRADE_DATE, 10.0)
        _seed_market_status(conn, CODE, TRADE_DATE)
        _record_intent(conn, config, intent_id="i1", code=CODE, trade_date=TRADE_DATE)

        report = run_replay(conn, config, TRADE_DATE, TRADE_DATE)

        assert report.scanned == 1
        assert report.diffs[0].diff_type == SAME
        assert report.diffs[0].old_passed is True
        assert report.diffs[0].new_passed is True
        assert report.changed == 0

    def test_regression_detected(self, conn: duckdb.DuckDBPyConnection, config: Any) -> None:
        """新增/改动规则导致旧通过变新拒绝 → REGRESSION，且只命中受影响标的。"""
        for code in (CODE, CODE2):
            _seed_stock(conn, code)
            _seed_bar(conn, code, PREV_DATE, 10.0)
            _seed_bar(conn, code, TRADE_DATE, 10.0)
            _seed_market_status(conn, code, TRADE_DATE)
        _record_intent(conn, config, intent_id="i1", code=CODE, trade_date=TRADE_DATE)
        _record_intent(conn, config, intent_id="i2", code=CODE2, trade_date=TRADE_DATE)

        report = run_replay(
            conn, config, TRADE_DATE, TRADE_DATE, rules=[DenyRule(), *default_rules()]
        )

        by_id = {d.intent_id: d for d in report.diffs}
        assert by_id["i1"].diff_type == REGRESSION
        assert by_id["i1"].new_passed is False
        assert "测试规则" in by_id["i1"].new_reason
        assert by_id["i2"].diff_type == SAME
        assert report.regressions == 1
        # 规则级差异需精确定位到 TEST_DENY
        assert any(r.rule_id == "TEST_DENY" for r in by_id["i1"].rule_diffs)

    def test_fixed_detected(self, conn: duckdb.DuckDBPyConnection, config: Any) -> None:
        """旧拒绝 → 新通过 → FIXED。"""
        _seed_stock(conn, CODE)
        _seed_bar(conn, CODE, PREV_DATE, 10.0)
        _seed_bar(conn, CODE, TRADE_DATE, 10.0)
        _seed_market_status(conn, CODE, TRADE_DATE)
        _record_intent(conn, config, intent_id="i1", code=CODE, trade_date=TRADE_DATE)

        # 人为把历史留痕改成「现金不足」拒绝
        conn.execute(
            "UPDATE rule_check_log SET passed = FALSE, message = '现金不足' "
            "WHERE intent_id = 'i1' AND rule_type = 'CASH_SUFFICIENCY'"
        )
        conn.execute(
            "UPDATE rule_check_log SET passed = TRUE "
            "WHERE intent_id = 'i1' AND rule_type <> 'CASH_SUFFICIENCY'"
        )

        report = run_replay(conn, config, TRADE_DATE, TRADE_DATE)
        assert report.diffs[0].diff_type == FIXED
        assert report.fixed == 1

    def test_position_rebuilt_as_of(self, conn: duckdb.DuckDBPyConnection, config: Any) -> None:
        """持仓由 trade_log 重放：决策日之前买入的 200 股支撑 SELL 100。"""
        _seed_stock(conn, CODE)
        _seed_bar(conn, CODE, PREV_DATE, 10.0)
        _seed_bar(conn, CODE, TRADE_DATE, 10.0)
        _seed_market_status(conn, CODE, TRADE_DATE)
        _seed_trade(conn, "t0", CODE, "BUY", 200, PREV_DATE)
        _seed_position(conn, CODE, 200, PREV_DATE)
        _record_intent(
            conn, config, intent_id="i1", code=CODE, trade_date=TRADE_DATE, side="SELL", qty=100
        )

        report = run_replay(conn, config, TRADE_DATE, TRADE_DATE)
        assert report.diffs[0].diff_type == SAME
        assert report.diffs[0].new_passed is True

    def test_market_status_derived_when_snapshot_missing(
        self, conn: duckdb.DuckDBPyConnection, config: Any
    ) -> None:
        """缺当日 market_status 快照 → 按同一口径内存推导，不跳过整笔。"""
        _seed_stock(conn, CODE)
        _seed_bar(conn, CODE, PREV_DATE, 10.0)
        _seed_bar(conn, CODE, TRADE_DATE, 10.0)
        _seed_market_status(conn, CODE, TRADE_DATE)
        _record_intent(conn, config, intent_id="i1", code=CODE, trade_date=TRADE_DATE)
        # 模拟快照被清理：回放侧只能按同一口径内存推导
        conn.execute("DELETE FROM market_status")

        report = run_replay(conn, config, TRADE_DATE, TRADE_DATE)
        d = report.diffs[0]
        # 快照里涨跌停价为 NULL，推导侧会算出具体价格 → 允许消息级漂移
        assert d.diff_type in (SAME, MESSAGE_ONLY)
        assert d.new_passed is True
        assert any("market_status" in note for note in report.notes)

    def test_missing_rule_log_skipped(self, conn: duckdb.DuckDBPyConnection, config: Any) -> None:
        """无 rule_check_log 的意图（未经规则引擎）→ SKIPPED。"""
        _seed_stock(conn, CODE)
        _seed_bar(conn, CODE, TRADE_DATE, 10.0)
        _seed_market_status(conn, CODE, TRADE_DATE)
        conn.execute(
            """
            INSERT INTO order_intent
                (intent_id, account_id, mode, code, market, side, order_type, qty,
                 submitted_at, config_hash, code_version)
            VALUES ('i9', 'acc_value', 'value', ?, 'SH', 'BUY', 'MARKET', 100, ?, 'h', '0.1.0')
            """,
            [CODE, datetime(2026, 8, 10, 10, 0)],
        )

        report = run_replay(conn, config, TRADE_DATE, TRADE_DATE)
        assert report.diffs[0].diff_type == SKIPPED
        assert "rule_check_log" in report.diffs[0].skip_reason

    def test_empty_db_safe(self, conn: duckdb.DuckDBPyConnection, config: Any) -> None:
        report = run_replay(conn, config, TRADE_DATE, TRADE_DATE)
        assert report.scanned == 0
        assert report.diffs == ()
        assert report.notes

    def test_account_filter(self, conn: duckdb.DuckDBPyConnection, config: Any) -> None:
        _seed_stock(conn, CODE)
        _seed_bar(conn, CODE, TRADE_DATE, 10.0)
        _seed_market_status(conn, CODE, TRADE_DATE)
        _record_intent(conn, config, intent_id="i1", code=CODE, trade_date=TRADE_DATE)

        assert run_replay(conn, config, TRADE_DATE, TRADE_DATE, account_id="acc_value").scanned == 1
        assert run_replay(conn, config, TRADE_DATE, TRADE_DATE, account_id="acc_other").scanned == 0

    def test_invalid_range(self, conn: duckdb.DuckDBPyConnection, config: Any) -> None:
        with pytest.raises(ValueError):
            run_replay(conn, config, TRADE_DATE, PREV_DATE)

    def test_to_dict_serializable(self, conn: duckdb.DuckDBPyConnection, config: Any) -> None:
        _seed_stock(conn, CODE)
        _seed_bar(conn, CODE, TRADE_DATE, 10.0)
        _seed_market_status(conn, CODE, TRADE_DATE)
        _record_intent(conn, config, intent_id="i1", code=CODE, trade_date=TRADE_DATE)

        payload = run_replay(conn, config, TRADE_DATE, TRADE_DATE).to_dict()
        text = json.dumps(payload, ensure_ascii=False)
        assert payload["summary"]["same"] == 1
        assert "REGRESSION" not in text or payload["summary"]["regression"] == 0


class TestRuleDiffShape:
    def test_rule_diff_is_frozen(self) -> None:
        r = RuleDiff("X", True, False, "a", "b")
        assert r.rule_id == "X"
        with pytest.raises(AttributeError):
            r.rule_id = "Y"  # type: ignore[misc]


def test_decimal_nan_guard() -> None:
    """Decimal('NaN') 的比较保护（close 缺失时不应污染市值）。"""
    from viqtrader.engine.replay import _is_nan

    assert _is_nan(Decimal("NaN")) is True
    assert _is_nan(Decimal("1.5")) is False
