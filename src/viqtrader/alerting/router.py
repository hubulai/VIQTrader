"""告警分级路由 v1（P5.1）。

分级语义（设计方案 7.3 节既定）：
- INFO：聚合推送——常驻进程形态按 `alerting.info_aggregate_minutes` 间隔，
  schtasks 单次运行形态退化为 **run 末尾一次性聚合**（flush 时写出）；
- WARN / CRITICAL / FATAL：实时写入。

v1 通道固定为 **控制台 + 告警日志文件**（`<data_dir>/logs/alerts/alerts-YYYYMMDD.log`，
按日滚动追加）。config `alerting.channels` 的邮件/webhook 多渠道键位为预留，
v1 不消费；FATAL 的「暂停引擎」语义在 schtasks 形态下无常驻进程可停，
退化为最高优先级醒目标记，由人工介入。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

AlertLevel = Literal["INFO", "WARN", "CRITICAL", "FATAL"]

_REALTIME_LEVELS: frozenset[AlertLevel] = frozenset({"WARN", "CRITICAL", "FATAL"})

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AlertRecord:
    """一条告警。source 为任务/模块名，message 为单行摘要。"""

    level: AlertLevel
    source: str
    message: str
    ts: datetime = field(default_factory=datetime.now)

    def format(self) -> str:
        return f"[{self.level}] {self.source}: {self.message}"


class AlertRouter:
    """分级路由器：实时级立即写，INFO 缓冲待 flush 聚合。

    控制台输出经 logging（WARNING 及以上实时，INFO 聚合同级），
    文件输出为纯文本逐行 `HH:MM:SS [LEVEL] source: message`。
    """

    def __init__(
        self, log_dir: Path, *, console: bool = True, today: datetime | None = None
    ) -> None:
        self._console = console
        now = today or datetime.now()
        log_dir.mkdir(parents=True, exist_ok=True)
        self._log_file = log_dir / f"alerts-{now:%Y%m%d}.log"
        self._info_buffer: list[AlertRecord] = []
        self._records: list[AlertRecord] = []

    @property
    def log_file(self) -> Path:
        return self._log_file

    @property
    def records(self) -> tuple[AlertRecord, ...]:
        """本 run 已发出的全部告警（含缓冲中的 INFO），供状态文件摘要。"""
        return tuple(self._records)

    def emit(self, level: AlertLevel, source: str, message: str) -> None:
        record = AlertRecord(level=level, source=source, message=message)
        self._records.append(record)
        if level in _REALTIME_LEVELS:
            self._write(record)
        else:
            self._info_buffer.append(record)

    def flush(self) -> list[AlertRecord]:
        """run 末尾聚合写出缓冲的 INFO，返回被聚合的记录（空缓冲返回空列表）。"""
        buffered = self._info_buffer
        self._info_buffer = []
        if not buffered:
            return []
        header = AlertRecord(
            level="INFO",
            source="alerting",
            message=f"INFO 聚合 {len(buffered)} 条（{sorted({r.source for r in buffered})}）",
        )
        self._write(header)
        for record in buffered:
            self._write(record)
        return buffered

    # ---------------- 内部 ----------------

    def _write(self, record: AlertRecord) -> None:
        line = f"{record.ts:%H:%M:%S} {record.format()}"
        with self._log_file.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        if self._console:
            # CRITICAL/FATAL 提级为 ERROR 让控制台醒目；INFO 聚合走 INFO 级
            log_level = logging.ERROR if record.level in ("CRITICAL", "FATAL") else logging.WARNING
            if record.level == "INFO":
                log_level = logging.INFO
            logger.log(log_level, "%s", record.format())


__all__ = ["AlertLevel", "AlertRecord", "AlertRouter"]
