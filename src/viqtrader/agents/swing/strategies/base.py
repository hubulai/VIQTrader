"""波段初筛策略协议与参数描述（P4.5.1 多策略架构地基）。

设计要点：
- ScreenStrategy 为可插拔协议：strategy_id / 展示元数据 / params_schema / compute。
- params_schema 逐参数声明类型、默认值、取值范围与中文说明，既用于配置合并校验，
  也供 P6 控制台直接渲染成可编辑表单（后端契约先行，界面后续消费 list_strategies()）。
- compute 为纯函数口径：输入升序 K 线 + 已解析参数，输出特征 dict
  （必须含 patterns: list[str] 与 score: int），样本不足返回 None。
- resolve_params 三层合并：schema 默认值 ← YAML 策略节 ← 调用方覆盖（CLI/回测网格），
  未知键直接报错防手滑，越界值 fail-fast。

ScreenBar 定义在本模块以避免 strategies ↔ screen 循环导入；
screen.py 与 picker_agent 经 re-export 继续沿用原路径。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol, runtime_checkable

__all__ = ["ParamSpec", "ScreenBar", "ScreenStrategy", "resolve_params"]


@dataclass(frozen=True, slots=True)
class ScreenBar:
    """初筛用日线条目（日期升序）。"""

    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float


@dataclass(frozen=True, slots=True)
class ParamSpec:
    """单个策略参数的声明（供配置校验与界面渲染）。"""

    name: str
    kind: type[int] | type[float]  # int 或 float，合并时按此强转
    default: int | float
    description: str  # 中文说明，P6 直接作为表单项 label/提示
    minimum: float | None = None
    maximum: float | None = None

    def coerce(self, value: Any) -> int | float:
        """按声明类型强转并校验取值范围；非法输入抛 ValueError。"""
        try:
            coerced = self.kind(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"参数 {self.name}={value!r} 无法转换为 {self.kind.__name__}") from exc
        if self.minimum is not None and coerced < self.minimum:
            raise ValueError(f"参数 {self.name}={coerced} 低于下限 {self.minimum}")
        if self.maximum is not None and coerced > self.maximum:
            raise ValueError(f"参数 {self.name}={coerced} 高于上限 {self.maximum}")
        return coerced


@runtime_checkable
class ScreenStrategy(Protocol):
    """波段初筛策略协议：元数据 + 参数 schema + 纯函数特征计算。"""

    strategy_id: str
    display_name: str
    description: str

    def params_schema(self) -> tuple[ParamSpec, ...]:
        """声明全部可调参数（默认值即 YAML 缺省兜底）。"""
        ...

    def compute(self, bars: list[ScreenBar], params: Mapping[str, Any]) -> dict[str, Any] | None:
        """对单只标的升序 bars 计算形态特征；样本不足返回 None。

        返回 dict 必须含 patterns（list[str]）与 score（int），
        其余键为策略自有特征快照（随信号落库供审计回放）。
        """
        ...


def resolve_params(
    schema: tuple[ParamSpec, ...],
    *sources: Mapping[str, Any] | None,
) -> dict[str, int | float]:
    """按 schema 合并参数：默认值 ← 依次被 sources（YAML 节、调用覆盖）覆盖。

    - sources 中出现的未知键 → ValueError（防配置手滑静默失效）；
    - 所有值经 ParamSpec.coerce 强转并做范围校验。
    """
    specs = {p.name: p for p in schema}
    raw: dict[str, Any] = {p.name: p.default for p in schema}
    for source in sources:
        if not source:
            continue
        unknown = set(source) - set(specs)
        if unknown:
            raise ValueError(f"存在未声明的策略参数 {sorted(unknown)}，已声明参数：{sorted(specs)}")
        raw.update(source)
    return {name: specs[name].coerce(value) for name, value in raw.items()}
