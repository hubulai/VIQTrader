"""P6 写操作批单测：信号确认闸 / 投委会拍板 / 狩猎清单 CRUD / 参数编辑。

红线断言：确认信号后 order_request 有行且 order_intent 无行——
web 层只写人工确认意图，绝不触发执行链路。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import date
from pathlib import Path

import duckdb
import pytest
from fastapi.testclient import TestClient

from viqtrader.config.loader import Config
from viqtrader.store.config_snapshot import record_snapshot
from viqtrader.store.db import connect
from viqtrader.store.migrate import migrate
from viqtrader.web.app import create_app
from viqtrader.web.services.params import (
    ParamsError,
    changed_top_sections,
    list_snapshots,
    read_user_yaml,
    save_user_yaml,
    validate_user_yaml,
)
from viqtrader.web.services.research import DecideError, decide_session
from viqtrader.web.services.signals import (
    SignalActionError,
    confirm_signal,
    list_signals,
    reject_signal,
)
from viqtrader.web.services.watchlist import (
    WatchlistError,
    add_watch,
    expire_watch,
    list_watchlist,
    reverify_watch,
)

DAY = date(2026, 10, 18)


@pytest.fixture()
def config(tmp_path: Path) -> Config:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "default.yaml").write_text(
        "platform:\n"
        f"  data_dir: {(tmp_path / 'data').as_posix()}\n"
        "  duckdb_file: test.duckdb\n"
        "value_track:\n  watchlist:\n    research_ttl_days: 90\n",
        encoding="utf-8",
    )
    return Config.load(config_dir)


@pytest.fixture()
def conn(config: Config) -> Iterator[duckdb.DuckDBPyConnection]:
    c = connect(config)
    migrate(c)
    yield c
    c.close()


@pytest.fixture()
def seeded_client(
    config: Config,
) -> Callable[[Callable[[duckdb.DuckDBPyConnection], None]], TestClient]:
    """种子后释放写锁再建 TestClient 的工厂（写路由冒烟用）。"""

    def _make(seed: Callable[[duckdb.DuckDBPyConnection], None]) -> TestClient:
        c = connect(config)
        migrate(c)
        seed(c)
        c.close()
        return TestClient(create_app(config))

    return _make


def _seed_base(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute("INSERT INTO account VALUES ('acc_swing', 'swing', 'CNY', 200000, 0.2)")
    conn.execute(
        "INSERT INTO dim_stock (code, market, name, industry, list_date, is_hk_connect) "
        "VALUES ('600519', 'SH', '贵州茅台', '白酒', '2001-08-27', false)"
    )


def _seed_signal(conn: duckdb.DuckDBPyConnection) -> None:
    _seed_base(conn)
    conn.execute(
        "INSERT INTO swing_signal (signal_id, code, market, name, signal_date, "
        "screen_features_json, signal_type, pattern_desc, entry_ref, stop_ref, "
        "target_ref, interpretation, status) VALUES "
        "('sig1', '600519', 'SH', '贵州茅台', '2026-10-17', '{}', 'breakout', "
        "'放量突破', 1690.0, 1650.0, 1800.0, '趋势走强', 'pending')"
    )


def _seed_session(conn: duckdb.DuckDBPyConnection) -> None:
    _seed_base(conn)
    conn.execute("INSERT INTO committee_session (session_id, code) VALUES ('s1', '600519')")


class TestSignalConfirm:
    """6.3 红线：确认 = order_request 有行 + order_intent 无行。"""

    def test_confirm_red_line(self, conn: duckdb.DuckDBPyConnection, config: Config) -> None:
        _seed_signal(conn)
        request_id = confirm_signal(conn, config, "sig1", qty=100, price=1690.0)
        req = conn.execute(
            "SELECT source_type, source_id, account_id, mode, code, side, order_type, "
            "qty, price, status, config_hash FROM order_request WHERE request_id = ?",
            [request_id],
        ).fetchone()
        assert req is not None
        assert tuple(req[:10]) == (
            "swing_signal",
            "sig1",
            "acc_swing",
            "swing",
            "600519",
            "BUY",
            "LIMIT",
            100,
            1690.0,
            "pending",
        )
        assert req[10] == config.content_hash()
        # 红线：web 确认绝不直接产生执行层意图
        assert conn.execute("SELECT count(*) FROM order_intent").fetchone()[0] == 0
        # 同事务：信号状态已翻转
        signals = list_signals(conn, status="confirmed")
        assert len(signals) == 1 and signals[0].signal_id == "sig1"

    def test_confirm_market_without_price(
        self, conn: duckdb.DuckDBPyConnection, config: Config
    ) -> None:
        _seed_signal(conn)
        confirm_signal(conn, config, "sig1", qty=200, price=None, order_type="MARKET")
        row = conn.execute("SELECT order_type, price FROM order_request").fetchone()
        assert row == ("MARKET", None)

    def test_confirm_rejects_invalid_params(
        self, conn: duckdb.DuckDBPyConnection, config: Config
    ) -> None:
        _seed_signal(conn)
        with pytest.raises(SignalActionError, match="正整数"):
            confirm_signal(conn, config, "sig1", qty=0, price=1.0)
        with pytest.raises(SignalActionError, match="正整数"):
            confirm_signal(conn, config, "sig1", qty=-100, price=1.0)
        with pytest.raises(SignalActionError, match="MARKET/LIMIT"):
            confirm_signal(conn, config, "sig1", qty=100, price=1.0, order_type="STOP")
        with pytest.raises(SignalActionError, match="限价"):
            confirm_signal(conn, config, "sig1", qty=100, price=None)
        with pytest.raises(SignalActionError, match="限价"):
            confirm_signal(conn, config, "sig1", qty=100, price=0.0)
        # 全部失败：信号仍 pending，队列为空
        assert len(list_signals(conn)) == 1
        assert conn.execute("SELECT count(*) FROM order_request").fetchone()[0] == 0

    def test_confirm_twice_rejected(self, conn: duckdb.DuckDBPyConnection, config: Config) -> None:
        _seed_signal(conn)
        confirm_signal(conn, config, "sig1", qty=100, price=1690.0)
        with pytest.raises(SignalActionError, match="已处理"):
            confirm_signal(conn, config, "sig1", qty=100, price=1690.0)
        with pytest.raises(SignalActionError, match="不存在"):
            confirm_signal(conn, config, "nope", qty=100, price=1.0)
        # 重复确认未产生重复队列行
        assert conn.execute("SELECT count(*) FROM order_request").fetchone()[0] == 1

    def test_reject_flow(self, conn: duckdb.DuckDBPyConnection) -> None:
        _seed_signal(conn)
        reject_signal(conn, "sig1")
        assert list_signals(conn) == []
        assert len(list_signals(conn, status="rejected")) == 1
        with pytest.raises(SignalActionError, match="已处理"):
            reject_signal(conn, "sig1")
        with pytest.raises(SignalActionError, match="不存在"):
            reject_signal(conn, "nope")


class TestCommitteeDecide:
    def test_decide_once(self, conn: duckdb.DuckDBPyConnection) -> None:
        _seed_session(conn)
        decide_session(conn, "s1", decision="买入", notes="估值方假设更保守")
        row = conn.execute(
            "SELECT final_decision, decided_by, CAST(decided_at AS TEXT), notes "
            "FROM committee_session WHERE session_id = 's1'"
        ).fetchone()
        assert row is not None
        assert row[0] == "买入" and row[1] == "user" and row[2] is not None
        assert row[3] == "估值方假设更保守"
        # 拍板仅存档：不产生任何下单意图
        assert conn.execute("SELECT count(*) FROM order_request").fetchone()[0] == 0

    def test_decide_twice_rejected(self, conn: duckdb.DuckDBPyConnection) -> None:
        _seed_session(conn)
        decide_session(conn, "s1", decision="观望")
        with pytest.raises(DecideError, match="已拍板"):
            decide_session(conn, "s1", decision="买入")
        with pytest.raises(DecideError, match="不存在"):
            decide_session(conn, "nope", decision="买入")
        with pytest.raises(DecideError, match="不能为空"):
            decide_session(conn, "s1", decision="  ")


class TestWatchlistWrite:
    def test_add_and_reverify(self, conn: duckdb.DuckDBPyConnection) -> None:
        _seed_base(conn)
        watch_id = add_watch(conn, code="600519", target=1500.0, summary="好公司")
        rows = list_watchlist(conn, today=date.today())  # last_review_date=CURRENT_DATE
        assert len(rows) == 1
        assert rows[0].status == "watching"
        assert rows[0].ttl_days_left == 90  # last_review_date=今天
        reverify_watch(conn, watch_id, target=1450.0)
        row = conn.execute(
            "SELECT status, target_buy_price FROM watchlist WHERE watch_id = ?",
            [watch_id],
        ).fetchone()
        assert row == ("watching", 1450.0)

    def test_add_invalid(self, conn: duckdb.DuckDBPyConnection) -> None:
        with pytest.raises(WatchlistError, match="不能为空"):
            add_watch(conn, code="  ", target=100.0)
        with pytest.raises(WatchlistError, match="正数"):
            add_watch(conn, code="600519", target=0.0)
        with pytest.raises(WatchlistError, match="区间"):
            add_watch(conn, code="600519", target=100.0, required_discount=1.5)
        assert conn.execute("SELECT count(*) FROM watchlist").fetchone()[0] == 0

    def test_expire_flow(self, conn: duckdb.DuckDBPyConnection) -> None:
        _seed_base(conn)
        watch_id = add_watch(conn, code="600519", target=1500.0)
        expire_watch(conn, watch_id)
        rows = list_watchlist(conn, today=DAY, status="expired")
        assert len(rows) == 1
        with pytest.raises(WatchlistError, match="已是 expired"):
            expire_watch(conn, watch_id)
        with pytest.raises(WatchlistError, match="不存在"):
            expire_watch(conn, "nope")
        with pytest.raises(WatchlistError, match="不存在"):
            reverify_watch(conn, "nope")
        # 证伪后可复核恢复
        reverify_watch(conn, watch_id)
        assert list_watchlist(conn, today=DAY, status="watching")[0].watch_id == watch_id


class TestParamsService:
    def test_validate_and_save(self, config: Config) -> None:
        assert read_user_yaml(config) == ""
        with pytest.raises(ParamsError, match="语法错误"):
            validate_user_yaml(config, "platform: [unclosed")
        with pytest.raises(ParamsError, match="mapping"):
            validate_user_yaml(config, "- just\n- a\n- list\n")
        changed = save_user_yaml(config, "value_track:\n  watchlist:\n    research_ttl_days: 60\n")
        assert changed == ["value_track"]
        assert "research_ttl_days: 60" in read_user_yaml(config)
        # 热加载生效
        assert config.get("value_track.watchlist.research_ttl_days") == 60
        # 非法文本不破坏已保存文件
        with pytest.raises(ParamsError):
            save_user_yaml(config, "bad: [yaml")
        assert "research_ttl_days: 60" in read_user_yaml(config)

    def test_changed_top_sections(self) -> None:
        old = "a: 1\nb: 2\n"
        new = "a: 1\nb: 3\nc: 4\n"
        assert changed_top_sections(old, new) == ["b", "c"]
        # 一侧解析失败按空映射处理：另一侧全部顶层节视为变更
        assert changed_top_sections("bad: [", new) == ["a", "b", "c"]

    def test_list_snapshots(self, conn: duckdb.DuckDBPyConnection, config: Config) -> None:
        assert list_snapshots(conn, config) == []
        assert record_snapshot(conn, config, note="初始") is True
        rows = list_snapshots(conn, config)
        assert len(rows) == 1
        assert rows[0].is_current and rows[0].note == "初始"
        # 改 user.yaml → 新快照为非当前需重载后判定
        save_user_yaml(config, "value_track:\n  watchlist:\n    research_ttl_days: 60\n")
        fresh = Config.load(config.config_dir)
        assert record_snapshot(conn, fresh, note="web 参数编辑") is True
        rows = list_snapshots(conn, config)
        assert len(rows) == 2
        assert sum(1 for r in rows if r.is_current) == 1


class TestWriteRoutes:
    def test_confirm_route_red_line(
        self,
        config: Config,
        seeded_client: Callable[..., TestClient],
    ) -> None:
        client = seeded_client(_seed_signal)
        resp = client.post(
            "/signals/sig1/confirm",
            data={"qty": "100", "price": "1690", "order_type": "LIMIT"},
        )
        assert resp.status_code == 200  # 303 → 重定向后列表页
        assert "已确认" in resp.text
        check = connect(config, read_only=True)
        try:
            assert check.execute("SELECT count(*) FROM order_request").fetchone()[0] == 1
            assert check.execute("SELECT count(*) FROM order_intent").fetchone()[0] == 0
        finally:
            check.close()

    def test_confirm_route_error_flash(self, seeded_client: Callable[..., TestClient]) -> None:
        client = seeded_client(_seed_signal)
        resp = client.post(
            "/signals/sig1/confirm",
            data={"qty": "abc", "price": "", "order_type": "LIMIT"},
        )
        assert resp.status_code == 200
        assert "格式非法" in resp.text

    def test_signals_page(self, seeded_client: Callable[..., TestClient]) -> None:
        client = seeded_client(_seed_signal)
        resp = client.get("/signals")
        assert resp.status_code == 200
        assert "贵州茅台" in resp.text and "确认" in resp.text

    def test_decide_route(self, seeded_client: Callable[..., TestClient]) -> None:
        client = seeded_client(_seed_session)
        resp = client.post(
            "/research/sessions/s1/decide",
            data={"decision": "买入", "notes": "保守假设"},
        )
        assert resp.status_code == 200
        assert "已拍板" in resp.text
        # 重复拍板 → 错误提示
        resp2 = client.post("/research/sessions/s1/decide", data={"decision": "否决"})
        assert "已拍板" in resp2.text and "不可重复" in resp2.text

    def test_watchlist_add_route(
        self,
        config: Config,
        seeded_client: Callable[..., TestClient],
    ) -> None:
        client = seeded_client(_seed_base)
        resp = client.post(
            "/watchlist/add",
            data={"code": "600519", "target": "1500", "summary": "好公司"},
        )
        assert resp.status_code == 200
        assert "已加入狩猎清单" in resp.text
        check = connect(config, read_only=True)
        try:
            assert check.execute(
                "SELECT status FROM watchlist WHERE code = '600519'"
            ).fetchone() == ("watching",)
        finally:
            check.close()

    def test_watchlist_expire_route(self, seeded_client: Callable[..., TestClient]) -> None:
        def _seed(conn: duckdb.DuckDBPyConnection) -> None:
            _seed_base(conn)
            conn.execute(
                "INSERT INTO watchlist (watch_id, code, target_buy_price, status, "
                "last_review_date) VALUES ('w1', '600519', 1500, 'watching', '2026-10-01')"
            )

        client = seeded_client(_seed)
        resp = client.post("/watchlist/w1/expire")
        assert resp.status_code == 200
        assert "证伪" in resp.text

    def test_params_page_and_save(
        self,
        config: Config,
        seeded_client: Callable[..., TestClient],
    ) -> None:
        client = seeded_client(_seed_base)
        resp = client.get("/params")
        assert resp.status_code == 200 and "参数编辑" in resp.text
        bad = client.post("/params", data={"text": "bad: [yaml"})
        assert "语法错误" in bad.text
        good = client.post(
            "/params", data={"text": "value_track:\n  watchlist:\n    research_ttl_days: 60\n"}
        )
        assert good.status_code == 200
        assert "已保存" in good.text and "新快照已入库" in good.text
        assert "value_track" in good.text  # 影响提示
        check = connect(config, read_only=True)
        try:
            assert check.execute("SELECT count(*) FROM config_snapshot").fetchone()[0] == 1
        finally:
            check.close()
