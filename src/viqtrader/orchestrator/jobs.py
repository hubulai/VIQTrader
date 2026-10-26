"""每日 DAG 任务定义（P5.1）。

任务形态分两类：
- 子进程任务：sync_market / run_swing_pipeline / review_portfolio —— 外部依赖重
  （QMT 终端、LLM），进程隔离避免粘连，复用既有 CLI 与盒带归档；
- 进程内任务：quality（轻量 SQL 口径）/ label_outcomes（learning API）/
  watchlist_watch（狩猎清单盯价）/ risk_scan（止损扫描）/
  value_screen（价值初筛零 LLM）—— 库内操作，快速且错误可精确捕获。

价值初筛：每周 `orchestrator.value_screen_weekday`（isoweekday，缺省周六）执行，
结果写 value_candidate 周快照；候选池供人工挑选取做投委会/狩猎清单。

财报月复盘：run_date 落在 config `orchestrator.earnings_months` 且为
`orchestrator.review_weekday`（isoweekday）时追加 review_value/review_swing，
区间=当季季首→run_date。
"""

from __future__ import annotations

import gc
import json
import logging
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from viqtrader.agents.shared.db_lease import LeaseFn
from viqtrader.agents.swing.strategies.registry import (
    DEFAULT_STRATEGY_ID,
    list_strategies,
)
from viqtrader.agents.value.screen import run_value_screen
from viqtrader.alerting.router import AlertRouter
from viqtrader.config.loader import Config
from viqtrader.engine.execution.consume import consume_order_requests, latest_trade_date
from viqtrader.engine.risk.scan import scan_position_risk
from viqtrader.engine.watchlist.scan import scan_watchlist
from viqtrader.learning.labeler import label_swing_signals, label_valuation_sessions
from viqtrader.learning.scorecard import write_scorecards
from viqtrader.orchestrator.exports import export_swing_signals, export_value_candidates

from .dag import Task, TaskResult

logger = logging.getLogger(__name__)

# 子进程输出摘要只保留末尾若干行，避免状态文件膨胀
_TAIL_LINES = 3
_DETAIL_MAX = 200


@dataclass(slots=True)
class JobContext:
    """任务执行上下文。每个 job 按需取短租约（file_lease/hold），用完即关。

    短租约语义：每次 `lease()` 调用打开独立连接，退出时立即 close 释放 DuckDB
    写锁。相邻子进程/LLM 任务之间不残留持有期——Web 控制台在 DAG 间隙始终可读。
    """

    config: Config
    lease: LeaseFn
    repo_root: Path
    run_date: date
    alerter: AlertRouter

    @property
    def run_date_str(self) -> str:
        return self.run_date.isoformat()


# ---------------------------------------------------------------------------
# 子进程任务
# ---------------------------------------------------------------------------


def _run_script(ctx: JobContext, script: str, *args: str, timeout_seconds: int) -> TaskResult:
    """以当前解释器子进程跑 scripts/<script>，退出码非 0 → failed。

    子进程读写同库前仅需 `gc.collect()` 触发前一任务短租约 close 后可能残留的
    Windows 文件句柄；不再有持久连接需要手动关闭/重连。
    """
    gc.collect()
    cmd = [sys.executable, str(ctx.repo_root / "scripts" / script), *args]
    try:
        proc = subprocess.run(
            cmd,
            cwd=ctx.repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return TaskResult(status="failed", error=f"超时（>{timeout_seconds}s）: {script}")
    tail = [line for line in (proc.stdout or "").strip().splitlines() if line][-_TAIL_LINES:]
    summary = " | ".join(tail)[-_DETAIL_MAX:]
    if proc.returncode != 0:
        err_tail = [line for line in (proc.stderr or "").strip().splitlines() if line][-1:]
        hint = f"；stderr: {err_tail[0][-_DETAIL_MAX:]}" if err_tail else ""
        return TaskResult(status="failed", error=f"exit={proc.returncode} {summary}{hint}")
    return TaskResult(status="ok", detail=summary)


def _job_sync_daily(ctx: JobContext) -> TaskResult:
    timeout = int(ctx.config.get("orchestrator.sync_timeout_seconds", 1800))
    return _run_script(ctx, "sync_market.py", timeout_seconds=timeout)


def _job_swing_signal(ctx: JobContext) -> TaskResult:
    timeout = int(ctx.config.get("orchestrator.script_timeout_seconds", 1800))
    return _run_script(ctx, "run_swing_pipeline.py", timeout_seconds=timeout)


def _make_review_job(account_id: str) -> Task[JobContext]:
    def _run(ctx: JobContext) -> TaskResult:
        quarter_start = date(ctx.run_date.year, 3 * ((ctx.run_date.month - 1) // 3) + 1, 1)
        timeout = int(ctx.config.get("orchestrator.script_timeout_seconds", 1800))
        # review_portfolio 退出码语义：failed=1；unavailable/empty/skipped=0（P4.6 既定）
        return _run_script(
            ctx,
            "review_portfolio.py",
            "--account",
            account_id,
            "--start",
            quarter_start.isoformat(),
            "--end",
            ctx.run_date_str,
            timeout_seconds=timeout,
        )

    task_name = f"review_{account_id.removeprefix('acc_')}"
    return Task(name=task_name, run=_run, depends_on=("sync_daily",))


# ---------------------------------------------------------------------------
# 进程内任务
# ---------------------------------------------------------------------------


def _job_quality(ctx: JobContext) -> TaskResult:
    """轻量质检口径（v1）：最新交易日 bar 覆盖率 + 当日重复键 + 数据陈旧度。

    阈值 `orchestrator.quality_min_coverage`（默认 0.85）：最新交易日有 bar 的
    SH/SZ/BJ 标的数 / dim_stock 同口径总数。逐 code 全量质检归 audit_db（P7）。
    """
    with ctx.lease() as conn:
        row = conn.execute("SELECT max(trade_date) FROM fact_daily_bar").fetchone()
        latest = row[0] if row else None
        if latest is None:
            return TaskResult(status="failed", error="fact_daily_bar 为空，请先完成数据同步")

        row = conn.execute(
            "SELECT count(*) FROM dim_stock WHERE market IN ('SH','SZ','BJ')"
        ).fetchone()
        total = int(row[0]) if row else 0
        row = conn.execute(
            "SELECT count(DISTINCT code) FROM fact_daily_bar WHERE trade_date = ?", [latest]
        ).fetchone()
        covered = int(row[0]) if row else 0
        row = conn.execute(
            """
            SELECT count(*) FROM (
                SELECT code, trade_date FROM fact_daily_bar WHERE trade_date = ?
                GROUP BY code, trade_date HAVING count(*) > 1
            )
            """,
            [latest],
        ).fetchone()
        dup = int(row[0]) if row else 0

        warnings: list[str] = []
        coverage = (covered / total) if total else 0.0
        min_coverage = float(ctx.config.get("orchestrator.quality_min_coverage", 0.85))
        if total == 0:
            warnings.append("dim_stock 无 SH/SZ/BJ 标的")
        elif coverage < min_coverage:
            warnings.append(
                f"最新交易日覆盖率 {coverage:.1%}（{covered}/{total}）低于阈值 {min_coverage:.0%}"
            )
        if dup:
            warnings.append(f"最新交易日重复主键 {dup} 组")
        stale_days = (ctx.run_date - latest).days
        if stale_days > int(ctx.config.get("orchestrator.quality_stale_days", 5)):
            warnings.append(f"行情最新交易日 {latest} 距业务日期 {stale_days} 天，疑似数据陈旧")

        for w in warnings:
            ctx.alerter.emit("WARN", "quality", w)
        detail = f"最新交易日 {latest}，覆盖率 {coverage:.1%}（{covered}/{total}），重复键 {dup}"
        return TaskResult(status="ok", detail=detail)


def _job_label_outcomes(ctx: JobContext) -> TaskResult:
    """打标 + 评分卡聚合（P5.3 入口进程内复用，幂等每日重跑安全）。"""
    with ctx.lease() as conn:
        swing = label_swing_signals(conn)
        value = label_valuation_sessions(conn)
        written = write_scorecards(conn, window_end=ctx.run_date)
    for w in (*swing.warnings, *value.warnings):
        ctx.alerter.emit("WARN", "label_outcomes", w)
    detail = (
        f"打标 波段+{swing.inserted}/价值+{value.inserted}"
        f"（观察中 {swing.pending + value.pending}），评分卡 {len(written)} 行"
    )
    return TaskResult(status="ok", detail=detail)


def _job_watchlist_watch(ctx: JobContext) -> TaskResult:
    """狩猎清单盯价扫描（P5.2）：watching → triggered/expired 状态流转。

    依赖 sync_daily 确保行情为最新；告警级别：triggered/expired 均为 WARN
    （需人工介入确认买入或重新投委会）。
    """
    with ctx.lease() as conn:
        report = scan_watchlist(conn, ctx.config, ctx.run_date)
    for v in report.triggered:
        ctx.alerter.emit(
            "WARN",
            "watchlist_watch",
            f"{v.code} 触发买入信号：{v.reason}",
        )
    for v in report.expired:
        ctx.alerter.emit(
            "WARN",
            "watchlist_watch",
            f"{v.code} 已过期：{v.reason}",
        )
    detail = (
        f"盯价 {report.watched} 只：触发 {len(report.triggered)}、过期 {len(report.expired)}、"
        f"保持 {report.unchanged}、无行情 {len(report.skipped_no_market)}"
    )
    return TaskResult(status="ok", detail=detail)


def _job_risk_scan(ctx: JobContext) -> TaskResult:
    """持仓止损扫描（价值轨 watch 档 + 波段轨三事件）：risk_event 落库 + WARN 告警。

    只告警不自动下单（v1 模拟盘人工确认卖出）；幂等可每日重跑。
    """
    with ctx.lease() as conn:
        report = scan_position_risk(conn, ctx.config, ctx.run_date)
    for e in report.events:
        ctx.alerter.emit("WARN", "risk_scan", f"{e.code} {e.rule_key}: {e.message}")
    detail = (
        f"扫描持仓 {report.scanned} 只：新触发 {len(report.events)}、"
        f"已告警未处理 {report.suppressed}、无行情 {len(report.skipped_no_market)}"
    )
    return TaskResult(status="ok", detail=detail)


def _job_value_screen(ctx: JobContext) -> TaskResult:
    """价值初筛（零 LLM 排除法漏斗）：value_candidate 周快照 + INFO 告警。

    每周快照幂等（同日先删后插）；候选池供人工挑选取做投委会/狩猎清单。
    """
    with ctx.lease() as conn:
        report = run_value_screen(conn, ctx.config, ctx.run_date)
    detail = (
        f"初筛完成 {report.screen_date}：universe {report.universe} → "
        f"能力圈 {report.after_circle} → 硬剔除后 {report.after_hard} → "
        f"质量达标 {report.after_quality}（估值闸门通过 {report.gate_passed}）"
    )
    if report.candidates:
        top = report.candidates[0]
        ctx.alerter.emit(
            "INFO",
            "value_screen",
            f"质量榜首 {top.code} {top.name}：roe 均值 {top.roe_mean:.1%}、"
            f"pe 分位 {'—' if top.pe_percentile is None else f'{top.pe_percentile:.0%}'}",
        )
    return TaskResult(status="ok", detail=detail)


def _job_export_strategies(ctx: JobContext) -> TaskResult:
    """波段策略元数据物化（P6 拍板：web 禁 import agents，经 strategies.json 消费）。

    原子写 <data_dir>/outputs/strategies.json；注册表新增策略次日自动可见。
    """
    strategies = list_strategies()
    payload = {
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "active_strategy": str(ctx.config.get("swing_track.active_strategy", DEFAULT_STRATEGY_ID)),
        "strategies": [
            {
                "strategy_id": s.strategy_id,
                "display_name": s.display_name,
                "description": s.description,
                "params": [
                    {
                        "name": p.name,
                        "kind": p.kind.__name__,
                        "default": p.default,
                        "description": p.description,
                        "minimum": p.minimum,
                        "maximum": p.maximum,
                    }
                    for p in s.params_schema
                ],
            }
            for s in strategies
        ],
    }
    outputs = Path(str(ctx.config.require("platform.data_dir"))) / "outputs"
    outputs.mkdir(parents=True, exist_ok=True)
    tmp = outputs / "strategies.json.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(outputs / "strategies.json")
    return TaskResult(status="ok", detail=f"导出 {len(strategies)} 个策略元数据 → strategies.json")


def _job_export_value(ctx: JobContext) -> TaskResult:
    """价值候选池 CSV 导出（初筛日自动刷新；查阅/排序走文件，Web 展示不全的补充）。"""
    exports_dir = Path(str(ctx.config.require("platform.data_dir"))) / "outputs" / "exports"
    exports_dir.mkdir(parents=True, exist_ok=True)
    with ctx.lease() as conn:
        result = export_value_candidates(conn, exports_dir)
    if result is None:
        return TaskResult(status="ok", detail="value_candidate 为空，跳过导出")
    path, n = result
    return TaskResult(status="ok", detail=f"导出 {n} 条候选 → {path.name}")


def _job_export_swing(ctx: JobContext) -> TaskResult:
    """波段信号 CSV 导出（每日全量刷新，含已确认/已拒绝，供 Excel 排序查阅）。"""
    exports_dir = Path(str(ctx.config.require("platform.data_dir"))) / "outputs" / "exports"
    exports_dir.mkdir(parents=True, exist_ok=True)
    with ctx.lease() as conn:
        path, n = export_swing_signals(conn, exports_dir, ctx.run_date)
    return TaskResult(status="ok", detail=f"导出 {n} 条信号 → {path.name}")


def _job_execute_orders(ctx: JobContext) -> TaskResult:
    """消费 order_request 人工确认队列（P6 拍板③：每日 DAG 消费时机之一）。

    FILLED → executed（INFO 聚合）；REJECTED/NOT_FILLED → rejected（WARN 实时，
    用户需感知并决定是否改价重新确认）。一次性消费，幂等锚点 request_id。
    """
    with ctx.lease() as conn:
        trade_date = latest_trade_date(conn)
        if trade_date is None:
            return TaskResult(status="failed", error="fact_daily_bar 为空，无法确定成交参考日")
        report = consume_order_requests(conn, ctx.config, trade_date)
    for o in report.outcomes:
        level: Literal["INFO", "WARN"] = "INFO" if o.result == "executed" else "WARN"
        ctx.alerter.emit(
            level, "execute_orders", f"{o.code} {o.side} x{o.qty} → {o.result}（{o.detail}）"
        )
    detail = (
        f"成交参考日 {trade_date}：消费 {len(report.outcomes)} 笔"
        f"（成交 {report.executed}、拒绝 {report.rejected}）"
    )
    if report.accounts_initialized:
        detail += f"；初始化账户 {','.join(report.accounts_initialized)}"
    return TaskResult(status="ok", detail=detail)


# ---------------------------------------------------------------------------
# DAG 装配
# ---------------------------------------------------------------------------


def is_review_day(run_date: date, earnings_months: list[int], review_weekday: int) -> bool:
    """财报月内的复盘日（isoweekday：1=周一 … 7=周日）。"""
    return run_date.month in earnings_months and run_date.isoweekday() == review_weekday


def is_screen_day(run_date: date, screen_weekday: int) -> bool:
    """价值初筛日：每周固定一天（isoweekday，缺省周六）。"""
    return run_date.isoweekday() == screen_weekday


def build_daily_tasks(config: Config, run_date: date) -> list[Task[JobContext]]:
    """装配当日任务清单（注册序即拓扑序，依赖必须先注册）。"""
    tasks: list[Task[JobContext]] = [
        Task(name="sync_daily", run=_job_sync_daily),
        Task(name="quality", run=_job_quality, depends_on=("sync_daily",)),
        Task(name="swing_signal", run=_job_swing_signal, depends_on=("quality",)),
        Task(name="label_outcomes", run=_job_label_outcomes),
        Task(name="watchlist_watch", run=_job_watchlist_watch, depends_on=("sync_daily",)),
        Task(name="risk_scan", run=_job_risk_scan, depends_on=("sync_daily",)),
        Task(name="export_strategies", run=_job_export_strategies),
        Task(name="export_swing", run=_job_export_swing, depends_on=("swing_signal",)),
        Task(name="execute_orders", run=_job_execute_orders, depends_on=("sync_daily",)),
    ]

    screen_weekday = int(config.get("orchestrator.value_screen_weekday", 6))
    if is_screen_day(run_date, screen_weekday):
        tasks.append(Task(name="value_screen", run=_job_value_screen, depends_on=("quality",)))
        tasks.append(Task(name="export_value", run=_job_export_value, depends_on=("value_screen",)))

    earnings_months = [int(m) for m in config.get("orchestrator.earnings_months", [1, 4, 8, 10])]
    review_weekday = int(config.get("orchestrator.review_weekday", 6))
    if is_review_day(run_date, earnings_months, review_weekday):
        for account_id in config.get("orchestrator.review_accounts", ["acc_value", "acc_swing"]):
            tasks.append(_make_review_job(str(account_id)))
    return tasks


__all__ = ["JobContext", "build_daily_tasks", "is_review_day", "is_screen_day"]
