"""跑一次波段流水线：全市场确定性初筛 → 选股 Agent → 解读 Agent → swing_signal（P4.5）。

用法（仓库根目录）：
    .venv\\Scripts\\python.exe scripts\\run_swing_pipeline.py
    .venv\\Scripts\\python.exe scripts\\run_swing_pipeline.py --limit 3
    .venv\\Scripts\\python.exe scripts\\run_swing_pipeline.py \
        --strategy trend_momentum --param volume_ratio_min=2.0

前置条件：本地 DuckDB 已含较全的日 K 数据（fact_daily_bar，QMT 终端同步）。
真实 LLM 响应自动以盒带归档到 tests/fixtures/llm_cassettes/swing/，
重跑同日命中指纹即离线重放，零成本可复现。

产物均为建议（swing_signal.status=pending），不产 OrderIntent、不进生命周期，
等待 P6 人工确认后才进入跟踪流程。
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
from viqtrader.agents.swing.pipeline import run_swing_pipeline  # noqa: E402
from viqtrader.agents.swing.screen import screen_candidates  # noqa: E402
from viqtrader.config.loader import Config  # noqa: E402
from viqtrader.store.db import connect  # noqa: E402
from viqtrader.store.migrate import migrate  # noqa: E402


def _parse_kv_pairs(pairs: list[str]) -> dict[str, str]:
    """解析 --param k=v 列表为 dict；值保持字符串，类型强转由策略 schema 完成。"""
    result: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"--param 格式错误（应为 k=v）: {pair!r}")
        key, _, value = pair.partition("=")
        result[key.strip()] = value.strip()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="运行波段选股 + 信号解读流水线")
    parser.add_argument(
        "--limit", type=int, default=None, help="覆盖初筛候选上限（默认读配置 screen_top_n）"
    )
    parser.add_argument(
        "--strategy",
        default=None,
        help="初筛策略 id（默认读配置 swing_track.active_strategy）",
    )
    parser.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="k=v",
        help="临时覆盖策略参数（可重复），如 --param volume_ratio_min=2.0",
    )
    args = parser.parse_args()

    params_override = _parse_kv_pairs(args.param)

    config = Config.load(REPO_ROOT / "config")
    conn = connect(config)
    migrate(conn)

    latest = conn.execute("SELECT max(trade_date), count(*) FROM fact_daily_bar").fetchone()
    if latest[0] is None:
        print(
            "库中 fact_daily_bar 无任何日 K 数据，请先完成数据同步（QMT 终端）。", file=sys.stderr
        )
        return 2
    print(f"日 K 最新交易日: {latest[0]}，总行数 {latest[1]:,}")

    candidates = screen_candidates(
        conn, config, strategy_id=args.strategy, params_override=params_override, top_n=args.limit
    )
    if not candidates:
        print("初筛无候选（可能是数据稀疏或当日无形态命中），流水线空跑结束。")
        return 0
    print(f"初筛策略: {candidates[0].strategy_id}")
    print(f"初筛候选 {len(candidates)} 只：")
    for c in candidates:
        print(f"  {c.code} {c.name}  分值={c.score}  形态={','.join(c.patterns)}")

    inner = OpenAICompatProvider.from_config(config)
    provider = CassetteProvider(
        inner,
        cassette_dir=REPO_ROOT / "tests" / "fixtures" / "llm_cassettes",
        agent="swing",
        case=f"swing_{date.today().isoformat()}",
        mode="record",
    )

    results = run_swing_pipeline(
        conn,
        config,
        provider,
        strategy_id=args.strategy,
        params_override=params_override,
        top_n=args.limit,
    )
    print("\n处理结果：")
    for r in results:
        line = f"  [{r.status}] {r.code} {r.name}"
        if r.signal_type:
            line += f"  signal_type={r.signal_type}"
        if r.error:
            line += f"  error={r.error}"
        print(line)
    signals = [r for r in results if r.status in ("signal", "signal_partial")]
    print(f"\n落库信号 {len(signals)} 条（swing_signal，status=pending，待人工确认）")
    print(f"盒带: {provider.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
