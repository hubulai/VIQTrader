"""狩猎清单人工管理 CLI（P5.2）。

P6 Web 控制台上线前的人工 CRUD 入口：投委会（run_committee.py）只产出研究
结论，需人工拍板「加入狩猎清单并设目标价」后落库。控制台上线后本 CLI 可
保留作运维备选。

子命令：
  add       加入狩猎清单（status=watching，last_review_date=today）
  list      查看清单及状态、距过期天数、最新价距目标价差
  expire    人工标记 thesis 证伪 → expired
  reverify  重新投委会后恢复 watching，更新 last_review_date/目标价

用法（仓库根目录）：
    .venv\\Scripts\\python.exe scripts\\watchlist.py add --code 600519 --target 1500
    .venv\\Scripts\\python.exe scripts\\watchlist.py list
    .venv\\Scripts\\python.exe scripts\\watchlist.py expire --watch-id <id>
    .venv\\Scripts\\python.exe scripts\\watchlist.py reverify --watch-id <id> --target 1450
"""

from __future__ import annotations

import argparse
import sys
import uuid
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from viqtrader.config.loader import Config  # noqa: E402
from viqtrader.store.db import connect  # noqa: E402
from viqtrader.store.migrate import migrate  # noqa: E402


def _today() -> date:
    return date.today()


def cmd_add(args: argparse.Namespace, conn) -> int:
    target = float(args.target)
    if target <= 0:
        print(f"target_buy_price 必须为正数，收到 {target}", file=sys.stderr)
        return 2
    if args.required_discount is not None and not (0 < float(args.required_discount) < 1):
        print("required_discount 必须在 (0,1) 区间", file=sys.stderr)
        return 2

    watch_id = str(uuid.uuid4())
    today = _today()
    conn.execute(
        """
        INSERT INTO watchlist
        (watch_id, code, committee_session_id, target_buy_price, required_discount,
         status, research_summary, last_review_date, thesis_id)
        VALUES (?, ?, ?, ?, ?, 'watching', ?, ?, ?)
        """,
        [
            watch_id,
            args.code,
            args.committee_session,
            target,
            float(args.required_discount) if args.required_discount else None,
            args.summary,
            today,
            args.thesis_id,
        ],
    )
    print(f"已加入狩猎清单：watch_id={watch_id} code={args.code} target={target} status=watching")
    return 0


def cmd_list(args: argparse.Namespace, conn, ttl_days: int = 90) -> int:
    where = "WHERE status = ?" if args.status else ""
    params = [args.status] if args.status else []
    rows = conn.execute(
        f"""
        SELECT w.watch_id, w.code, w.status, w.target_buy_price, w.last_review_date,
               b.close AS latest_close
        FROM watchlist w
        LEFT JOIN (
            SELECT code, close FROM fact_daily_bar
            WHERE trade_date = (SELECT max(trade_date) FROM fact_daily_bar)
        ) b ON b.code = w.code
        {where}
        ORDER BY w.status, w.code
        """,
        params,
    ).fetchall()

    if not rows:
        print("（狩猎清单为空）")
        return 0

    today = _today()
    header = (
        f"{'watch_id':<38} {'code':<8} {'status':<10} "
        f"{'target':>10} {'latest':>10} {'gap%':>8} {'ttl':>5}"
    )
    print(header)
    print("-" * 95)
    for r in rows:
        watch_id, code, status, target, last_review, latest_close = r
        gap = ""
        if latest_close is not None and target:
            gap_pct = (latest_close - target) / target * 100
            gap = f"{gap_pct:+.1f}%"
        ttl = ""
        if last_review is not None:
            days_left = ttl_days - (today - last_review).days
            ttl = f"{days_left}d"
        wid = watch_id[:8] if len(watch_id) > 8 else watch_id
        print(
            f"{wid:<38} {code:<8} {status:<10} "
            f"{target:>10.2f} "
            f"{(f'{latest_close:.2f}' if latest_close else '—'):>10} "
            f"{gap:>8} {ttl:>5}"
        )
    print(f"\n共 {len(rows)} 条")
    return 0


def cmd_expire(args: argparse.Namespace, conn) -> int:
    cur = conn.execute(
        "UPDATE watchlist SET status = 'expired' WHERE watch_id = ? AND status <> 'expired'",
        [args.watch_id],
    )
    if cur.rowcount == 0:
        row = conn.execute(
            "SELECT status FROM watchlist WHERE watch_id = ?", [args.watch_id]
        ).fetchone()
        if row is None:
            print(f"watch_id 不存在：{args.watch_id}", file=sys.stderr)
            return 2
        print(f"已是 expired 状态：{args.watch_id}")
        return 0
    print(f"已标记过期：{args.watch_id}")
    return 0


def cmd_reverify(args: argparse.Namespace, conn) -> int:
    today = _today()
    sets = ["status = 'watching'", "last_review_date = ?"]
    params: list = [today]
    if args.target is not None:
        target = float(args.target)
        if target <= 0:
            print("target 必须为正数", file=sys.stderr)
            return 2
        sets.append("target_buy_price = ?")
        params.append(target)
    params.append(args.watch_id)
    cur = conn.execute(
        f"UPDATE watchlist SET {', '.join(sets)} WHERE watch_id = ?",
        params,
    )
    if cur.rowcount == 0:
        print(f"watch_id 不存在：{args.watch_id}", file=sys.stderr)
        return 2
    print(f"已恢复 watching：{args.watch_id}（last_review_date={today}）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="狩猎清单人工管理（P5.2）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="加入狩猎清单")
    p_add.add_argument("--code", required=True)
    p_add.add_argument("--target", required=True, help="安全边际买入价（绝对价）")
    p_add.add_argument("--committee-session", default=None)
    p_add.add_argument("--summary", default=None, help="研究结论摘要")
    p_add.add_argument("--thesis-id", default=None)
    p_add.add_argument("--required-discount", default=None, help="要求折价率 0~1")

    p_list = sub.add_parser("list", help="查看狩猎清单")
    p_list.add_argument("--status", choices=["watching", "triggered", "expired"], default=None)

    p_expire = sub.add_parser("expire", help="标记 thesis 证伪 → expired")
    p_expire.add_argument("--watch-id", required=True)

    p_reverify = sub.add_parser("reverify", help="重新投委会后恢复 watching")
    p_reverify.add_argument("--watch-id", required=True)
    p_reverify.add_argument("--target", default=None, help="更新目标价（可选）")

    args = parser.parse_args()

    config = Config.load(REPO_ROOT / "config")
    conn = connect(config)
    migrate(conn)
    ttl_days = int(config.get("value_track.watchlist.research_ttl_days", 90))

    try:
        if args.cmd == "add":
            return cmd_add(args, conn)
        if args.cmd == "list":
            return cmd_list(args, conn, ttl_days)
        if args.cmd == "expire":
            return cmd_expire(args, conn)
        if args.cmd == "reverify":
            return cmd_reverify(args, conn)
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
