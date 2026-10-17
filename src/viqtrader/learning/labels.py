"""P5.3 结果打标纯函数（零 DB 依赖，直接单测）。

标签口径（设计评审拍板）：
- T+N 固定时点：波段 T+5/10/20，价值 T+20/60/250。基准价 = 决策日收盘（复权），
  判定价 = 决策日之后第 N 个交易日收盘（复权）；hit = return_pct > 0；
  决策日后可用 K 线不足 N 根 = 未到期，返回 None（留待下次打标）。
- 触位事件（波段信号自带 entry/stop/target 参考位）：从决策日次日逐日扫描，
  low <= stop_ref 视为触止损、high >= target_ref 视为触目标，先触者定胜负；
  同日触两侧保守按 stop 先。return_pct 按触位参考价 / entry_ref 估算（保守口径，
  实际成交价只会更差或相当）。全部 K 线扫完未触位 = 观察中，返回 None。
- value_eval（价值轨估值决策）：与 T+N 同机制，detail 附估值误差
  valuation_error =（估值中枢 - 判定价）/ 判定价（正值 = 高估，与 P4.7 评分卡
  「error_mean 正值=高估」口径一致）。

复权口径：调用方传入的 BarRow 一律使用后复权价（close*adj_factor 等比缩放，
收益率与缩放基准无关）；参考位为信号产生时的未复权价，与近端 K 线同源，
5~20 日短窗口内分红间隙影响可忽略（detail 中保留原始参考位供审计）。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from typing import Any

# 拍板口径：波段 T+N  horizons / 价值 T+N horizons
SWING_T_N_HORIZONS: tuple[int, ...] = (5, 10, 20)
VALUE_T_N_HORIZONS: tuple[int, ...] = (20, 60, 250)

KIND_T_N = "t_n"
KIND_TOUCH = "touch"
KIND_VALUE_EVAL = "value_eval"
TOUCH_HORIZON = "first_touch"


@dataclass(frozen=True, slots=True)
class BarRow:
    """打标用日线（复权后价格，按 trade_date 升序）。"""

    trade_date: date
    high: float
    low: float
    close: float


@dataclass(frozen=True, slots=True)
class OutcomeLabel:
    """一条结果标签（与 outcome_label 表行同构，detail 落库前序列化为 JSON）。"""

    decision_type: str
    decision_ref: str
    code: str
    decision_date: date
    label_kind: str
    horizon: str
    label_date: date
    return_pct: float
    hit: bool
    detail: dict[str, Any]


def t_n_label(
    *,
    decision_type: str,
    decision_ref: str,
    code: str,
    decision_date: date,
    horizon_days: int,
    base_close: float,
    bars_after: list[BarRow],
    kind: str = KIND_T_N,
    extra_detail: dict[str, Any] | None = None,
) -> OutcomeLabel | None:
    """T+N 固定时点标签；bars_after 不足 N 根返回 None（未到期）。

    bars_after 为决策日之后的交易日序列（升序，不含决策日），第 N 根即 T+N。
    """
    if horizon_days <= 0:
        raise ValueError("horizon_days 必须为正整数")
    if base_close <= 0:
        raise ValueError("base_close 必须为正数")
    if len(bars_after) < horizon_days:
        return None
    target = bars_after[horizon_days - 1]
    return_pct = target.close / base_close - 1.0
    detail: dict[str, Any] = {
        "base_close": base_close,
        "label_close": target.close,
        "horizon_days": horizon_days,
    }
    if extra_detail:
        detail.update(extra_detail)
    return OutcomeLabel(
        decision_type=decision_type,
        decision_ref=decision_ref,
        code=code,
        decision_date=decision_date,
        label_kind=kind,
        horizon=f"T+{horizon_days}",
        label_date=target.trade_date,
        return_pct=return_pct,
        hit=return_pct > 0,
        detail=detail,
    )


def touch_label(
    *,
    decision_type: str,
    decision_ref: str,
    code: str,
    decision_date: date,
    entry_ref: float,
    stop_ref: float,
    target_ref: float,
    bars_after: list[BarRow],
) -> OutcomeLabel | None:
    """触位事件标签：先触止损/目标者定胜负，同日触两侧保守 stop 先。

    全部 bars_after 扫完未触位返回 None（观察中，留待下次打标）。
    """
    if entry_ref <= 0:
        raise ValueError("entry_ref 必须为正数")
    for days, bar in enumerate(bars_after, start=1):
        hit_stop = bar.low <= stop_ref
        hit_target = bar.high >= target_ref
        if not (hit_stop or hit_target):
            continue
        side = "stop" if hit_stop else "target"  # 同日触两侧：hit_stop 必为真 → stop 先
        touch_price = stop_ref if side == "stop" else target_ref
        return_pct = touch_price / entry_ref - 1.0
        return OutcomeLabel(
            decision_type=decision_type,
            decision_ref=decision_ref,
            code=code,
            decision_date=decision_date,
            label_kind=KIND_TOUCH,
            horizon=TOUCH_HORIZON,
            label_date=bar.trade_date,
            return_pct=return_pct,
            hit=side == "target",
            detail={
                "side": side,
                "entry_ref": entry_ref,
                "stop_ref": stop_ref,
                "target_ref": target_ref,
                "touch_price": touch_price,
                "days_to_touch": days,
                "same_day_both": hit_stop and hit_target,
            },
        )
    return None


def value_eval_label(
    *,
    decision_type: str,
    decision_ref: str,
    code: str,
    decision_date: date,
    horizon_days: int,
    base_close: float,
    bars_after: list[BarRow],
    estimate_mid: float | None,
) -> OutcomeLabel | None:
    """价值轨估值标签：T+N 机制 + detail 附估值误差（正值=高估）。"""
    label = t_n_label(
        decision_type=decision_type,
        decision_ref=decision_ref,
        code=code,
        decision_date=decision_date,
        horizon_days=horizon_days,
        base_close=base_close,
        bars_after=bars_after,
        kind=KIND_VALUE_EVAL,
    )
    if label is None or estimate_mid is None:
        return label
    label_close = label.detail["label_close"]
    detail = dict(label.detail)
    detail["estimate_mid"] = estimate_mid
    detail["valuation_error"] = (estimate_mid - label_close) / label_close
    return replace(label, detail=detail)


__all__ = [
    "KIND_TOUCH",
    "KIND_T_N",
    "KIND_VALUE_EVAL",
    "SWING_T_N_HORIZONS",
    "TOUCH_HORIZON",
    "VALUE_T_N_HORIZONS",
    "BarRow",
    "OutcomeLabel",
    "t_n_label",
    "touch_label",
    "value_eval_label",
]
