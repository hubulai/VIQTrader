"""P3.5 Walk-Forward 滚动验证（设计文档第九节，波段轨标配；价值轨豁免）。

滚动窗口机制：
- 用 is_days 个交易日做样本内（IS）参数寻优（IS Sharpe 最优），
  紧接 oos_days 个交易日做样本外（OOS）评估，按 oos_days 步长滚动前进；
- 尾段不足一个完整 OOS 窗口的丢弃；
- 每窗输出 IS/OOS Sharpe、oos_is_ratio = OOS/IS、过拟合分数 = (IS - OOS) / IS；
- 汇总门禁：summary_ratio = 各窗 oos_is_ratio 均值，
  passed_gate = summary_ratio >= min_oos_is_ratio 且窗口数 >= min_windows（默认 2）。

边界规则（保守处理）：
- 某窗 IS Sharpe <= 0 或不可计算：该窗 oos_is_ratio 记 0、overfit_score 记 None
  （负 IS 下比率语义翻转，直接按最差处理）；
- 某窗内全部参数组 IS Sharpe 不可计算：跳过该窗（不参与均值）。

状态隔离（关键）：
- 回测会在真实表（position/cash_ledger/trade_log/order_intent...）落库，
  多参数组/多窗口连跑必然互相污染；
- 本模块统一把策略产出的 Intent 重定向到独立试跑账户（acc_wf，配额复制自波段账户），
  每轮试跑后清场该账户全部流水，主账户 acc_value/acc_swing 全程零污染。

引擎层纪律：纯代码零 LLM；阈值全部来自 config 的 swing_track.walk_forward。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any

import duckdb

from viqtrader.engine.backtest.engine import BacktestEngine
from viqtrader.engine.backtest.strategy import Strategy

TRIAL_ACCOUNT = "acc_wf"


@dataclass(frozen=True, slots=True)
class WindowResult:
    """单个滚动窗口的 IS/OOS 对照结果。"""

    window_index: int
    is_start: date
    is_end: date
    oos_start: date
    oos_end: date
    best_params: dict[str, Any] | None  # 该窗 IS 最优参数组；全无效时为 None
    is_sharpe: float | None
    oos_sharpe: float | None
    oos_is_ratio: float  # IS<=0 或不可算时记 0（保守）
    overfit_score: float | None  # (IS-OOS)/IS；IS<=0 时 None


@dataclass(frozen=True, slots=True)
class WalkForwardResult:
    """Walk-Forward 总结果：窗口明细 + 汇总门禁。全部字段可 JSON 序列化。"""

    windows: tuple[WindowResult, ...] = field(default_factory=tuple)
    summary_ratio: float = 0.0  # 各窗 oos_is_ratio 均值
    mean_overfit_score: float | None = None
    passed_gate: bool = False
    min_ratio: float = 0.7  # 判定用阈值（来自 config，留痕）
    min_windows: int = 2

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary_ratio": self.summary_ratio,
            "mean_overfit_score": self.mean_overfit_score,
            "passed_gate": self.passed_gate,
            "min_ratio": self.min_ratio,
            "min_windows": self.min_windows,
            "window_count": len(self.windows),
            "windows": [
                {
                    "window_index": w.window_index,
                    "is_start": w.is_start.isoformat(),
                    "is_end": w.is_end.isoformat(),
                    "oos_start": w.oos_start.isoformat(),
                    "oos_end": w.oos_end.isoformat(),
                    "best_params": w.best_params,
                    "is_sharpe": w.is_sharpe,
                    "oos_sharpe": w.oos_sharpe,
                    "oos_is_ratio": w.oos_is_ratio,
                    "overfit_score": w.overfit_score,
                }
                for w in self.windows
            ],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


class _AccountRedirect:
    """策略包装：把 Intent 重定向到试跑账户（OrderIntent 不可变，replace 重建）。"""

    def __init__(self, inner: Strategy, account_id: str) -> None:
        self._inner = inner
        self._account_id = account_id

    def on_bar(self, ctx: Any) -> list[Any]:
        return [replace(i, account_id=self._account_id) for i in self._inner.on_bar(ctx)]


class WalkForwardRunner:
    """Walk-Forward 滚动验证执行器（复用 BacktestEngine，环境同构）。"""

    def __init__(self, conn: duckdb.DuckDBPyConnection, config: Any) -> None:
        self._conn = conn
        self._config = config
        wf_cfg = (config.as_dict().get("swing_track", {}) or {}).get("walk_forward", {}) or {}
        self._is_days = int(wf_cfg.get("is_days", 250))
        self._oos_days = int(wf_cfg.get("oos_days", 60))
        self._min_ratio = float(wf_cfg.get("min_oos_is_ratio", 0.7))
        self._min_windows = int(wf_cfg.get("min_windows", 2))

    def run(
        self,
        strategy_factory: Callable[[dict[str, Any]], Strategy],
        param_grid: list[dict[str, Any]],
        start: date,
        end: date,
        *,
        source_account: str = "acc_swing",
    ) -> WalkForwardResult:
        """执行滚动验证。

        Args:
            strategy_factory: 参数组 → 新策略实例（每组参数必须产出全新实例，策略可带内部状态）。
            param_grid: 候选参数组（非空）。
            start/end: 验证区间（含首尾）。
            source_account: 配额与模式来源账户（默认波段子账本）。

        Raises:
            ValueError: param_grid 为空，或区间交易日不足一个完整 IS+OOS 窗口。
        """
        if not param_grid:
            raise ValueError("param_grid 不能为空")
        days = self._trading_days(start, end)
        windows = self._split_windows(days)
        if not windows:
            raise ValueError(
                f"交易日数 {len(days)} 不足一个完整窗口"
                f"（IS {self._is_days} + OOS {self._oos_days}）"
            )

        self._ensure_trial_account(source_account)
        try:
            results = tuple(
                self._run_window(strategy_factory, param_grid, idx, is_days, oos_days)
                for idx, (is_days, oos_days) in enumerate(windows)
            )
        finally:
            self._teardown_trial_account()

        valid = [w for w in results if w.best_params is not None]
        summary = sum(w.oos_is_ratio for w in valid) / len(valid) if valid else 0.0
        scores = [w.overfit_score for w in valid if w.overfit_score is not None]
        mean_score = sum(scores) / len(scores) if scores else None
        passed = summary >= self._min_ratio and len(valid) >= self._min_windows
        return WalkForwardResult(
            windows=results,
            summary_ratio=summary,
            mean_overfit_score=mean_score,
            passed_gate=passed,
            min_ratio=self._min_ratio,
            min_windows=self._min_windows,
        )

    # ------------------------------------------------------------------
    # 窗口
    # ------------------------------------------------------------------

    def _split_windows(self, days: list[date]) -> list[tuple[list[date], list[date]]]:
        """滚动切分：(IS, OOS) 对，步长 = oos_days，尾段不足完整 OOS 丢弃。"""
        out: list[tuple[list[date], list[date]]] = []
        cursor = 0
        total = len(days)
        while cursor + self._is_days + self._oos_days <= total:
            is_win = days[cursor : cursor + self._is_days]
            oos_win = days[cursor + self._is_days : cursor + self._is_days + self._oos_days]
            out.append((is_win, oos_win))
            cursor += self._oos_days
        return out

    def _run_window(
        self,
        strategy_factory: Callable[[dict[str, Any]], Strategy],
        param_grid: list[dict[str, Any]],
        window_index: int,
        is_days: list[date],
        oos_days: list[date],
    ) -> WindowResult:
        # IS 寻优：Sharpe 最优（None 视为无效参数组）
        best_params: dict[str, Any] | None = None
        best_sharpe: float | None = None
        for params in param_grid:
            sharpe = self._trial_sharpe(strategy_factory, params, is_days[0], is_days[-1])
            if sharpe is None:
                continue
            if best_sharpe is None or sharpe > best_sharpe:
                best_sharpe = sharpe
                best_params = params

        if best_params is None:
            return WindowResult(
                window_index=window_index,
                is_start=is_days[0],
                is_end=is_days[-1],
                oos_start=oos_days[0],
                oos_end=oos_days[-1],
                best_params=None,
                is_sharpe=None,
                oos_sharpe=None,
                oos_is_ratio=0.0,
                overfit_score=None,
            )

        # best_params 非 None 时 best_sharpe 必非 None（同一循环内成对赋值）
        if best_sharpe is None:
            raise RuntimeError("内部不变量被破坏：best_params 非空但 best_sharpe 为 None")
        is_sharpe = best_sharpe
        oos_sharpe = self._trial_sharpe(strategy_factory, best_params, oos_days[0], oos_days[-1])
        if is_sharpe <= 0 or oos_sharpe is None:
            ratio, score = 0.0, None
        else:
            ratio = oos_sharpe / is_sharpe
            score = (is_sharpe - oos_sharpe) / is_sharpe
        return WindowResult(
            window_index=window_index,
            is_start=is_days[0],
            is_end=is_days[-1],
            oos_start=oos_days[0],
            oos_end=oos_days[-1],
            best_params=best_params,
            is_sharpe=is_sharpe,
            oos_sharpe=oos_sharpe,
            oos_is_ratio=ratio,
            overfit_score=score,
        )

    def _trial_sharpe(
        self,
        strategy_factory: Callable[[dict[str, Any]], Strategy],
        params: dict[str, Any],
        start: date,
        end: date,
    ) -> float | None:
        """单参数组试跑（独立试跑账户 + 跑后清场），返回账户 Sharpe。"""
        self._reset_trial_account()
        strategy = _AccountRedirect(strategy_factory(params), TRIAL_ACCOUNT)
        engine = BacktestEngine(self._conn, self._config)
        result = engine.run(strategy, start, end, accounts=[TRIAL_ACCOUNT])
        sharpe = result.metrics(TRIAL_ACCOUNT)["sharpe"]
        return float(sharpe) if sharpe is not None else None

    # ------------------------------------------------------------------
    # 试跑账户隔离
    # ------------------------------------------------------------------

    def _ensure_trial_account(self, source_account: str) -> None:
        row = self._conn.execute(
            "SELECT capital_quota, target_ratio FROM account WHERE account_id = ?",
            [source_account],
        ).fetchone()
        if row is None:
            raise ValueError(f"来源账户不存在: {source_account}")
        self._conn.execute(
            """
            INSERT INTO account (account_id, mode, base_currency, capital_quota, target_ratio)
            VALUES (?, 'swing', 'CNY', ?, ?)
            """,
            [TRIAL_ACCOUNT, float(row[0]), float(row[1])],
        )

    def _reset_trial_account(self) -> None:
        """清空试跑账户全部流水（FK 安全顺序），配额行保留。"""
        self._conn.execute(
            """
            DELETE FROM rule_check_log WHERE intent_id IN
                (SELECT intent_id FROM order_intent WHERE account_id = ?)
            """,
            [TRIAL_ACCOUNT],
        )
        for table in ("order_intent", "trade_log", "cash_ledger", "risk_event", "position"):
            self._conn.execute(f"DELETE FROM {table} WHERE account_id = ?", [TRIAL_ACCOUNT])

    def _teardown_trial_account(self) -> None:
        self._reset_trial_account()
        self._conn.execute("DELETE FROM account WHERE account_id = ?", [TRIAL_ACCOUNT])

    def _trading_days(self, start: date, end: date) -> list[date]:
        rows = self._conn.execute(
            """
            SELECT DISTINCT trade_date FROM fact_daily_bar
            WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date
            """,
            [start, end],
        ).fetchall()
        return [r[0] for r in rows]


__all__ = [
    "TRIAL_ACCOUNT",
    "WalkForwardResult",
    "WalkForwardRunner",
    "WindowResult",
]
