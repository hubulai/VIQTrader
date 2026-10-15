"""P3.1 回测结果与通用指标（价值/波段专属指标分别在 3.3/3.4）。

指标语义：
- 净值序列按账户逐日记录（现金 + 持仓市值折 CNY），可合并为组合总值；
- 年化按 252 个交易日；Sharpe 无风险利率取 0（v1 简化）；
- 指标计算内部用 float（展示用途），资金/持仓全程 Decimal 可回放。
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from viqtrader.engine.types import TradeLog

PRECISION_NOTE = (
    "精度受限：财报按披露日（ann_date）近似可见；港股分红按除息日到账近似；"
    "港股通名单历史变动无流水源，is_hk_connect 按当前名单近似"
)


@dataclass(frozen=True, slots=True)
class DailyEquity:
    """单账户逐日净值快照（均 CNY 口径）。"""

    trade_date: date
    account_id: str
    cash: Decimal
    market_value: Decimal
    total_value: Decimal


@dataclass(frozen=True, slots=True)
class RejectedIntent:
    """回测中被规则管线拒绝的意图（审计入口）。"""

    intent_id: str
    trade_date: date
    account_id: str
    code: str
    side: str
    qty: int
    rejection_reason: str


@dataclass(slots=True)
class BacktestResult:
    """回测输出：净值曲线 + 成交记录 + 被拒意图 + 通用指标。"""

    start: date
    end: date
    accounts: list[str]
    equity_curve: list[DailyEquity] = field(default_factory=list)
    trades: list[TradeLog] = field(default_factory=list)
    rejected: list[RejectedIntent] = field(default_factory=list)
    unfilled: list[RejectedIntent] = field(default_factory=list)
    precision_note: str = PRECISION_NOTE

    # ------------------------------------------------------------------
    # 净值序列
    # ------------------------------------------------------------------

    def series(self, account_id: str | None = None) -> list[tuple[date, Decimal]]:
        """逐日总净值序列；account_id=None 时全组合合计。"""
        if account_id is not None:
            return [
                (e.trade_date, e.total_value)
                for e in self.equity_curve
                if e.account_id == account_id
            ]
        by_day: dict[date, Decimal] = {}
        for e in self.equity_curve:
            by_day[e.trade_date] = by_day.get(e.trade_date, Decimal("0")) + e.total_value
        return sorted(by_day.items())

    # ------------------------------------------------------------------
    # 通用指标
    # ------------------------------------------------------------------

    def metrics(self, account_id: str | None = None) -> dict[str, float | int | None]:
        """累计收益 / 年化 / 最大回撤 / Sharpe / 交易日数。

        净值不足 2 个点时收益类指标为 None（无法计算）。
        """
        pts = self.series(account_id)
        n = len(pts)
        if n < 2:
            return {
                "trading_days": n,
                "total_return": None,
                "annualized_return": None,
                "max_drawdown": None,
                "sharpe": None,
            }
        values = [float(v) for _, v in pts]
        first, last = values[0], values[-1]
        total_return = last / first - 1.0 if first > 0 else 0.0
        annualized = (1.0 + total_return) ** (252.0 / (n - 1)) - 1.0 if total_return > -1 else -1.0

        peak = values[0]
        max_dd = 0.0
        for v in values:
            peak = max(peak, v)
            if peak > 0:
                max_dd = min(max_dd, v / peak - 1.0)

        daily_rets = [values[i] / values[i - 1] - 1.0 for i in range(1, n) if values[i - 1] > 0]
        sharpe: float | None = None
        if len(daily_rets) >= 2:
            sd = statistics.stdev(daily_rets)
            if sd > 0:
                sharpe = statistics.mean(daily_rets) / sd * math.sqrt(252.0)

        return {
            "trading_days": n,
            "total_return": total_return,
            "annualized_return": annualized,
            "max_drawdown": max_dd,
            "sharpe": sharpe,
        }
