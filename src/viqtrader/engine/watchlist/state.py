"""狩猎清单状态机纯函数（P5.2）。

零状态零外部依赖：输入行情/财务/配置参数，输出状态判定结果。
v1 三态：watching / triggered / expired（ready 字段保留不使用）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True)
class WatchItem:
    """单条狩猎清单标的（纯数据载体，仅供 scan 层传递）。"""

    watch_id: str
    code: str
    target_buy_price: float
    last_review_date: date | None
    created_date: date  # watchlist.created_at 的 DATE 部分，用于反查复权因子
    status: str


@dataclass(frozen=True, slots=True)
class ScanVerdict:
    """单条扫描结论。"""

    watch_id: str
    code: str
    new_status: str  # watching / triggered / expired
    reason: str
    latest_close: float | None = None
    adjusted_target: float | None = None


def is_expired(last_review_date: date | None, today: date, ttl_days: int) -> bool:
    """保质期过期判定：last_review_date 缺失视为已过期（研究基准缺失）。

    边界：last_review_date + ttl_days 当天仍有效，次日过期（严格大于）。
    """
    if last_review_date is None:
        return True
    return (today - last_review_date).days > ttl_days


def adjust_target_price(
    target_buy_price: float,
    target_day_adj_factor: float | None,
    latest_adj_factor: float | None,
) -> float | None:
    """将设定日的 target_buy_price 按复权因子调整到最新交易日基准。

    除息日未复权 close 会跳变，须用复权因子把目标价平移到同一口径，
    否则分红后会误触发。无复权因子（如新股无历史）返回 None。
    """
    if target_day_adj_factor is None or latest_adj_factor is None:
        return None
    if target_day_adj_factor <= 0:
        return None
    return target_buy_price * (latest_adj_factor / target_day_adj_factor)


def is_triggered(latest_close: float | None, adjusted_target: float | None) -> bool:
    """价格触发：最新未复权收盘价 ≤ 复权调整后的目标价。"""
    if latest_close is None or adjusted_target is None:
        return False
    return latest_close <= adjusted_target


def has_new_report(last_review_date: date | None, latest_ann_date: date | None) -> bool:
    """复核期内是否有新财报披露。last_review_date 缺失视为有新财报。"""
    if latest_ann_date is None:
        return False
    if last_review_date is None:
        return True
    return latest_ann_date > last_review_date


__all__ = [
    "ScanVerdict",
    "WatchItem",
    "adjust_target_price",
    "has_new_report",
    "is_expired",
    "is_triggered",
]
