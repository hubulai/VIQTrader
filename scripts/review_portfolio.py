"""跑一次组合复盘（P4.6）：Brinson 归因定量分解 → 三段 LLM 定性解读。

用法（仓库根目录）：
    .venv\\Scripts\\python.exe scripts\\review_portfolio.py \\
        --account acc_value --start 2026-07-01 --end 2026-09-30
    .venv\\Scripts\\python.exe scripts\\review_portfolio.py \\
        --account acc_swing --start 2026-07-01 --end 2026-09-30 --benchmark 000300

三段相互独立、失败隔离：
  1. 归因段：账本空 / 基准 universe 缺市值时为 unavailable（跳过，不算失败）；
  2. thesis 段：仅复核关联了 active thesis 的期初持仓，其余跳过留痕；
  3. 风控段：区间无 risk_event 时为 empty（正常）。

真实 LLM 响应以盒带归档到 tests/fixtures/llm_cassettes/review_portfolio/，
同账户同区间重跑命中指纹即离线重放，零成本可复现。
退出码：任一段 failed 返回 1（供运维感知），其余情况 0。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import duckdb  # noqa: E402

from viqtrader.agents.llm.cassette import CassetteProvider  # noqa: E402
from viqtrader.agents.llm.openai_compat import OpenAICompatProvider  # noqa: E402
from viqtrader.agents.shared.review.pipeline import (  # noqa: E402
    ReviewPipelineResult,
    run_review_pipeline,
)
from viqtrader.config.loader import Config  # noqa: E402
from viqtrader.store.db import connect  # noqa: E402
from viqtrader.store.migrate import migrate  # noqa: E402


def _load_output_json(conn: duckdb.DuckDBPyConnection, run_id: str | None) -> dict | None:
    """按 run_id 取 SUCCESS 落库的结构化输出（展示层用，不改变 pipeline 契约）。"""
    if not run_id:
        return None
    row = conn.execute("SELECT output_json FROM agent_output WHERE run_id = ?", [run_id]).fetchone()
    if row is None:
        return None
    return json.loads(row[0])


def _print_attribution(conn: duckdb.DuckDBPyConnection, result: ReviewPipelineResult) -> None:
    sec = result.attribution
    print("=== 一、Brinson 归因 + 定性解读 ===")
    if sec.status == "unavailable":
        print("  [跳过] 账本为空或基准 universe 不可用（缺行情/市值），本期无归因对象。")
        return
    if sec.active_return is not None:
        print(f"  定量：区间超额收益 active_return = {sec.active_return:+.2%}")
    if sec.status == "failed":
        print(f"  [失败] 定性解读未成功（{sec.agent_status}）：{sec.error}")
        return

    payload = _load_output_json(conn, sec.run_id)
    if payload is None:
        print(f"  [失败] 解读输出缺失，run_id={sec.run_id}")
        return
    print(f"  摘要：{payload['summary']}")
    print(f"  行业配置效应：{payload['allocation_comment']}")
    print(f"  个股选择效应：{payload['selection_comment']}")
    print("  关键发现：")
    for finding in payload["key_findings"]:
        print(f"    - {finding}")


def _print_thesis(conn: duckdb.DuckDBPyConnection, result: ReviewPipelineResult) -> None:
    print("\n=== 二、逐持仓 thesis 复核 ===")
    if not result.thesis_reviews:
        print("  期初无持仓。")
        return
    for item in result.thesis_reviews:
        if item.status == "success":
            head = (
                f"  {item.code} [成功] thesis={item.thesis_status} → 建议 {item.suggested_action}"
            )
            print(head)
            payload = _load_output_json(conn, item.run_id)
            if payload:
                for change in payload.get("changes", []):
                    print(f"    变化：{change}")
                print(f"    推理：{payload['reasoning']}")
        elif item.status == "skipped":
            print(f"  {item.code} [跳过] {item.reason}")
        else:
            print(f"  {item.code} [失败] run_id={item.run_id}：{item.reason}")


def _print_risk(conn: duckdb.DuckDBPyConnection, result: ReviewPipelineResult) -> None:
    sec = result.risk
    print("\n=== 三、风控事件解读 ===")
    if sec.status == "empty":
        print("  区间无风控事件。")
        return
    if sec.status == "failed":
        print(f"  [失败] 风控解读未成功（{sec.agent_status}）：{sec.error}")
        return
    payload = _load_output_json(conn, sec.run_id)
    print(f"  区间风控事件 {sec.event_count} 条，综合风险等级：{payload['overall_risk']}")
    for item in payload["items"]:
        print(f"    - {item['rule_key']}：{item['interpretation']}")
        print(f"      建议动作：{item['suggested_action']}")
    print(f"  总结：{payload['summary']}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="运行组合复盘：Brinson 归因 + thesis 复核 + 风控解读"
    )
    parser.add_argument("--account", required=True, help="子账本 id（acc_value / acc_swing）")
    parser.add_argument(
        "--start", required=True, type=date.fromisoformat, help="区间起日 YYYY-MM-DD"
    )
    parser.add_argument("--end", required=True, type=date.fromisoformat, help="区间止日 YYYY-MM-DD")
    parser.add_argument(
        "--benchmark",
        default=None,
        help="指数参考基准代码（默认读 attribution.benchmark_code，缺省 000300）",
    )
    args = parser.parse_args()

    if args.start > args.end:
        print(f"区间非法：start={args.start} 晚于 end={args.end}", file=sys.stderr)
        return 2

    config = Config.load(REPO_ROOT / "config")
    benchmark_code = args.benchmark or str(config.get("attribution.benchmark_code", "000300"))

    conn = connect(config)
    migrate(conn)

    account_exists = conn.execute(
        "SELECT 1 FROM account WHERE account_id = ?", [args.account]
    ).fetchone()
    if account_exists is None:
        print(f"账户不存在: {args.account}", file=sys.stderr)
        return 2

    inner = OpenAICompatProvider.from_config(config)
    case = f"{args.account}_{args.start.isoformat()}_{args.end.isoformat()}"
    provider = CassetteProvider(
        inner,
        cassette_dir=REPO_ROOT / "tests" / "fixtures" / "llm_cassettes",
        agent="review_portfolio",
        case=case,
        mode="record",
    )

    print(f"复盘账户 {args.account}，区间 {args.start} ~ {args.end}，基准 {benchmark_code}\n")
    result = run_review_pipeline(
        conn,
        config,
        provider,
        account_id=args.account,
        start=args.start,
        end=args.end,
        benchmark_code=benchmark_code,
    )

    _print_attribution(conn, result)
    _print_thesis(conn, result)
    _print_risk(conn, result)

    if result.notes:
        print("\n=== 数据质量备注 ===")
        for note in result.notes:
            print(f"  - {note}")

    print(f"\n盒带：{provider.path}")

    has_failure = (
        result.attribution.status == "failed"
        or any(i.status == "failed" for i in result.thesis_reviews)
        or result.risk.status == "failed"
    )
    if has_failure:
        print(
            "存在失败段（详见上方），请检查 LLM/契约后重跑（盒带命中可离线重试）。", file=sys.stderr
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
