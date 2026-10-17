"""自我学习地基（P5.3）：结果打标 + 评分卡聚合 + 案例尸检。

分层：learning 位于 domain 层（与 lifecycle/attribution 并列），
只 import engine/data/store/config，不 import agents/orchestrator/web。
注入侧（agents/context.py，P4.7）只读本包产出的 case_library/agent_scorecard。
"""

from .labeler import (
    DECISION_SWING,
    DECISION_VALUATION,
    LabelReport,
    label_swing_signals,
    label_valuation_sessions,
)
from .labels import (
    BarRow,
    OutcomeLabel,
    t_n_label,
    touch_label,
    value_eval_label,
)
from .scorecard import (
    LabelFact,
    ScorecardMetrics,
    aggregate_swing,
    aggregate_valuation,
    write_scorecards,
)

__all__ = [
    "DECISION_SWING",
    "DECISION_VALUATION",
    "BarRow",
    "LabelFact",
    "LabelReport",
    "OutcomeLabel",
    "ScorecardMetrics",
    "aggregate_swing",
    "aggregate_valuation",
    "label_swing_signals",
    "label_valuation_sessions",
    "t_n_label",
    "touch_label",
    "value_eval_label",
    "write_scorecards",
]
