"""P4.6 Brinson 归因定量层（BHB 单期模型，纯函数零状态零外部依赖）。

口径说明（2026-10-17 定稿，与设计方案归因章节一致）：
- 单期 BHB：期初权重、区间收益；
  allocation_i  = (wp_i - wb_i) * (rb_i - rb)    行业配置效应
  selection_i   = wb_i * (rp_i - rb_i)           个股选择效应
  interaction_i = (wp_i - wb_i) * (rp_i - rb_i)  交互效应
  恒等式：Σ三效应 = rp - rb（前提 Σwp + 现金权重 = 1 且 Σwb = 1）。
- 基准为自算全市场市值加权基准：wb_i / rb_i 由装配层 loader 按
  fact_valuation.market_cap 期初市值加权计算，保证恒等式严格成立；
- 沪深300指数收益仅作「指数口径超额」参考字段，不参与三效应分解；
- 现金作为 cash 段注入：rp=0、wb=0，rb_cash 约定 = rb（allocation 恒为 0，
  现金拖累全部落入 interaction，现金段总效应 = -wp_cash * rb）；
- 区间新建仓位（open_date 落在区间内）不参与 Brinson 分解，由装配层剔除后经
  new_positions 单列透传展示（口径：新建仓收益不属于期初配置决策的结果）；
- 未持有行业段（wp=0）：装配层须将其 return_portfolio 填为基准段收益，
  否则 selection 会产生伪贡献（本模块不静默修正）。

输入校验（fail-fast，与波段策略参数校验同风格）：
- 段权重为负、Σwp + cash_weight 偏离 1 超过 1e-6、Σwb 偏离 1 超过 1e-6、
  start > end → ValueError；
- 无行业段 → 返回 None（纯现金组合无可归因对象，区别于归因值 0）。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

CASH_SEGMENT = "cash"
_WEIGHT_TOL = 1e-6


@dataclass(frozen=True, slots=True)
class SegmentInput:
    """单个行业段归因输入（期初权重 + 区间收益，装配层已剔除区间新建仓）。"""

    segment: str  # 行业名（与 dim_stock.industry 口径一致）
    weight_portfolio: float  # wp：期初组合权重
    return_portfolio: float  # rp：区间组合段收益
    weight_benchmark: float  # wb：期初基准权重（市值加权）
    return_benchmark: float  # rb_i：区间基准段收益


@dataclass(frozen=True, slots=True)
class NewPositionItem:
    """区间新建仓位（不参与 Brinson 分解，单列展示）。"""

    code: str
    open_date: str  # ISO 日期


@dataclass(frozen=True, slots=True)
class SegmentAttribution:
    """单段三效应分解结果；total = 三效应之和。"""

    segment: str
    weight_portfolio: float
    weight_benchmark: float
    return_portfolio: float
    return_benchmark: float
    allocation: float
    selection: float
    interaction: float
    total: float


@dataclass(frozen=True, slots=True)
class BrinsonResult:
    """Brinson 归因汇总；active_return = portfolio_return - benchmark_return。"""

    account_id: str
    start: str  # ISO 日期
    end: str  # ISO 日期
    portfolio_return: float  # rp（含现金段，现金收益按 0 计）
    benchmark_return: float  # rb（自算市值加权基准）
    active_return: float  # rp - rb
    total_allocation: float
    total_selection: float
    total_interaction: float
    segments: tuple[SegmentAttribution, ...] = ()
    new_positions: tuple[NewPositionItem, ...] = ()
    index_return: float | None = None  # 指数区间收益（参考口径，不参与分解）
    index_excess: float | None = None  # rp - index_return


def compute_brinson(
    *,
    account_id: str,
    start: date,
    end: date,
    segments: Sequence[SegmentInput],
    cash_weight: float = 0.0,
    index_return: float | None = None,
    new_positions: Sequence[NewPositionItem] = (),
) -> BrinsonResult | None:
    """单期 Brinson 三效应分解（纯函数，不碰数据库/网络/时钟）。

    Args:
        account_id: 目标子账本（acc_value / acc_swing）。
        start/end: 归因区间（期初权重按 start、收益按 [start, end]，由装配层保证）。
        segments: 行业段输入（不含现金、不含区间新建仓）。
        cash_weight: 期初现金权重（0~1）。
        index_return: 沪深300指数区间收益（参考口径，可不提供）。
        new_positions: 区间新建仓位列表（透传单列）。

    Returns:
        BrinsonResult；无行业段时返回 None。

    Raises:
        ValueError: 负权重、权重和偏离 1 超过 1e-6、start > end。
    """
    if start > end:
        raise ValueError(f"归因区间非法：start={start} > end={end}")
    if not segments:
        return None
    if cash_weight < 0.0:
        raise ValueError(f"现金权重非法：{cash_weight}")
    for s in segments:
        if s.weight_portfolio < 0.0 or s.weight_benchmark < 0.0:
            raise ValueError(
                f"段 {s.segment} 权重非法：wp={s.weight_portfolio} wb={s.weight_benchmark}"
            )
    sum_wp = math.fsum(s.weight_portfolio for s in segments) + cash_weight
    sum_wb = math.fsum(s.weight_benchmark for s in segments)
    if abs(sum_wp - 1.0) > _WEIGHT_TOL:
        raise ValueError(f"组合权重和（含现金）须为 1，实际 {sum_wp:.8f}")
    if abs(sum_wb - 1.0) > _WEIGHT_TOL:
        raise ValueError(f"基准权重和须为 1，实际 {sum_wb:.8f}")

    benchmark_return = math.fsum(s.weight_benchmark * s.return_benchmark for s in segments)
    portfolio_return = math.fsum(s.weight_portfolio * s.return_portfolio for s in segments)

    # 行业段按名称排序保证输出确定性；现金段固定排最后
    ordered = sorted(segments, key=lambda s: s.segment)
    if cash_weight > 0.0:
        ordered.append(SegmentInput(CASH_SEGMENT, cash_weight, 0.0, 0.0, benchmark_return))

    attributed: list[SegmentAttribution] = []
    for s in ordered:
        allocation = (s.weight_portfolio - s.weight_benchmark) * (
            s.return_benchmark - benchmark_return
        )
        selection = s.weight_benchmark * (s.return_portfolio - s.return_benchmark)
        interaction = (s.weight_portfolio - s.weight_benchmark) * (
            s.return_portfolio - s.return_benchmark
        )
        attributed.append(
            SegmentAttribution(
                segment=s.segment,
                weight_portfolio=s.weight_portfolio,
                weight_benchmark=s.weight_benchmark,
                return_portfolio=s.return_portfolio,
                return_benchmark=s.return_benchmark,
                allocation=allocation,
                selection=selection,
                interaction=interaction,
                total=allocation + selection + interaction,
            )
        )

    index_excess = portfolio_return - index_return if index_return is not None else None
    return BrinsonResult(
        account_id=account_id,
        start=start.isoformat(),
        end=end.isoformat(),
        portfolio_return=portfolio_return,
        benchmark_return=benchmark_return,
        active_return=portfolio_return - benchmark_return,
        total_allocation=math.fsum(r.allocation for r in attributed),
        total_selection=math.fsum(r.selection for r in attributed),
        total_interaction=math.fsum(r.interaction for r in attributed),
        segments=tuple(attributed),
        new_positions=tuple(new_positions),
        index_return=index_return,
        index_excess=index_excess,
    )
