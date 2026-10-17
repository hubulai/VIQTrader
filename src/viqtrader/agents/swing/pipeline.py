"""波段轨流水线编排（P4.5，设计方案 4.2 ⑥）。

确定性初筛（零 LLM）→ 波段选股 Agent → 信号解读 Agent → swing_signal 落库。

隔离与闸门口径：
- 单候选任何异常不影响其他候选（失败仅记录在返回结果中）；
- 选股 Agent 非 SUCCESS（DEGRADED/FAILED）→ 不跑解读、不落 swing_signal；
- signal_type=none → 不跑解读、不落库（无交易形态）；
- 解读 Agent 非 SUCCESS → 选股部分仍落库（interpret 列留空），人工评审时可见；
- (code, signal_date) 已存在 → 跳过（幂等，防同日重复跑产生重复信号）。

swing_signal 为建议层：status=pending，等 P6 人工确认，不产 OrderIntent、不进生命周期。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

import duckdb

from viqtrader.agents.contracts.swing import SignalInterpretOutput, SwingPickOutput
from viqtrader.agents.llm.base import LLMProvider
from viqtrader.agents.llm.runs import STATUS_SUCCESS
from viqtrader.agents.swing.interpreter_agent import run_interpreter_agent
from viqtrader.agents.swing.picker_agent import run_picker_agent
from viqtrader.agents.swing.screen import ScreenCandidate, screen_candidates
from viqtrader.config.loader import Config

__all__ = ["SwingPipelineResult", "run_swing_pipeline"]


@dataclass(frozen=True, slots=True)
class SwingPipelineResult:
    """单候选处理结果（供 CLI 打印与测试断言）。"""

    code: str
    name: str
    signal_date: str
    status: str  # signal / signal_partial / none / picker_failed / duplicate / error
    strategy_id: str | None = None
    signal_id: str | None = None
    signal_type: str | None = None
    picker_run_id: str | None = None
    interpreter_run_id: str | None = None
    error: str | None = None


def _signal_exists(conn: duckdb.DuckDBPyConnection, code: str, signal_date: Any) -> bool:
    row = conn.execute(
        "SELECT 1 FROM swing_signal WHERE code = ? AND signal_date = ?",
        [code, signal_date],
    ).fetchone()
    return row is not None


def _insert_signal(
    conn: duckdb.DuckDBPyConnection,
    candidate: ScreenCandidate,
    pick: SwingPickOutput,
    *,
    picker_run_id: str,
    interpretation: SignalInterpretOutput | None,
    interpreter_run_id: str | None,
) -> str:
    signal_id = str(uuid.uuid4())
    screen_snapshot = {
        "patterns": list(candidate.patterns),
        "score": candidate.score,
        **candidate.features,
    }
    conn.execute(
        """
        INSERT INTO swing_signal (
            signal_id, code, market, name, signal_date, strategy_id,
            screen_features_json, signal_type, pattern_desc,
            entry_ref, stop_ref, target_ref,
            picker_risks_json, picker_run_id,
            interpretation, invalidation_json, interpreter_risks_json,
            win_rate_note, interpreter_run_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            signal_id,
            candidate.code,
            candidate.market,
            candidate.name,
            candidate.signal_date,
            candidate.strategy_id,
            json.dumps(screen_snapshot, ensure_ascii=False),
            pick.signal_type,
            pick.pattern_desc,
            pick.entry_ref,
            pick.stop_ref,
            pick.target_ref,
            json.dumps(list(pick.risks), ensure_ascii=False),
            picker_run_id,
            interpretation.interpretation if interpretation is not None else None,
            json.dumps(list(interpretation.invalidation_conditions), ensure_ascii=False)
            if interpretation is not None
            else None,
            json.dumps(list(interpretation.key_risks), ensure_ascii=False)
            if interpretation is not None
            else None,
            interpretation.win_rate_note if interpretation is not None else None,
            interpreter_run_id,
        ],
    )
    return signal_id


def run_swing_pipeline(
    conn: duckdb.DuckDBPyConnection,
    config: Config,
    provider: LLMProvider,
    *,
    strategy_id: str | None = None,
    params_override: dict[str, Any] | None = None,
    top_n: int | None = None,
) -> list[SwingPipelineResult]:
    """跑完整波段流水线，返回逐候选结果。

    strategy_id 缺省读配置 swing_track.active_strategy；params_override 透传
    初筛（回测/调参用），落库信号记录实际生效的 strategy_id。
    """
    candidates = screen_candidates(
        conn, config, strategy_id=strategy_id, params_override=params_override, top_n=top_n
    )
    results: list[SwingPipelineResult] = []

    for candidate in candidates:
        date_str = str(candidate.signal_date)
        try:
            if _signal_exists(conn, candidate.code, candidate.signal_date):
                results.append(
                    SwingPipelineResult(
                        code=candidate.code,
                        name=candidate.name,
                        signal_date=date_str,
                        status="duplicate",
                        strategy_id=candidate.strategy_id,
                    )
                )
                continue

            picker_run_id, picker_run = run_picker_agent(
                conn, config, provider, candidate=candidate
            )
            if picker_run.status != STATUS_SUCCESS or not isinstance(
                picker_run.validation.parsed, SwingPickOutput
            ):
                results.append(
                    SwingPipelineResult(
                        code=candidate.code,
                        name=candidate.name,
                        signal_date=date_str,
                        status="picker_failed",
                        strategy_id=candidate.strategy_id,
                        picker_run_id=picker_run_id,
                        error="; ".join(picker_run.validation.errors),
                    )
                )
                continue

            pick = picker_run.validation.parsed
            if pick.signal_type == "none":
                results.append(
                    SwingPipelineResult(
                        code=candidate.code,
                        name=candidate.name,
                        signal_date=date_str,
                        status="none",
                        strategy_id=candidate.strategy_id,
                        picker_run_id=picker_run_id,
                    )
                )
                continue

            interpretation: SignalInterpretOutput | None = None
            interpreter_run_id: str | None = None
            interp_run = run_interpreter_agent(
                conn, config, provider, candidate=candidate, pick=pick
            )
            interpreter_run_id, interp_result = interp_run
            partial = False
            if interp_result.status == STATUS_SUCCESS and isinstance(
                interp_result.validation.parsed, SignalInterpretOutput
            ):
                interpretation = interp_result.validation.parsed
            else:
                partial = True  # 选股有效但解读失败：保留信号，解读列留空

            signal_id = _insert_signal(
                conn,
                candidate,
                pick,
                picker_run_id=picker_run_id,
                interpretation=interpretation,
                interpreter_run_id=interpreter_run_id,
            )
            results.append(
                SwingPipelineResult(
                    code=candidate.code,
                    name=candidate.name,
                    signal_date=date_str,
                    status="signal_partial" if partial else "signal",
                    strategy_id=candidate.strategy_id,
                    signal_id=signal_id,
                    signal_type=pick.signal_type,
                    picker_run_id=picker_run_id,
                    interpreter_run_id=interpreter_run_id,
                )
            )
        except Exception as exc:  # 单候选隔离，流水线继续
            results.append(
                SwingPipelineResult(
                    code=candidate.code,
                    name=candidate.name,
                    signal_date=date_str,
                    status="error",
                    strategy_id=candidate.strategy_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )

    return results
