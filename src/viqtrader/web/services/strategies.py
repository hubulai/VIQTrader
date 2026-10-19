"""波段策略页数据装配（P6）：读 outputs/strategies.json 物化元数据。

web 层禁止 import agents（import-linter forbidden 契约），策略注册表元数据
由 run_daily DAG 每日导出为 strategies.json（P6.3），本模块纯文件消费。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class ParamView:
    name: str
    kind: str
    default: float
    description: str
    minimum: float | None
    maximum: float | None


@dataclass(frozen=True, slots=True)
class StrategyCard:
    strategy_id: str
    display_name: str
    description: str
    params: list[ParamView]
    is_active: bool


@dataclass(frozen=True, slots=True)
class StrategiesView:
    exported_at: str
    active_strategy: str
    cards: list[StrategyCard]


def load_strategies(outputs: Path, active_strategy: str) -> StrategiesView | None:
    """读取物化元数据；文件缺失/损坏返回 None（页面降级提示等待导出）。"""
    path = outputs / "strategies.json"
    if not path.exists():
        return None
    try:
        raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    cards: list[StrategyCard] = []
    for s in raw.get("strategies", []):
        params = [
            ParamView(
                name=str(p["name"]),
                kind=str(p.get("kind", "float")),
                default=float(p["default"]),
                description=str(p.get("description", "")),
                minimum=float(p["minimum"]) if p.get("minimum") is not None else None,
                maximum=float(p["maximum"]) if p.get("maximum") is not None else None,
            )
            for p in s.get("params", [])
        ]
        sid = str(s["strategy_id"])
        cards.append(
            StrategyCard(
                strategy_id=sid,
                display_name=str(s.get("display_name", sid)),
                description=str(s.get("description", "")),
                params=params,
                is_active=sid == active_strategy,
            )
        )
    return StrategiesView(
        exported_at=str(raw.get("exported_at", "")),
        active_strategy=active_strategy,
        cards=cards,
    )
