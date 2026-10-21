"""P5.5 回放验证：历史决策在当前规则链下的 diff 报告（设计文档 12.1）。

用途：引擎/规则/配置改动后，重放窗口内真实发生过的全部订单意图，
diff「历史规则判定（rule_check_log 留痕）」与「当前代码重跑结果」，
人工审阅后再上线。比断言测试覆盖更全——回放覆盖的是真实场景。

纪律：
- **只读**：仅调用 TradingRuleEngine.validate()，不走网关、不记账、不写库
- **前视防控**：规则上下文按决策日 as-of 重建——持仓由 trade_log 重放
  （trade_date < 决策日）、现金 = 配额 + Σ cash_ledger（trade_date < 决策日）、
  市值取 as-of 最近收盘 x 汇率；绝不用 position/cash_ledger 的当前值
- **可解释**：diff 精确到规则级（rule_id 级翻转 + 消息漂移），并显式列出
  重建口径局限（frozen_qty 无历史留痕、rule_check_log 未存 severity）
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

import duckdb

from viqtrader.engine.execution.context import get_fx_rate, get_latest_close
from viqtrader.engine.market_status import derive_market_status, get_market_status
from viqtrader.engine.rules.base import TradingRule, TradingRuleEngine
from viqtrader.engine.types import (
    MarketStatusView,
    OrderIntent,
    OrderSide,
    OrderType,
    PositionView,
    RuleCheckResult,
    RuleContext,
    TimeInForce,
    to_decimal,
)

logger = logging.getLogger(__name__)

# diff 分类
SAME = "SAME"
REGRESSION = "REGRESSION"  # 旧通过 → 新拒绝（必须人工审阅）
FIXED = "FIXED"  # 旧拒绝 → 新通过
SHIFTED_REJECTION = "SHIFTED_REJECTION"  # 两侧都拒绝，但首个拒绝规则不同
MESSAGE_ONLY = "MESSAGE_ONLY"  # 结论一致，规则消息漂移
SKIPPED = "SKIPPED"  # 无法重建/未留痕

_D0 = Decimal("0")

CONTEXT_LIMITATIONS: tuple[str, ...] = (
    "frozen_qty 无历史留痕：重放按 0 处理（日终视角），当日买入即卖出的 T+1 场景可能偏乐观",
    "rule_check_log 未存 severity：QuotaRule 等 WARN 级软限制的告警变化不在 diff 范围",
    "规则链与配置取当前版本（default.yaml + user.yaml），非决策时快照；这正是要观测的变量",
)


@dataclass(frozen=True, slots=True)
class RuleDiff:
    """单条规则的判定差异。None 表示该侧未执行到该规则（管线提前终止）。"""

    rule_id: str
    old_passed: bool | None
    new_passed: bool | None
    old_message: str = ""
    new_message: str = ""


@dataclass(frozen=True, slots=True)
class IntentDiff:
    """单笔订单意图的回放结果。"""

    intent_id: str
    account_id: str
    code: str
    side: str
    trade_date: date
    old_status: str
    old_passed: bool
    old_reason: str
    new_passed: bool
    new_reason: str
    diff_type: str
    rule_diffs: tuple[RuleDiff, ...] = ()
    skip_reason: str = ""


@dataclass(frozen=True, slots=True)
class ReplayReport:
    """一次回放验证的汇总。"""

    start: date
    end: date
    scanned: int
    diffs: tuple[IntentDiff, ...]
    notes: tuple[str, ...] = ()

    def _count(self, diff_type: str) -> int:
        return sum(1 for d in self.diffs if d.diff_type == diff_type)

    @property
    def regressions(self) -> int:
        return self._count(REGRESSION)

    @property
    def fixed(self) -> int:
        return self._count(FIXED)

    @property
    def shifted(self) -> int:
        return self._count(SHIFTED_REJECTION)

    @property
    def message_only(self) -> int:
        return self._count(MESSAGE_ONLY)

    @property
    def same(self) -> int:
        return self._count(SAME)

    @property
    def skipped(self) -> int:
        return self._count(SKIPPED)

    @property
    def changed(self) -> int:
        """结论发生变化的笔数（不含消息漂移与跳过）。"""
        return self.regressions + self.fixed + self.shifted

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "scanned": self.scanned,
            "summary": {
                "regression": self.regressions,
                "fixed": self.fixed,
                "shifted_rejection": self.shifted,
                "message_only": self.message_only,
                "same": self.same,
                "skipped": self.skipped,
                "changed": self.changed,
            },
            "diffs": [
                {
                    "intent_id": d.intent_id,
                    "account_id": d.account_id,
                    "code": d.code,
                    "side": d.side,
                    "trade_date": d.trade_date.isoformat(),
                    "old_status": d.old_status,
                    "old_passed": d.old_passed,
                    "old_reason": d.old_reason,
                    "new_passed": d.new_passed,
                    "new_reason": d.new_reason,
                    "diff_type": d.diff_type,
                    "skip_reason": d.skip_reason,
                    "rule_diffs": [
                        {
                            "rule_id": r.rule_id,
                            "old_passed": r.old_passed,
                            "new_passed": r.new_passed,
                            "old_message": r.old_message,
                            "new_message": r.new_message,
                        }
                        for r in d.rule_diffs
                    ],
                }
                for d in self.diffs
            ],
            "notes": list(self.notes),
        }


def run_replay(
    conn: duckdb.DuckDBPyConnection,
    config: Any,
    start: date,
    end: date,
    *,
    account_id: str | None = None,
    rules: Sequence[TradingRule] | None = None,
) -> ReplayReport:
    """重放 [start, end] 内全部订单意图，返回新旧规则判定 diff 报告。

    Args:
        rules: 规则链；None 时使用引擎正式规则链 default_rules()。

    Raises:
        ValueError: start > end。
    """
    if start > end:
        raise ValueError(f"回放区间非法：start={start} > end={end}")

    rule_list: list[TradingRule] = list(rules) if rules is not None else _default_rules()
    engine = TradingRuleEngine(rule_list)

    try:
        rows = _load_intents(conn, start, end, account_id)
    except duckdb.Error as exc:  # 空库/缺表安全降级
        logger.warning("回放读取 order_intent 失败：%s", exc)
        return ReplayReport(
            start=start,
            end=end,
            scanned=0,
            diffs=(),
            notes=(f"读取 order_intent 失败（空库或缺表）：{exc}",),
        )

    diffs: list[IntentDiff] = []
    derived = 0
    for row in rows:
        if get_market_status(conn, str(row[3]), row[15]) is None:
            derived += 1
        diffs.append(_replay_one(conn, config, engine, row))

    notes: list[str] = list(CONTEXT_LIMITATIONS)
    if derived:
        notes.append(
            f"{derived} 笔缺 market_status 当日快照，已按同一口径内存推导"
            "（当日无 bar 会被判为停牌，可能显示为 REGRESSION）"
        )
    non_rule_rejects = sum(
        1 for d in diffs if d.old_status == "REJECTED" and d.old_passed and d.diff_type != SKIPPED
    )
    if non_rule_rejects:
        notes.append(
            f"{non_rule_rejects} 笔旧状态 REJECTED 但规则全通过"
            "（成交期资金不足/未成交等非规则拒绝）"
        )
    if not rows:
        notes.append("窗口内无可回放订单意图（order_intent 为空）")

    return ReplayReport(
        start=start, end=end, scanned=len(rows), diffs=tuple(diffs), notes=tuple(notes)
    )


# ---------------------------------------------------------------------------
# 单笔回放
# ---------------------------------------------------------------------------


def _replay_one(
    conn: duckdb.DuckDBPyConnection,
    config: Any,
    engine: TradingRuleEngine,
    row: tuple[Any, ...],
) -> IntentDiff:
    (
        intent_id,
        account_id,
        mode,
        code,
        market,
        side,
        order_type,
        qty,
        price,
        stop_price,
        time_in_force,
        thesis_id,
        strategy_id,
        status,
        rejection_reason,
        trade_date,
    ) = row

    base = IntentDiff(
        intent_id=str(intent_id),
        account_id=str(account_id),
        code=str(code),
        side=str(side),
        trade_date=trade_date,
        old_status=str(status),
        old_passed=False,
        old_reason=str(rejection_reason or ""),
        new_passed=False,
        new_reason="",
        diff_type=SKIPPED,
    )

    old_rows = _load_rule_log(conn, str(intent_id))
    if not old_rows:
        return _replace(
            base,
            skip_reason="无 rule_check_log 记录（该意图未走过规则引擎）",
        )

    ms = get_market_status(conn, str(code), trade_date)
    if ms is None:
        # 快照缺失：按与 refresh_market_status 同一口径只读推导（当日无 bar 视为停牌）
        ms = derive_market_status(conn, config.as_dict(), str(code), trade_date)

    ctx = _build_context(conn, config, str(account_id), str(code), trade_date, ms)

    intent = OrderIntent(
        intent_id=str(intent_id),
        account_id=str(account_id),
        mode=str(mode),
        code=str(code),
        market=str(market),
        side=OrderSide(str(side)),
        order_type=OrderType(str(order_type)),
        qty=int(qty),
        price=to_decimal(price),
        stop_price=to_decimal(stop_price),
        time_in_force=TimeInForce(str(time_in_force)),
        thesis_id=str(thesis_id) if thesis_id is not None else None,
        strategy_id=str(strategy_id) if strategy_id is not None else None,
        submitted_at=datetime.combine(trade_date, time(9, 31)),
    )
    validation = engine.validate(intent, ctx)

    old_map = {str(r[0]): (bool(r[1]), str(r[2] or "")) for r in old_rows}
    old_passed = all(passed for passed, _msg in old_map.values())
    old_first_fail = _first_fail(old_rows, passed_idx=1, rule_idx=0)

    new_map = {r.rule_id: (r.passed, r.message) for r in validation.results}
    new_first_fail = _first_fail_new(validation.results)

    rule_diffs = _rule_diffs(old_map, new_map, old_passed, validation.passed, new_first_fail)

    diff_type = _classify(
        old_passed=old_passed,
        new_passed=validation.passed,
        old_first_fail=old_first_fail,
        new_first_fail=new_first_fail,
        rule_diffs=rule_diffs,
    )

    return IntentDiff(
        intent_id=base.intent_id,
        account_id=base.account_id,
        code=base.code,
        side=base.side,
        trade_date=base.trade_date,
        old_status=base.old_status,
        old_passed=old_passed,
        old_reason=base.old_reason,
        new_passed=validation.passed,
        new_reason=validation.rejection_reason,
        diff_type=diff_type,
        rule_diffs=rule_diffs,
    )


def _replace(base: IntentDiff, *, skip_reason: str) -> IntentDiff:
    return IntentDiff(
        intent_id=base.intent_id,
        account_id=base.account_id,
        code=base.code,
        side=base.side,
        trade_date=base.trade_date,
        old_status=base.old_status,
        old_passed=base.old_passed,
        old_reason=base.old_reason,
        new_passed=base.new_passed,
        new_reason=base.new_reason,
        diff_type=SKIPPED,
        skip_reason=skip_reason,
    )


def _rule_diffs(
    old_map: dict[str, tuple[bool, str]],
    new_map: dict[str, tuple[bool, str]],
    old_passed: bool,
    new_passed: bool,
    new_first_fail: str | None,
) -> tuple[RuleDiff, ...]:
    """只输出有意义的差异：两侧都执行到的规则翻转/消息漂移 + 新侧首个拒绝规则。

    旧侧因管线提前终止而缺失的规则不列出（否则每笔拒绝都会刷出一串噪声）。
    """
    out: list[RuleDiff] = []
    for rule_id, (new_ok, new_msg) in new_map.items():
        old = old_map.get(rule_id)
        if old is None:
            if not new_ok and rule_id == new_first_fail:
                out.append(RuleDiff(rule_id, None, False, "", new_msg))
            continue
        old_ok, old_msg = old
        if old_ok != new_ok or old_msg.strip() != new_msg.strip():
            out.append(RuleDiff(rule_id, old_ok, new_ok, old_msg, new_msg))

    # 双侧结论一致但旧侧首拒绝规则已不在新侧（新侧更早拒绝）时，补一条便于定位
    if (
        not old_passed
        and not new_passed
        and new_first_fail
        and new_first_fail not in old_map
        and all(r.rule_id != new_first_fail for r in out)
    ):
        out.append(RuleDiff(new_first_fail, None, False, "", new_map[new_first_fail][1]))
    return tuple(out)


def _classify(
    *,
    old_passed: bool,
    new_passed: bool,
    old_first_fail: str | None,
    new_first_fail: str | None,
    rule_diffs: tuple[RuleDiff, ...],
) -> str:
    if old_passed and not new_passed:
        return REGRESSION
    if not old_passed and new_passed:
        return FIXED
    if not old_passed and not new_passed:
        if old_first_fail != new_first_fail:
            return SHIFTED_REJECTION
        return MESSAGE_ONLY if rule_diffs else SAME
    return MESSAGE_ONLY if rule_diffs else SAME


def _first_fail(rows: list[tuple[Any, ...]], *, passed_idx: int, rule_idx: int) -> str | None:
    for r in rows:
        if not bool(r[passed_idx]):
            return str(r[rule_idx])
    return None


def _first_fail_new(results: tuple[RuleCheckResult, ...]) -> str | None:
    for r in results:
        if not r.passed and r.severity != "WARN":
            return r.rule_id
    return None


# ---------------------------------------------------------------------------
# 历史上下文重建（as-of，前视安全）
# ---------------------------------------------------------------------------


def _build_context(
    conn: duckdb.DuckDBPyConnection,
    config: Any,
    account_id: str,
    code: str,
    trade_date: date,
    ms: MarketStatusView,
) -> RuleContext:
    cfg = config.as_dict()
    fx_pair = str(cfg.get("data", {}).get("fx_pair", "HKD/CNY"))
    fx = get_fx_rate(conn, code, trade_date, fx_pair)
    account_mode = _account_mode(conn, account_id)

    qty = _holdings_as_of(conn, account_id, code, trade_date)
    position = (
        PositionView(
            account_id=account_id,
            mode=account_mode,
            code=code,
            qty=qty,
            frozen_qty=0,
            avg_cost=_D0,
            avg_cost_cny=_D0,
        )
        if qty > 0
        else None
    )

    cash = _cash_as_of(conn, account_id, trade_date)
    market_value = _market_value_as_of(conn, account_id, trade_date)

    total = _D0
    for acc_id, _mode, _ratio in conn.execute(
        "SELECT account_id, mode, target_ratio FROM account"
    ).fetchall():
        total += _cash_as_of(conn, str(acc_id), trade_date)
        total += _market_value_as_of(conn, str(acc_id), trade_date)

    current_price = _open_on(conn, code, trade_date)
    if current_price is None:
        current_price = get_latest_close(conn, code, trade_date)

    target_ratio = _account_target_ratio(conn, account_id)

    return RuleContext(
        trade_date=trade_date,
        market_status=ms,
        position=position,
        track_cash=cash,
        track_market_value=market_value,
        total_portfolio_value=total,
        config=cfg,
        is_trading_day=_is_trading_day(conn, trade_date),
        current_price=current_price,
        account_mode=account_mode,
        target_ratio=target_ratio,
        fx_rate=fx,
    )


def _cash_as_of(conn: duckdb.DuckDBPyConnection, account_id: str, as_of: date) -> Decimal:
    """配额 + 决策日之前的现金流水（与 LedgerEngine/_loader 期初现金同口径）。"""
    quota_row = conn.execute(
        "SELECT capital_quota FROM account WHERE account_id = ?", [account_id]
    ).fetchone()
    quota = to_decimal(quota_row[0], _D0) if quota_row else _D0
    flow_row = conn.execute(
        "SELECT COALESCE(SUM(amount_cny), 0) FROM cash_ledger "
        "WHERE account_id = ? AND trade_date < ?",
        [account_id, as_of],
    ).fetchone()
    return quota + (to_decimal(flow_row[0], _D0) if flow_row else _D0)


def _holdings_as_of(
    conn: duckdb.DuckDBPyConnection, account_id: str, code: str, as_of: date
) -> int:
    """trade_date < as_of 的成交重放得到该标的净持仓。"""
    rows = conn.execute(
        """
        SELECT side, qty FROM trade_log
        WHERE account_id = ? AND code = ? AND trade_date < ?
        ORDER BY trade_date, trade_id
        """,
        [account_id, code, as_of],
    ).fetchall()
    net = 0
    for side, qty in rows:
        net += int(qty) if str(side) == "BUY" else -int(qty)
    return max(net, 0)


def _market_value_as_of(conn: duckdb.DuckDBPyConnection, account_id: str, as_of: date) -> Decimal:
    """账户 as-of 持仓市值（CNY）：Σ qty x 最近收盘 x 汇率。"""
    rows = conn.execute(
        """
        SELECT code,
               SUM(CASE WHEN side = 'BUY' THEN qty ELSE -qty END) AS net_qty
        FROM trade_log
        WHERE account_id = ? AND trade_date < ?
        GROUP BY code
        """,
        [account_id, as_of],
    ).fetchall()
    total = _D0
    for item_code, net_qty in rows:
        qty = int(net_qty or 0)
        if qty <= 0:
            continue
        close = get_latest_close(conn, str(item_code), as_of)
        if close is None or _is_nan(close):
            continue
        fx = get_fx_rate(conn, str(item_code), as_of)
        total += close * qty * fx
    return total


def _open_on(conn: duckdb.DuckDBPyConnection, code: str, trade_date: date) -> Decimal | None:
    row = conn.execute(
        "SELECT open FROM fact_daily_bar WHERE code = ? AND trade_date = ?",
        [code, trade_date],
    ).fetchone()
    if row is None or row[0] is None:
        return None
    value = to_decimal(row[0])
    if value is None or _is_nan(value):
        return None
    return value


def _is_trading_day(conn: duckdb.DuckDBPyConnection, trade_date: date) -> bool:
    row = conn.execute(
        "SELECT count(*) FROM fact_daily_bar WHERE trade_date = ?", [trade_date]
    ).fetchone()
    return bool(row and int(row[0]) > 0)


def _account_mode(conn: duckdb.DuckDBPyConnection, account_id: str) -> str:
    row = conn.execute("SELECT mode FROM account WHERE account_id = ?", [account_id]).fetchone()
    return str(row[0]) if row else ""


def _account_target_ratio(conn: duckdb.DuckDBPyConnection, account_id: str) -> Decimal:
    row = conn.execute(
        "SELECT target_ratio FROM account WHERE account_id = ?", [account_id]
    ).fetchone()
    return to_decimal(row[0], _D0) if row else _D0


def _is_nan(value: Decimal) -> bool:
    """Decimal NaN 过滤（to_decimal(float('nan')) 会得到 Decimal('NaN')）。"""
    return isinstance(value, Decimal) and value.is_nan()


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------


def _load_intents(
    conn: duckdb.DuckDBPyConnection, start: date, end: date, account_id: str | None
) -> list[tuple[Any, ...]]:
    where = "CAST(submitted_at AS DATE) BETWEEN ? AND ?"
    args: list[Any] = [start, end]
    if account_id:
        where += " AND account_id = ?"
        args.append(account_id)
    return conn.execute(
        f"""
        SELECT intent_id, account_id, mode, code, market, side, order_type,
               qty, price, stop_price, time_in_force, thesis_id, strategy_id,
               status, rejection_reason, CAST(submitted_at AS DATE) AS trade_date
        FROM order_intent
        WHERE {where}
        ORDER BY submitted_at, intent_id
        """,
        args,
    ).fetchall()


def _load_rule_log(conn: duckdb.DuckDBPyConnection, intent_id: str) -> list[tuple[Any, ...]]:
    return conn.execute(
        """
        SELECT rule_type, passed, message FROM rule_check_log
        WHERE intent_id = ?
        ORDER BY checked_at, check_id
        """,
        [intent_id],
    ).fetchall()


def _default_rules() -> list[TradingRule]:
    from viqtrader.engine.backtest.engine import default_rules

    return list(default_rules())


__all__ = [
    "FIXED",
    "MESSAGE_ONLY",
    "REGRESSION",
    "SAME",
    "SHIFTED_REJECTION",
    "SKIPPED",
    "IntentDiff",
    "ReplayReport",
    "RuleDiff",
    "run_replay",
]
