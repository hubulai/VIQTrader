"""P4.5 波段流水线端到端测试（内存库 + FakeProvider，全离线）。

覆盖：全链落库、none 不落库、选股 DEGRADED 不跑解读、解读 DEGRADED 保留半程、
单候选失败隔离、空结果空跑、(code, signal_date) 幂等。
"""

from __future__ import annotations

import json
from datetime import date, timedelta

from viqtrader.agents.llm.fake import FakeProvider
from viqtrader.agents.swing.pipeline import run_swing_pipeline


def _seed_breakout(conn, code: str, name: str, market: str = "SH") -> None:
    conn.execute(
        "INSERT INTO dim_stock (code, market, name, industry, list_date, is_hk_connect) "
        "VALUES (?, ?, ?, NULL, '2000-01-01', false)",
        [code, market, name],
    )
    base = date(2024, 1, 1)
    for i in range(60):
        close = 10.5 if i == 59 else 10.0
        volume = 20_000_000 if i == 59 else 10_000_000
        conn.execute(
            """
            INSERT INTO fact_daily_bar
                (code, trade_date, open, high, low, close, volume, amount, adj_factor)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1.0)
            """,
            [
                code,
                base + timedelta(days=i),
                close,
                close + 0.05,
                close - 0.05,
                close,
                volume,
                close * volume,
            ],
        )


def _pick_json(signal_type: str = "breakout") -> str:
    return json.dumps(
        {
            "code": "600001",
            "signal_type": signal_type,
            "pattern_desc": "收盘创 20 日新高，量比 2.0",
            "entry_ref": 10.2,
            "stop_ref": 9.96,
            "target_ref": 10.5,
            "risks": ["假突破风险"],
        },
        ensure_ascii=False,
    )


_INTERPRET_JSON = json.dumps(
    {
        "interpretation": "这是放量突破形态，右侧机会，参考位用于风控。",
        "invalidation_conditions": ["收盘跌回 10.0 突破位下方", "跌破 9.96"],
        "key_risks": ["假突破", "大盘系统性回调"],
        "win_rate_note": "暂无历史样本，胜率待积累",
    },
    ensure_ascii=False,
)


def _provider(pick_content: str = "", interp_content: str = "") -> FakeProvider:
    pick_content = pick_content or _pick_json()
    interp_content = interp_content or _INTERPRET_JSON

    def respond(messages, model, params):
        head = messages[0].content
        if "信号解读 Agent" in head:
            return interp_content
        return pick_content

    return FakeProvider(respond)


def test_full_pipeline_persists_pending_signal(conn, config) -> None:
    _seed_breakout(conn, "600001", "突破股份")
    provider = _provider()

    results = run_swing_pipeline(conn, config, provider)
    assert len(results) == 1
    r = results[0]
    assert r.status == "signal"
    assert r.signal_type == "breakout"
    assert r.signal_id and r.picker_run_id and r.interpreter_run_id

    row = conn.execute(
        "SELECT code, signal_type, entry_ref, stop_ref, target_ref, interpretation, "
        "status, picker_run_id, interpreter_run_id, screen_features_json "
        "FROM swing_signal WHERE signal_id = ?",
        [r.signal_id],
    ).fetchone()
    assert row[0] == "600001"
    assert row[1] == "breakout"
    assert (row[2], row[3], row[4]) == (10.2, 9.96, 10.5)
    assert "放量突破" in row[5]
    assert row[6] == "pending"  # 建议层：等 P6 人工确认
    features = json.loads(row[9])
    assert features["patterns"] == ["breakout"]
    assert len(provider.calls) == 2  # 选股 + 解读各一次


def test_none_signal_not_persisted_and_interpreter_skipped(conn, config) -> None:
    _seed_breakout(conn, "600001", "突破股份")
    provider = _provider(pick_content=_pick_json("none"))

    results = run_swing_pipeline(conn, config, provider)
    assert results[0].status == "none"
    assert conn.execute("SELECT count(*) FROM swing_signal").fetchone()[0] == 0
    assert len(provider.calls) == 1  # 只调了选股
    assert "信号解读 Agent" not in provider.calls[0][0][0].content


def test_picker_degraded_skips_interpret_and_persists_nothing(conn, config) -> None:
    _seed_breakout(conn, "600001", "突破股份")
    provider = _provider(pick_content="这不是JSON", interp_content=_INTERPRET_JSON)

    results = run_swing_pipeline(conn, config, provider)
    assert results[0].status == "picker_failed"
    assert results[0].picker_run_id  # agent_run 仍留痕
    assert conn.execute("SELECT count(*) FROM swing_signal").fetchone()[0] == 0
    # 初次 + 契约纠错重试 = 2 次，且全是选股（无解读调用）
    assert len(provider.calls) == 2
    assert all("信号解读 Agent" not in c[0][0].content for c in provider.calls)
    assert conn.execute("SELECT count(*) FROM agent_output").fetchone()[0] == 0


def test_interpreter_degraded_keeps_signal_partial(conn, config) -> None:
    _seed_breakout(conn, "600001", "突破股份")
    provider = _provider(interp_content="坏响应")

    results = run_swing_pipeline(conn, config, provider)
    assert results[0].status == "signal_partial"
    row = conn.execute(
        "SELECT signal_type, interpretation, interpreter_run_id FROM swing_signal"
    ).fetchone()
    assert row[0] == "breakout"
    assert row[1] is None  # 解读缺失留空，选股结论仍可见
    assert row[2] is not None  # 失败 run 也有留痕 id


def test_single_candidate_failure_isolated(conn, config) -> None:
    _seed_breakout(conn, "600001", "异常股份")
    _seed_breakout(conn, "000002", "正常股份", market="SZ")

    def respond(messages, model, params):
        head = messages[0].content
        if "信号解读 Agent" in head:
            return _INTERPRET_JSON
        if "600001" in head:
            raise RuntimeError("模拟 LLM 爆炸")
        return _pick_json().replace("600001", "000002")

    provider = FakeProvider(respond)
    results = run_swing_pipeline(conn, config, provider)
    by_code = {r.code: r for r in results}
    assert by_code["600001"].status == "picker_failed"
    assert by_code["000002"].status == "signal"
    rows = conn.execute("SELECT code FROM swing_signal").fetchall()
    assert rows == [("000002",)]


def test_empty_screen_runs_nothing(conn, config) -> None:
    provider = _provider()
    assert run_swing_pipeline(conn, config, provider) == []
    assert provider.calls == []


def test_duplicate_signal_date_is_idempotent(conn, config) -> None:
    _seed_breakout(conn, "600001", "突破股份")
    provider = _provider()

    first = run_swing_pipeline(conn, config, provider)
    assert first[0].status == "signal"
    second = run_swing_pipeline(conn, config, provider)
    assert second[0].status == "duplicate"
    assert conn.execute("SELECT count(*) FROM swing_signal").fetchone()[0] == 1
    assert len(provider.calls) == 2  # 第二次零 LLM 调用
