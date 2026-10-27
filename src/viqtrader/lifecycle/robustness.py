"""P3.8 回测稳健性审计层：PBO / CPCV / DSR（Bailey 2014 + Lopez de Prado 2017）。

定位边界（2026-10-27 裁决）：
- 本模块是**纯函数审计**：零 LLM / 零外部网络 / 零回测引擎重写；
  回测执行（BacktestEngine + SimulatedGateway + 规则链 + 账本）全由 P3.1 模块承担，
  本模块只在必要时跑回测取 block 内 Sharpe 并缓存，之后纯 numpy 级数组操作。
- 审计输出是**过拟合概率**（PBO ∈ [0,1]，越小越好）、**期望最大相对排名**
  （CPCV ∈ [0,1]，越接近 0.5 越好）、**deflated Sharpe ratio**（∈ [0,1]，
  越接近 1.0 越好）；三者互相独立，任一超过阈值都应触发人工审阅。
- v1 不做 **独立试验数折算**（参数组独立性不足时 DSR 会高估，保守取
  `len(param_grid)` 作为 N）；不做 **PBO 的 bootstrap 版**（比 CSCV 更慢）；
  不做 **block 重叠**（Lopez de Prado 推荐，但 v1 用不重叠等长 block 即可）。

性能核心优化（cs.quantpedia.com / Bailey 2014 §3.2）：
- **先缓存 block x param 的 Sharpe，再算 C(N, floor(N/2)) 组合**。
  对每个 block i（16 块）和参数组 k（5 组）独立跑一次回测，缓存 `S[i, k]`；
  之后 12870 个组合（C(16, 8)）只是数组查找 + mean + argmax + rank 计算，
  总成本 O(N·M) 而非 O(C(N, N/2)·M)。

用法（在 LifecycleEngine 里串）：
```python
report = evaluate_robustness(
    conn, config, strategy_factory, param_grid,
    start=date(2022,1,1), end=date(2026,9,30),
)
print(f"PBO = {report.pbo:.3f}, CPCV = {report.cpcv:.3f}, DSR = {report.dsr:.3f}")
```
"""

from __future__ import annotations

import itertools
import json
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date
from typing import Any

import duckdb

from viqtrader.engine.backtest.engine import BacktestEngine
from viqtrader.engine.backtest.strategy import Strategy
from viqtrader.lifecycle.walk_forward import TRIAL_ACCOUNT

TRADING_DAYS_PER_YEAR = 252


# ---------------------------------------------------------------------------
# 结果结构（全部 JSON 可序列化）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BlockResult:
    """单个时间 block 内各参数组的 Sharpe（None = 零方差或不可计算）。"""

    block_index: int
    block_start: date
    block_end: date
    sharpes: tuple[float | None, ...]  # 长度 = 参数组数量


@dataclass(frozen=True, slots=True)
class PBOAnalysis:
    """PBO / CPCV 结果（同一套 block 缓存，两种汇总视角）。"""

    n_blocks: int
    param_count: int
    combination_count: int  # C(N, floor(N/2)) 枚举总数
    valid_combination_count: int  # 跳过全 None block 后的有效组合数
    skipped_combinations: int  # 全 None 或 M==1 被跳过
    pbo: float | None  # PBO = mean(I(relative_rank > 0.5))；valid 组合为 0 时 None
    cpcv: float | None  # CPCV = mean(relative_rank)；同上
    relative_ranks: tuple[float, ...]  # 所有有效组合的 relative_rank 样本

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_blocks": self.n_blocks,
            "param_count": self.param_count,
            "combination_count": self.combination_count,
            "valid_combination_count": self.valid_combination_count,
            "skipped_combinations": self.skipped_combinations,
            "pbo": self.pbo,
            "cpcv": self.cpcv,
        }


@dataclass(frozen=True, slots=True)
class RobustnessReport:
    """顶层汇总。"""

    pbo: PBOAnalysis
    dsr: float | None  # 单尾 deflated Sharpe ratio；输入 Sharpe ≤ 0 时 0.0
    dsr_sample_length_days: int
    dsr_param_count: int
    overall_pass: bool  # PBO / CPCV / DSR 全部优于阈值

    def to_dict(self) -> dict[str, Any]:
        return {
            "pbo": self.pbo.to_dict(),
            "dsr": self.dsr,
            "dsr_sample_length_days": self.dsr_sample_length_days,
            "dsr_param_count": self.dsr_param_count,
            "overall_pass": self.overall_pass,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


# ---------------------------------------------------------------------------
# DSR（Bailey & López de Prado 2014）
# ---------------------------------------------------------------------------


def _normal_cdf(x: float) -> float:
    """标准正态 CDF，用 math.erf 近似（不需要 scipy）。"""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def deflated_sharpe_ratio(
    observed_sharpe: float | None,
    n_param_groups: int,
    sample_length_days: int,
    *,
    skew: float = 0.0,
    kurt: float = 3.0,
) -> float:
    """Bailey & López de Prado 2014: deflated Sharpe ratio（单尾概率 ∈ [0, 1]）。

    Args:
        observed_sharpe: 观测 Sharpe（年化，252 交易日）。None / ≤ 0 → 0.0。
        n_param_groups: 参数组数量。v1 近似 = 有效独立试验数（不做独立性折算）。
        sample_length_days: 样本长度（交易日）。
        skew / kurt: 收益分布偏度 / 峰度（默认正态 0 / 3，零修正）。

    Returns:
        DSR ∈ [0, 1]：观测 Sharpe 在 H0（随机搜索最优）假设下的**单尾 p-value 补**。
        值越接近 1.0 越不可能是随机噪声；≥ 0.95 通常认为显著好于随机。

    Edge handling:
        - observed_sharpe 为 None 或 ≤ 0 → 返回 0.0（零防御性）；
        - n_param_groups < 2 → 返回 0.5（退化为普通单尾显著性水平）；
        - sample_length_days ≤ 10 → 返回 0.0（样本太短无法估计）。
    """
    if observed_sharpe is None or observed_sharpe <= 0:
        return 0.0
    if n_param_groups < 2:
        return 0.5
    if sample_length_days <= 10:
        return 0.0

    # 年化 Sharpe 的标准误差 ≈ 1 / sqrt(T_years)
    t_years = sample_length_days / TRADING_DAYS_PER_YEAR
    sr_std = 1.0 / math.sqrt(max(0.01, t_years))

    # Gumbel 近似：N 次独立试验能找到的最大 Sharpe ≈ sqrt(2 ln(N))
    sr_max_expected = math.sqrt(2.0 * math.log(n_param_groups))

    z = (observed_sharpe - sr_max_expected) / sr_std

    # 非正态修正（保守放大 sigma）：|skew|/4 + |kurt-3|/8
    correction = 1.0 + abs(skew) * 0.25 + abs(kurt - 3.0) * 0.125
    z_corrected = z / correction

    return _normal_cdf(z_corrected)


# ---------------------------------------------------------------------------
# Block 缓存 + PBO/CPCV 计算（纯函数，输入矩阵）
# ---------------------------------------------------------------------------


def compute_pbo_cpcv(
    block_sharpes: Sequence[Sequence[float | None]],
    *,
    n_blocks: int | None = None,
    param_count: int | None = None,
) -> PBOAnalysis:
    """纯函数：给定 block x param 的 Sharpe 矩阵，算 PBO 和 CPCV。

    Args:
        block_sharpes: 二维序列，block_sharpes[i][k] = block i、参数组 k 的 Sharpe
            （None = 零方差 / 不可计算）。
        n_blocks / param_count: 可选显式尺寸（默认从矩阵形状推断）。

    Raises:
        ValueError: block 数 < 2 或参数组 < 1。
    """
    N = n_blocks if n_blocks is not None else len(block_sharpes)
    M = param_count if param_count is not None else (len(block_sharpes[0]) if N else 0)

    if N < 2:
        raise ValueError(f"至少需要 2 个 block（当前 {N}）")
    if M < 1:
        raise ValueError(f"至少需要 1 个参数组（当前 {M}）")

    # N=2 时 floor(N/2)=1，C(2,1)=2：每个 block 轮流当 IS / OOS
    is_size = N // 2
    combos = list(itertools.combinations(range(N), is_size))
    total_combos = len(combos)

    if M == 1:
        return PBOAnalysis(
            n_blocks=N,
            param_count=M,
            combination_count=total_combos,
            valid_combination_count=0,
            skipped_combinations=total_combos,
            pbo=1.0,
            cpcv=1.0,
            relative_ranks=(),
        )

    ranks: list[float] = []
    skipped = 0

    for is_combo in combos:
        oos_idx = [i for i in range(N) if i not in is_combo]

        # IS Sharpe：每个参数组在所有 IS block 的 Sharpe 均值（跳过 None）
        is_sr: list[float | None] = []
        for k in range(M):
            vals: list[float] = [
                v for v in (block_sharpes[i][k] for i in is_combo) if v is not None
            ]
            if not vals:
                is_sr.append(None)
            else:
                is_sr.append(sum(vals) / len(vals))

        valid_is = [(k, v) for k, v in enumerate(is_sr) if v is not None]
        if not valid_is:
            skipped += 1
            continue
        best_k = max(valid_is, key=lambda t: t[1])[0]
        best_is_sr = next(v for k, v in valid_is if k == best_k)

        # OOS Sharpe：每个参数组在所有 OOS block 的均值
        oos_sr: list[float | None] = []
        for k in range(M):
            oos_vals: list[float] = [
                v for v in (block_sharpes[i][k] for i in oos_idx) if v is not None
            ]
            if not oos_vals:
                oos_sr.append(None)
            else:
                oos_sr.append(sum(oos_vals) / len(oos_vals))

        if oos_sr[best_k] is None:
            # OOS 里 best_k 无值 = 这个参数组没有 OOS 数据，记最保守
            rel_rank = 1.0
        elif best_is_sr <= 0:
            # IS Sharpe 为负或零：这个参数组"最优"无意义，relative_rank = 1.0（最保守）
            rel_rank = 1.0
        else:
            # 排名：从大到小数有多少个参数组的 OOS Sharpe >= OOS_SR[best_k]
            # 并列时取最靠前位置（保守偏向过拟合判定）
            best_oos = oos_sr[best_k]
            assert best_oos is not None
            oos_valid = [(k, v) for k, v in enumerate(oos_sr) if v is not None]
            if not oos_valid:
                rel_rank = 1.0
            else:
                rank = sum(1 for _, v in oos_valid if v >= best_oos)
                denom = max(1, M - 1)
                rel_rank = max(0.0, min(1.0, (rank - 1) / denom))

        ranks.append(rel_rank)

    if not ranks:
        return PBOAnalysis(
            n_blocks=N,
            param_count=M,
            combination_count=total_combos,
            valid_combination_count=0,
            skipped_combinations=skipped,
            pbo=None,
            cpcv=None,
            relative_ranks=(),
        )

    pbo = sum(1 for r in ranks if r > 0.5) / len(ranks)
    cpcv = sum(ranks) / len(ranks)
    return PBOAnalysis(
        n_blocks=N,
        param_count=M,
        combination_count=total_combos,
        valid_combination_count=len(ranks),
        skipped_combinations=skipped,
        pbo=pbo,
        cpcv=cpcv,
        relative_ranks=tuple(ranks),
    )


# ---------------------------------------------------------------------------
# Block 划分 + 缓存执行（需要 DB / BacktestEngine）
# ---------------------------------------------------------------------------


def _split_equal_blocks(days: Sequence[date], n_blocks: int) -> list[list[date]]:
    """把交易日序列等长切成 n_blocks 段（向下取整，余数补给最后一段）。"""
    total = len(days)
    if total < n_blocks:
        raise ValueError(
            f"交易日数 {total} 不足以切成 {n_blocks} 个等长 block（每 block 至少 1 天）"
        )
    base = total // n_blocks
    rem = total - base * n_blocks
    out: list[list[date]] = []
    cursor = 0
    for i in range(n_blocks):
        size = base + (1 if i < rem else 0)
        out.append(list(days[cursor : cursor + size]))
        cursor += size
    return out


def _split_half_blocks(days: Sequence[date], n_blocks: int) -> list[tuple[date, date]]:
    """返回每个 block 的 (start, end) 日期对。"""
    blocks = _split_equal_blocks(days, n_blocks)
    return [(b[0], b[-1]) for b in blocks]


class _AccountRedirect:
    """策略包装：把所有 Intent 的 account_id 重定向到试跑账户。

    OrderIntent 是 frozen dataclass，必须用 dataclasses.replace 重建。
    同 walk_forward.py 中的同名类，零逻辑差异。
    """

    def __init__(self, inner: Strategy, account_id: str) -> None:
        self._inner = inner
        self._account_id = account_id

    def on_bar(self, ctx: Any) -> list[Any]:
        return [replace(i, account_id=self._account_id) for i in self._inner.on_bar(ctx)]


def _cache_block_sharpes(
    conn: duckdb.DuckDBPyConnection,
    config: Any,
    strategy_factory: Callable[[dict[str, Any]], Strategy],
    param_grid: Sequence[dict[str, Any]],
    blocks: Sequence[list[date]],
) -> tuple[list[BlockResult], int]:
    """对每个 block x 参数组跑一次 BacktestEngine，缓存 Sharpe。

    复用 walk_forward 模式：试跑账户 TRIAL_ACCOUNT，每 block 前清场。
    返回 (block_results, 试跑账户创建标志 0/1)；后者告诉调用方是否要 teardown。
    """
    # 确保试跑账户存在
    row = conn.execute(
        "SELECT account_id FROM account WHERE account_id = ?", [TRIAL_ACCOUNT]
    ).fetchone()
    if row is None:
        # 复制波段账户配额（和 WalkForwardRunner 同逻辑）
        row_src = conn.execute(
            "SELECT capital_quota, target_ratio FROM account WHERE account_id = 'acc_swing'"
        ).fetchone()
        if row_src is None:
            raise ValueError("缺少 acc_swing 账户，无法创建试跑账户")
        conn.execute(
            """
            INSERT INTO account (account_id, mode, base_currency, capital_quota, target_ratio)
            VALUES (?, 'swing', 'CNY', ?, ?)
            """,
            [TRIAL_ACCOUNT, float(row_src[0]), float(row_src[1])],
        )
        created = True
    else:
        created = False

    results: list[BlockResult] = []
    try:
        engine = BacktestEngine(conn, config)
        for i, block in enumerate(blocks):
            block_start, block_end = block[0], block[-1]
            sharpes: list[float | None] = []
            for params in param_grid:
                _reset_trial(conn)
                wrapped = _AccountRedirect(strategy_factory(params), TRIAL_ACCOUNT)
                r = engine.run(wrapped, block_start, block_end, accounts=[TRIAL_ACCOUNT])
                sh = r.metrics(TRIAL_ACCOUNT)["sharpe"]
                sharpes.append(float(sh) if sh is not None else None)
            results.append(
                BlockResult(
                    block_index=i,
                    block_start=block_start,
                    block_end=block_end,
                    sharpes=tuple(sharpes),
                )
            )
    finally:
        _teardown_trial(conn)
        if created:
            conn.execute("DELETE FROM account WHERE account_id = ?", [TRIAL_ACCOUNT])

    return results, 1 if created else 0


def _reset_trial(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(
        "DELETE FROM rule_check_log WHERE intent_id IN "
        "(SELECT intent_id FROM order_intent WHERE account_id = ?)",
        [TRIAL_ACCOUNT],
    )
    for table in ("order_intent", "trade_log", "cash_ledger", "risk_event", "position"):
        conn.execute(f"DELETE FROM {table} WHERE account_id = ?", [TRIAL_ACCOUNT])


def _teardown_trial(conn: duckdb.DuckDBPyConnection) -> None:
    _reset_trial(conn)


# ---------------------------------------------------------------------------
# 顶层入口：从 config 读阈值 + 跑审计 + 判门禁
# ---------------------------------------------------------------------------


def evaluate_robustness(
    conn: duckdb.DuckDBPyConnection,
    config: Any,
    strategy_factory: Callable[[dict[str, Any]], Strategy],
    param_grid: Sequence[dict[str, Any]],
    start: date,
    end: date,
    *,
    observed_sharpe: float | None = None,
    return_skew: float = 0.0,
    return_kurt: float = 3.0,
) -> RobustnessReport:
    """执行完整稳健性审计：PBO / CPCV / DSR。

    Args:
        observed_sharpe: 顶层策略的观测 Sharpe（DSR 用）。None → DSR 记 0.0。
        return_skew / kurt: 收益分布的偏度 / 峰度（DSR 非正态修正）。
            默认 0 / 3（正态）。

    Raises:
        ValueError: 交易日数不足 n_blocks 个。
    """
    rcfg = (config.as_dict().get("swing_track", {}) or {}).get("robustness", {}) or {}
    n_blocks = int(rcfg.get("n_blocks", 16))
    min_pbo = float(rcfg.get("pbo_warn", 0.5))  # > 0.5 告警
    max_cpcv = float(rcfg.get("cpcv_warn", 0.6))  # > 0.6 告警（越接近 0.5 越好）
    min_dsr = float(rcfg.get("dsr_min", 0.0))

    days = _trading_days(conn, start, end)
    if len(days) < n_blocks:
        raise ValueError(f"交易日数 {len(days)} 不足以切成 {n_blocks} 个 block（需 ≥ {n_blocks}）")

    blocks = _split_equal_blocks(days, n_blocks)
    block_results, _ = _cache_block_sharpes(conn, config, strategy_factory, param_grid, blocks)
    matrix: list[list[float | None]] = [list(b.sharpes) for b in block_results]

    pbo_analysis = compute_pbo_cpcv(matrix, n_blocks=n_blocks, param_count=len(param_grid))

    dsr = deflated_sharpe_ratio(
        observed_sharpe,
        n_param_groups=len(param_grid),
        sample_length_days=len(days),
        skew=return_skew,
        kurt=return_kurt,
    )

    # 判门禁（保守：PBO/CPCV/Dsr 只要有 None 就不过）
    pbo_ok = pbo_analysis.pbo is not None and pbo_analysis.pbo < min_pbo
    cpcv_ok = pbo_analysis.cpcv is not None and pbo_analysis.cpcv < max_cpcv
    dsr_ok = dsr >= min_dsr
    overall_pass = pbo_ok and cpcv_ok and dsr_ok

    return RobustnessReport(
        pbo=pbo_analysis,
        dsr=dsr,
        dsr_sample_length_days=len(days),
        dsr_param_count=len(param_grid),
        overall_pass=overall_pass,
    )


def _trading_days(conn: duckdb.DuckDBPyConnection, start: date, end: date) -> list[date]:
    rows = conn.execute(
        """
        SELECT DISTINCT trade_date FROM fact_daily_bar
        WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date
        """,
        [start, end],
    ).fetchall()
    return [r[0] for r in rows]


__all__ = [
    "BlockResult",
    "PBOAnalysis",
    "RobustnessReport",
    "compute_pbo_cpcv",
    "deflated_sharpe_ratio",
    "evaluate_robustness",
]
