"""跑一次真实投委会（P4.4）。

用法（仓库根目录）：
    .venv\\Scripts\\python.exe scripts\\run_committee.py --code 600519

前置条件：本地 DuckDB 已含该标的的财务/估值数据（QMT 终端同步，P1.6）。
真实 LLM 响应自动以盒带归档到 tests/fixtures/llm_cassettes/committee/，
重跑同日会话命中指纹即离线重放，零成本可复现。
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from viqtrader.agents.llm.cassette import CassetteProvider  # noqa: E402
from viqtrader.agents.llm.openai_compat import OpenAICompatProvider  # noqa: E402
from viqtrader.agents.shared.db_lease import file_lease  # noqa: E402
from viqtrader.agents.value.committee import run_committee  # noqa: E402
from viqtrader.config.loader import Config  # noqa: E402
from viqtrader.store.db import connect  # noqa: E402
from viqtrader.store.migrate import migrate  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="运行一次投委会（估值方 + 反方）")
    parser.add_argument("--code", default="600519", help="标的代码（默认 600519 贵州茅台）")
    parser.add_argument("--name", default="贵州茅台")
    parser.add_argument("--market", default="SH")
    parser.add_argument(
        "--peer-codes", default="", help="同业对比代码，逗号分隔（如 000858,601088）"
    )
    args = parser.parse_args()

    config = Config.load(REPO_ROOT / "config")
    conn = connect(config)
    migrate(conn)
    try:
        n_financial = conn.execute(
            "SELECT COUNT(*) FROM fact_financial WHERE code = ?", [args.code]
        ).fetchone()[0]
    finally:
        # 关键：预检后立即释放文件锁，LLM 阶段不得持有读写连接（否则 Web 控制台 503）
        conn.close()

    if n_financial == 0:
        print(
            f"库中无 {args.code} 财务数据，请先完成数据同步（QMT 终端，P1.6）。"
            "投委会拒绝在无财务输入的情况下产出估值结论。",
            file=sys.stderr,
        )
        return 2

    peer_codes = tuple(c.strip() for c in args.peer_codes.split(",") if c.strip())
    inner = OpenAICompatProvider.from_config(config)
    case = f"{args.code}_{date.today().isoformat()}"
    provider = CassetteProvider(
        inner,
        cassette_dir=REPO_ROOT / "tests" / "fixtures" / "llm_cassettes",
        agent="committee",
        case=case,
        mode="record",
    )

    summary = run_committee(
        lambda: file_lease(config),  # 读/写段各自短连接，LLM 段不持锁
        config,
        provider,
        code=args.code,
        name=args.name,
        market=args.market,
        peer_codes=peer_codes,
    )

    print(f"会话 ID: {summary.session_id}")
    print(f"估值方: {summary.valuation_status}  反方: {summary.dissent_status}")
    print(f"中止: {summary.aborted}")
    print(f"备注: {summary.notes}")
    print(f"盒带: {provider.path}")
    return 0 if not summary.aborted else 1


if __name__ == "__main__":
    raise SystemExit(main())
