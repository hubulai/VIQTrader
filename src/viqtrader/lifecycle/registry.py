"""P3.6 波段轨策略生命周期状态机（设计文档 5.3，KPI 驱动自动迁移 + 资金配额联动）。

阶段：INCUBATING → PAPER → PROBATION → PRODUCTION，任一阶段可退到 RETIRED。
判定优先级（每日 evaluate 一次，单步迁移，逐级/逐次评估不跳档）：
1. 立即退役：最近单日收益 ≤ retire_daily_loss（默认 -5%，2026-10-16 裁决）
2. 低迷退役：滚动 30 日 Sharpe < retire_sharpe 持续 retire_duration_days 个自然日
3. 降级：PRODUCTION 跌破保持线（滚动 30 日 Sharpe < production_min_sharpe
   或滚动回撤 < production_max_drawdown）→ 回 PROBATION
4. 晋级：INCUBATING（Walk-Forward 门禁通过，由编排层注入 wf_gate_passed）
   / PAPER（满 paper_min_days 且累计收益 > paper_min_return 且回撤 > paper_max_drawdown）
   / PROBATION（滚动 30 日 Sharpe > probation_to_production_min_sharpe）

资金配额联动（记录 + 联动更新，不做下单硬阻断，与 80/20 软限制同风格）：
INCUBATING/PAPER = 0，PROBATION = probation_capital_pct，
PRODUCTION = production_capital_pct，RETIRED = 0（清仓走退役流程）。

退役清仓：对该策略在波段子账本下的全部持仓生成 MARKET 卖出 Intent（只产 Intent，
走正常 ExecutionService 规则管线）；当日买入受 T+1 冻结的部分拆到下一交易日卖出。

价值轨纪律：track != 'swing' 拒绝注册（价值轨走 thesis 证伪，不适用此生命周期）。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time
from enum import StrEnum
from typing import Any

import duckdb

from viqtrader import __version__
from viqtrader.engine.types import OrderIntent, OrderSide, OrderType, TimeInForce
from viqtrader.lifecycle.equity import replay_strategy_equity, strategy_base_cash
from viqtrader.lifecycle.kpi import compute_kpi_snapshot


class Stage(StrEnum):
    INCUBATING = "INCUBATING"
    PAPER = "PAPER"
    PROBATION = "PROBATION"
    PRODUCTION = "PRODUCTION"
    RETIRED = "RETIRED"


# 各阶段资金配额配置键（INCUBATING/PAPER/RETIRED 恒 0）
_STAGE_CAPITAL_KEYS: dict[Stage, str | None] = {
    Stage.INCUBATING: None,
    Stage.PAPER: None,
    Stage.PROBATION: "probation_capital_pct",
    Stage.PRODUCTION: "production_capital_pct",
    Stage.RETIRED: None,
}


@dataclass(frozen=True, slots=True)
class LifecycleEvaluation:
    """evaluate 的结果：目标阶段 + 判定依据（不落库，由 apply 统一持久化）。"""

    strategy_id: str
    current_stage: Stage
    target_stage: Stage
    reason: str
    kpi: dict[str, Any]
    as_of: date  # 评估业务日期（apply 用它落 entered_at/history.decided_at，回测可回放）
    liquidation_intents: tuple[OrderIntent, ...] = ()  # 仅 RETIRED 时非空

    @property
    def changed(self) -> bool:
        return self.target_stage != self.current_stage


class LifecycleEngine:
    """策略生命周期注册与每日评估。定时触发归 P5 编排层，本引擎只做判定与落库。"""

    def __init__(self, conn: duckdb.DuckDBPyConnection, config: Any) -> None:
        self._conn = conn
        self._config = config
        lc = (config.as_dict().get("swing_track", {}) or {}).get("lifecycle", {}) or {}
        self._min_is_oos = float(lc.get("incubating_min_is_oos", 0.7))
        self._paper_min_days = int(lc.get("paper_min_days", 20))
        self._paper_min_return = float(lc.get("paper_min_return", 0.0))
        self._paper_max_drawdown = float(lc.get("paper_max_drawdown", -0.05))
        self._probation_to_production_min_sharpe = float(
            lc.get("probation_to_production_min_sharpe", 1.0)
        )
        self._production_min_sharpe = float(lc.get("production_min_sharpe", 1.0))
        self._production_max_drawdown = float(lc.get("production_max_drawdown", -0.15))
        self._retire_sharpe = float(lc.get("retire_sharpe", 0.3))
        self._retire_duration_days = int(lc.get("retire_duration_days", 14))
        self._retire_daily_loss = float(lc.get("retire_daily_loss", -0.05))
        self._capital_pcts = {
            k: float(lc.get(v, 0.0)) for k, v in _STAGE_CAPITAL_KEYS.items() if v is not None
        }
        self._config_hash = str(config.content_hash())
        self._code_version = __version__

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------

    def register(
        self,
        name: str,
        version: str,
        params: dict[str, Any] | None,
        *,
        track: str = "swing",
        as_of: date | None = None,
    ) -> str:
        """注册新策略，初始阶段 INCUBATING（配额 0）。返回 strategy_id。

        Args:
            as_of: 业务日期（回测场景用区间首日）；None 时用当前日期。

        Raises:
            ValueError: track != 'swing'（价值轨不适用生命周期）。
        """
        if track != "swing":
            raise ValueError(f"价值轨不适用策略生命周期（走 thesis 证伪），收到 track={track!r}")
        strategy_id = str(uuid.uuid4())
        entry_ts = datetime.combine(as_of, time(9, 30)) if as_of is not None else datetime.now()
        params_json = json.dumps(params, ensure_ascii=False) if params else None
        self._conn.execute(
            """
            INSERT INTO strategy_definition
                (strategy_id, name, track, version, params_json, config_hash, code_version)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [strategy_id, name, track, version, params_json, self._config_hash, self._code_version],
        )
        self._conn.execute(
            """
            INSERT INTO strategy_stage (strategy_id, stage, capital_pct, entered_at, updated_at)
            VALUES (?, ?, 0, ?, ?)
            """,
            [strategy_id, str(Stage.INCUBATING), entry_ts, entry_ts],
        )
        self._write_history(
            strategy_id, None, Stage.INCUBATING, "注册：初始孵化", kpi=None, decided_at=entry_ts
        )
        return strategy_id

    # ------------------------------------------------------------------
    # 每日评估（判定，不落库）
    # ------------------------------------------------------------------

    def evaluate(
        self,
        strategy_id: str,
        as_of: date,
        *,
        wf_gate_passed: bool = False,
        swing_account: str = "acc_swing",
    ) -> LifecycleEvaluation:
        """按 KPI 快照判定目标阶段。wf_gate_passed 由编排层先跑 Walk-Forward 注入。"""
        stage = self._current_stage(strategy_id)
        if stage == Stage.RETIRED:
            return LifecycleEvaluation(
                strategy_id=strategy_id,
                current_stage=stage,
                target_stage=Stage.RETIRED,
                reason="已退役，不再迁移",
                kpi={},
                as_of=as_of,
            )

        series = self._equity_series(strategy_id, as_of, stage, swing_account)
        kpi = compute_kpi_snapshot(series, retire_sharpe=self._retire_sharpe)

        # 1. 立即退役：单日亏损
        daily = kpi.get("daily_return_latest")
        if daily is not None and daily <= self._retire_daily_loss:
            return self._retire_eval(
                strategy_id,
                stage,
                f"单日亏损 {daily:.2%} ≤ {self._retire_daily_loss:.2%}，立即退役",
                kpi,
                as_of,
                swing_account,
            )

        # 2. 低迷退役：滚动 Sharpe 持续低于阈值
        low_days = kpi.get("consecutive_low_sharpe_calendar_days") or 0
        if low_days >= self._retire_duration_days:
            rs = kpi.get("rolling_30d_sharpe")
            return self._retire_eval(
                strategy_id,
                stage,
                f"滚动 30 日 Sharpe {rs:.2f} < {self._retire_sharpe} 持续 {low_days} 自然日"
                if rs is not None
                else f"滚动 30 日 Sharpe 持续低迷 {low_days} 自然日",
                kpi,
                as_of,
                swing_account,
            )

        # 3. 降级：主力跌破保持线
        if stage == Stage.PRODUCTION:
            rs = kpi.get("rolling_30d_sharpe")
            rdd = kpi.get("rolling_30d_max_drawdown")
            if (rs is not None and rs < self._production_min_sharpe) or (
                rdd is not None and rdd < self._production_max_drawdown
            ):
                return LifecycleEvaluation(
                    strategy_id=strategy_id,
                    current_stage=stage,
                    target_stage=Stage.PROBATION,
                    reason=(
                        f"主力保持线跌破：滚动 30 日 Sharpe {rs}"
                        f"（保持 {self._production_min_sharpe}），"
                        f"滚动回撤 {rdd}（上限 {self._production_max_drawdown}），降级回试用"
                    ),
                    kpi=kpi,
                    as_of=as_of,
                )

        # 4. 晋级
        if stage == Stage.INCUBATING and wf_gate_passed:
            return LifecycleEvaluation(
                strategy_id=strategy_id,
                current_stage=stage,
                target_stage=Stage.PAPER,
                reason=f"Walk-Forward 门禁通过（IS/OOS ≥ {self._min_is_oos}），进入纸交易",
                kpi=kpi,
                as_of=as_of,
            )
        if stage == Stage.PAPER:
            days = kpi.get("trading_days") or 0
            total_return = kpi.get("total_return")
            max_dd = kpi.get("max_drawdown")
            if (
                days >= self._paper_min_days
                and total_return is not None
                and total_return > self._paper_min_return
                and max_dd is not None
                and max_dd > self._paper_max_drawdown
            ):
                return LifecycleEvaluation(
                    strategy_id=strategy_id,
                    current_stage=stage,
                    target_stage=Stage.PROBATION,
                    reason=(
                        f"纸交易 {days} 日达标：累计收益 {total_return:.2%} > "
                        f"{self._paper_min_return:.2%}，"
                        f"最大回撤 {max_dd:.2%} > {self._paper_max_drawdown:.2%}，进入试用"
                    ),
                    kpi=kpi,
                    as_of=as_of,
                )
        if stage == Stage.PROBATION:
            rs = kpi.get("rolling_30d_sharpe")
            if rs is not None and rs > self._probation_to_production_min_sharpe:
                return LifecycleEvaluation(
                    strategy_id=strategy_id,
                    current_stage=stage,
                    target_stage=Stage.PRODUCTION,
                    reason=(
                        f"试用达标：滚动 30 日 Sharpe {rs:.2f} > "
                        f"{self._probation_to_production_min_sharpe}，晋升主力"
                    ),
                    kpi=kpi,
                    as_of=as_of,
                )

        return LifecycleEvaluation(
            strategy_id=strategy_id,
            current_stage=stage,
            target_stage=stage,
            reason="KPI 未触发任何迁移",
            kpi=kpi,
            as_of=as_of,
        )

    # ------------------------------------------------------------------
    # 迁移落库（配额联动 + 流水 + 退役清仓 Intent 返回）
    # ------------------------------------------------------------------

    def apply(self, evaluation: LifecycleEvaluation) -> LifecycleEvaluation:
        """把 evaluate 的结果落库；无变化时仅刷新 KPI 快照。"""
        sid = evaluation.strategy_id
        kpi_json = json.dumps(evaluation.kpi, ensure_ascii=False, default=str)
        if not evaluation.changed:
            self._conn.execute(
                "UPDATE strategy_stage SET kpi_json = ?, updated_at = now() WHERE strategy_id = ?",
                [kpi_json, sid],
            )
            return evaluation

        capital_pct = self._capital_pct_of(evaluation.target_stage)
        entry_ts = datetime.combine(evaluation.as_of, time(9, 30))
        self._conn.execute(
            """
            UPDATE strategy_stage
            SET stage = ?, capital_pct = ?, kpi_json = ?, entered_at = ?, updated_at = ?
            WHERE strategy_id = ?
            """,
            [str(evaluation.target_stage), capital_pct, kpi_json, entry_ts, entry_ts, sid],
        )
        self._write_history(
            sid,
            evaluation.current_stage,
            evaluation.target_stage,
            evaluation.reason,
            evaluation.kpi,
            decided_at=entry_ts,
        )
        return evaluation

    def evaluate_and_apply(
        self,
        strategy_id: str,
        as_of: date,
        *,
        wf_gate_passed: bool = False,
        swing_account: str = "acc_swing",
    ) -> LifecycleEvaluation:
        """便捷入口：evaluate + apply。返回含退役清仓 Intent（若有）的结果。"""
        return self.apply(
            self.evaluate(
                strategy_id, as_of, wf_gate_passed=wf_gate_passed, swing_account=swing_account
            )
        )

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def stage_of(self, strategy_id: str) -> Stage:
        return self._current_stage(strategy_id)

    def capital_pct_of(self, strategy_id: str) -> float:
        row = self._conn.execute(
            "SELECT capital_pct FROM strategy_stage WHERE strategy_id = ?", [strategy_id]
        ).fetchone()
        return float(row[0]) if row else 0.0

    def history(self, strategy_id: str) -> list[dict[str, Any]]:
        # decided_at CAST 为 DATE：DuckDB 原生 TIMESTAMPTZ 转 Python 依赖 pytz（未装）
        rows = self._conn.execute(
            """
            SELECT from_stage, to_stage, reason, kpi_json, decided_by, CAST(decided_at AS DATE)
            FROM strategy_stage_history WHERE strategy_id = ? ORDER BY decided_at, history_id
            """,
            [strategy_id],
        ).fetchall()
        return [
            {
                "from_stage": r[0],
                "to_stage": r[1],
                "reason": r[2],
                "kpi": json.loads(r[3]) if r[3] else None,
                "decided_by": r[4],
                "decided_at": r[5],
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _retire_eval(
        self,
        strategy_id: str,
        stage: Stage,
        reason: str,
        kpi: dict[str, Any],
        as_of: date,
        swing_account: str,
    ) -> LifecycleEvaluation:
        intents = self._liquidation_intents(strategy_id, as_of, swing_account)
        return LifecycleEvaluation(
            strategy_id=strategy_id,
            current_stage=stage,
            target_stage=Stage.RETIRED,
            reason=reason,
            kpi=kpi,
            as_of=as_of,
            liquidation_intents=intents,
        )

    def _liquidation_intents(
        self, strategy_id: str, as_of: date, swing_account: str
    ) -> tuple[OrderIntent, ...]:
        """退役清仓：该策略名下波段子账本全部持仓按可用量生成 MARKET 卖出 Intent。

        只产 Intent，不直接执行——走 ExecutionService 规则管线（T+1/涨跌停照常约束）；
        当日买入的冻结量经 PositionLimitRule 拦截后由编排层次日重试。
        """
        rows = self._conn.execute(
            """
            SELECT DISTINCT p.code, ds.market, p.qty, COALESCE(p.frozen_qty, 0)
            FROM position p
            LEFT JOIN dim_stock ds ON ds.code = p.code
            WHERE p.account_id = ? AND p.qty > 0
              AND EXISTS (
                  SELECT 1 FROM order_intent oi
                  JOIN trade_log t ON t.intent_id = oi.intent_id
                  WHERE oi.strategy_id = ? AND t.code = p.code AND oi.side = 'BUY'
              )
            ORDER BY p.code
            """,
            [swing_account, strategy_id],
        ).fetchall()
        out: list[OrderIntent] = []
        for code, market, qty, frozen in rows:
            available = int(qty) - int(frozen)
            if available <= 0:
                continue
            out.append(
                OrderIntent(
                    intent_id=str(uuid.uuid4()),
                    account_id=swing_account,
                    mode="swing",
                    code=code,
                    market=market or ("HK" if len(code) == 5 else "SH"),
                    side=OrderSide.SELL,
                    order_type=OrderType.MARKET,
                    qty=available,
                    price=None,
                    strategy_id=strategy_id,
                    submitted_at=datetime.combine(as_of, time(9, 30)),
                    time_in_force=TimeInForce.GTC,
                )
            )
        return tuple(out)

    def _equity_series(
        self, strategy_id: str, as_of: date, stage: Stage, swing_account: str
    ) -> list[tuple[date, float]]:
        """净值序列：起点取 MIN(阶段进入日, 最早成交日)，本金用策略实际占用资金口径。

        本金（分母）= 策略截至起点的累计净投入（BUY 流出 - SELL 流入，含费用）。
        这样单日收益/Sharpe/回撤都是策略自身资金口径，不被账本名义总额稀释——
        例如末日 -8% 单日亏损能如实反映为 -8%，而非 -0.04%（全账本摊薄）。
        无成交时回退为当前配额 x 子账本额度（仍可能为 0，此时序列全零、KPI 为 None）。
        """
        entered = self._stage_entered_date(strategy_id)
        first_trade = self._first_trade_date(strategy_id)
        start = min(entered, first_trade) if first_trade is not None else entered
        base_cash = self._peak_capital(strategy_id, start, as_of)
        if base_cash <= 0:
            # 无成交（或净投入为 0）：回退当前配额口径，仍防除零
            capital_pct = self.capital_pct_of(strategy_id)
            base_cash = strategy_base_cash(self._conn, capital_pct, swing_account)
        return replay_strategy_equity(self._conn, strategy_id, start, as_of, base_cash)

    def _first_trade_date(self, strategy_id: str) -> date | None:
        row = self._conn.execute(
            """
            SELECT MIN(t.trade_date) FROM trade_log t
            JOIN order_intent oi ON t.intent_id = oi.intent_id
            WHERE oi.strategy_id = ?
            """,
            [strategy_id],
        ).fetchone()
        return row[0] if row and row[0] is not None else None

    def _peak_capital(self, strategy_id: str, start: date, end: date) -> float:
        """策略在 [start, end] 内的峰值累计净投入（资金占用口径的分母）。

        逐日累计净现金流（-amount_cny 为流入策略资金），取区间内最大值——
        代表策略实际动用的最大资金量，收益/Sharpe/回撤以此为本金，
        不被账本名义总额稀释，也不会因起点尚未买入而为 0。
        """
        rows = self._conn.execute(
            """
            SELECT t.trade_date, -SUM(t.amount_cny) AS day_flow
            FROM trade_log t
            JOIN order_intent oi ON t.intent_id = oi.intent_id
            WHERE oi.strategy_id = ? AND t.trade_date BETWEEN ? AND ?
            GROUP BY t.trade_date ORDER BY t.trade_date
            """,
            [strategy_id, start, end],
        ).fetchall()
        peak = 0.0
        cumulative = 0.0
        for _d, day_flow in rows:
            cumulative += float(day_flow)
            peak = max(peak, cumulative)
        return peak

    def _stage_entered_date(self, strategy_id: str) -> date:
        row = self._conn.execute(
            "SELECT CAST(entered_at AS DATE) FROM strategy_stage WHERE strategy_id = ?",
            [strategy_id],
        ).fetchone()
        if row is None:
            raise ValueError(f"策略未注册: {strategy_id}")
        entered = row[0]
        if not isinstance(entered, date):
            raise TypeError(f"entered_at 转换异常: {entered!r}")
        return entered

    def _current_stage(self, strategy_id: str) -> Stage:
        row = self._conn.execute(
            "SELECT stage FROM strategy_stage WHERE strategy_id = ?", [strategy_id]
        ).fetchone()
        if row is None:
            raise ValueError(f"策略未注册: {strategy_id}")
        return Stage(row[0])

    def _capital_pct_of(self, stage: Stage) -> float:
        return self._capital_pcts.get(stage, 0.0)

    def _write_history(
        self,
        strategy_id: str,
        from_stage: Stage | None,
        to_stage: Stage,
        reason: str,
        kpi: dict[str, Any] | None,
        decided_at: datetime | None = None,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO strategy_stage_history
                (history_id, strategy_id, from_stage, to_stage, reason, kpi_json,
                 decided_by, decided_at, config_hash, code_version)
            VALUES (?, ?, ?, ?, ?, ?, 'engine', COALESCE(?, now()), ?, ?)
            """,
            [
                str(uuid.uuid4()),
                strategy_id,
                str(from_stage) if from_stage else None,
                str(to_stage),
                reason,
                json.dumps(kpi, ensure_ascii=False, default=str) if kpi else None,
                decided_at,
                self._config_hash,
                self._code_version,
            ],
        )


__all__ = [
    "LifecycleEngine",
    "LifecycleEvaluation",
    "Stage",
]
