"""P5.3 案例尸检单测：契约校验 / 装配 / 候选扫描幂等。"""

from __future__ import annotations

import json

import pytest

from viqtrader.agents.contracts.base import ContractError
from viqtrader.agents.contracts.postmortem import PostmortemContract
from viqtrader.agents.shared.postmortem_agent import (
    assemble_case_input,
    case_id_for,
    scan_postmortem_candidates,
)
from viqtrader.store.db import connect_memory
from viqtrader.store.migrate import migrate

# ---------------------------------------------------------------------------
# 契约校验
# ---------------------------------------------------------------------------


def _valid_payload() -> dict:
    return {
        "title": "假突破回落：放量长阳后量能断档",
        "failure_mode": "假突破",
        "postmortem": "突破日放量但次日量能断档，冲高回落触止损。",
        "tags": ["breakout", "量能断档"],
        "data_refs": ["决策快照 screen_features", "结果标签 touch"],
    }


def test_postmortem_contract_valid() -> None:
    out = PostmortemContract().parse(_valid_payload())
    assert out.title.startswith("假突破")
    assert out.failure_mode == "假突破"
    assert out.tags == ("breakout", "量能断档")


def test_postmortem_contract_success_case_null_failure_mode() -> None:
    payload = _valid_payload()
    payload["failure_mode"] = None
    out = PostmortemContract().parse(payload)
    assert out.failure_mode is None


def test_postmortem_contract_rejects_empty_data_refs() -> None:
    payload = _valid_payload()
    payload["data_refs"] = []
    with pytest.raises(ContractError, match="data_refs"):
        PostmortemContract().parse(payload)


def test_postmortem_contract_rejects_missing_title() -> None:
    payload = _valid_payload()
    del payload["title"]
    with pytest.raises(ContractError, match="title"):
        PostmortemContract().parse(payload)


# ---------------------------------------------------------------------------
# 装配与候选扫描
# ---------------------------------------------------------------------------


def _seed_swing_case(conn) -> None:
    """一只波段信号 + 一枚已判定 touch 标签。"""
    conn.execute(
        """
        INSERT INTO swing_signal (
            signal_id, code, market, name, signal_date, screen_features_json,
            signal_type, pattern_desc, entry_ref, stop_ref, target_ref, status
        ) VALUES ('sig1', '600000', 'SH', '测试股', '2026-01-05', '{"a":1}',
                  'breakout', '放量突破', 100.0, 95.0, 120.0, 'pending')
        """
    )
    conn.execute(
        """
        INSERT INTO outcome_label (
            decision_type, decision_ref, code, decision_date,
            label_kind, horizon, label_date, return_pct, hit, detail_json
        ) VALUES ('swing_pick', 'sig1', '600000', '2026-01-05', 'touch', 'first_touch',
                  '2026-01-08', -0.05, FALSE, '{"side":"stop"}')
        """
    )


def test_case_id_for() -> None:
    assert case_id_for("swing_pick", "sig1") == "swing_pick:sig1"


def test_assemble_case_input_swing() -> None:
    conn = connect_memory()
    migrate(conn)
    _seed_swing_case(conn)
    case_input = assemble_case_input(conn, "swing_pick", "sig1")
    assert case_input is not None
    assert case_input.code == "600000"
    assert case_input.market == "SH"
    assert case_input.thesis_type == "breakout"
    assert "放量突破" in case_input.decision_snapshot
    assert "touch/first_touch" in case_input.outcome_labels
    assert "-5.00%" in case_input.outcome_labels


def test_assemble_case_input_unknown_ref_returns_none() -> None:
    conn = connect_memory()
    migrate(conn)
    assert assemble_case_input(conn, "swing_pick", "nope") is None


def test_assemble_case_input_unknown_type_raises() -> None:
    conn = connect_memory()
    migrate(conn)
    with pytest.raises(ValueError, match="未知决策类型"):
        assemble_case_input(conn, "other", "x")


def test_scan_candidates_excludes_existing_case_and_draft(tmp_path) -> None:
    conn = connect_memory()
    migrate(conn)
    _seed_swing_case(conn)
    # 第二个决策：只有标签没有信号行（扫描只看标签表）
    conn.execute(
        """
        INSERT INTO outcome_label (
            decision_type, decision_ref, code, decision_date,
            label_kind, horizon, label_date, return_pct, hit, detail_json
        ) VALUES ('swing_pick', 'sig2', '600001', '2026-01-05', 't_n', 'T+5',
                  '2026-01-12', 0.03, TRUE, '{}')
        """
    )
    # sig1 已有案例 → 排除
    conn.execute("INSERT INTO case_library (case_id, title) VALUES ('swing_pick:sig1', '已入库')")
    # sig2 有草稿文件 → 排除
    (tmp_path / "swing_pick-sig2.json").write_text("{}", encoding="utf-8")

    assert scan_postmortem_candidates(conn, drafts_dir=tmp_path) == []

    # 清掉草稿与案例后应出现两个候选
    (tmp_path / "swing_pick-sig2.json").unlink()
    conn.execute("DELETE FROM case_library")
    candidates = scan_postmortem_candidates(conn, drafts_dir=tmp_path)
    refs = {c.decision_ref for c in candidates}
    assert refs == {"sig1", "sig2"}
    assert all(c.label_count == 1 for c in candidates)


def test_scan_candidates_ignores_nonexistent_draft_dir(tmp_path) -> None:
    conn = connect_memory()
    migrate(conn)
    _seed_swing_case(conn)
    missing = tmp_path / "not_exist"
    candidates = scan_postmortem_candidates(conn, drafts_dir=missing)
    assert len(candidates) == 1


# ---------------------------------------------------------------------------
# 入库幂等锚点（与 scripts/postmortem.py --confirm 同一主键口径）
# ---------------------------------------------------------------------------


def test_case_library_insert_and_duplicate_detected() -> None:
    conn = connect_memory()
    migrate(conn)
    conn.execute(
        """
        INSERT INTO case_library (
            case_id, code, industry, thesis_type, failure_mode, title, postmortem, tags
        ) VALUES ('swing_pick:sig1', '600000', '银行', 'breakout', '假突破',
                  '标题', '结论', '["breakout"]')
        """
    )
    row = conn.execute(
        "SELECT failure_mode, tags FROM case_library WHERE case_id = 'swing_pick:sig1'"
    ).fetchone()
    assert row[0] == "假突破"
    assert json.loads(row[1]) == ["breakout"]
    exists = conn.execute("SELECT 1 FROM case_library WHERE case_id = 'swing_pick:sig1'").fetchone()
    assert exists is not None  # --confirm 据此幂等跳过
