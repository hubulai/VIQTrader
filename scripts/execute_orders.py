"""order_request 消费 CLI（P6 执行侧，拍板③）。

人工确认闸唯一消费方：扫描 order_request.pending → ExecutionService 全链留痕
（规则校验/DryRun 撮合/账本）→ 回写请求终态。三处复用同一 consume 实现：
每日 DAG 进程内任务 / 控制台「立即执行」subprocess / 本命令行。

用法（仓库根目录）：
    .venv\\Scripts\\python.exe scripts\\execute_orders.py
    .venv\\Scripts\\python.exe scripts\\execute_orders.py --date 2026-10-17

退出码：0 正常消费（含业务拒绝）；1 执行异常；2 参数非法/无行情。
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from viqtrader.config.loader import Config  # noqa: E402
from viqtrader.engine.execution.consume import (  # noqa: E402
    consume_order_requests,
    latest_trade_date,
)
from viqtrader.store.db import connect  # noqa: E402
from viqtrader.store.migrate import migrate  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="order_request 人工确认队列消费（P6 执行侧）")
    parser.add_argument("--date", default=None, help="成交参考日 YYYY-MM-DD（默认库内最新交易日）")
    parser.add_argument(
        "--config-dir", default=str(REPO_ROOT / "config"), help="配置目录（默认仓库 config/）"
    )
    args = parser.parse_args()

    trade_date: date | None
    if args.date:
        try:
            trade_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            print(f"--date 格式非法（应为 YYYY-MM-DD）: {args.date!r}", file=sys.stderr)
            return 2

    config = Config.load(Path(args.config_dir))
    conn = connect(config)
    migrate(conn)
    try:
        if not args.date:
            trade_date = latest_trade_date(conn)
        if trade_date is None:
            print("fact_daily_bar 为空，无法确定成交参考日", file=sys.stderr)
            return 2
        report = consume_order_requests(conn, config, trade_date)
    finally:
        conn.close()

    if report.accounts_initialized:
        print(f"初始化账户：{', '.join(report.accounts_initialized)}（按 config 配额建账）")
    for o in report.outcomes:
        print(f"  {o.request_id[:8]} {o.code} {o.side} x{o.qty} → {o.result}（{o.detail}）")
    print(
        f"消费完成（成交参考日 {report.trade_date}）：共 {len(report.outcomes)} 笔，"
        f"成交 {report.executed}、拒绝 {report.rejected}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
