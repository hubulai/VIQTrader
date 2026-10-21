"""元复盘 CLI（P7）：学习提案的产出 → 反方审查 → 人工拍板闸。

v1 纯手动、不挂 DAG：窗口参数化（--days，默认 90），无硬性季度闸。
生效路径为轻量人工闸——批准只标状态并输出 YAML 片段建议，人工手动合并
user.yaml 后 --applied 收口；平台无自动改写配置路径。

用法（仓库根目录）：
    .venv\\Scripts\\python.exe scripts\\meta_review.py --run [--days 90]
    .venv\\Scripts\\python.exe scripts\\meta_review.py --dissent [--days 90]
    .venv\\Scripts\\python.exe scripts\\meta_review.py --list [--days 90] [--json]
    .venv\\Scripts\\python.exe scripts\\meta_review.py --approve <proposal_id>
    .venv\\Scripts\\python.exe scripts\\meta_review.py --reject <proposal_id>
    .venv\\Scripts\\python.exe scripts\\meta_review.py --applied <proposal_id>

退出码：0 正常；1 业务失败（LLM 失败/降级、非法状态迁移）；2 参数错误。
--run/--dissent 真实 LLM 响应自动盒带归档 tests/fixtures/llm_cassettes/，离线可重放。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from viqtrader.agents.llm.cassette import CassetteProvider  # noqa: E402
from viqtrader.agents.llm.openai_compat import OpenAICompatProvider  # noqa: E402
from viqtrader.agents.shared.meta_review_agent import (  # noqa: E402
    apply_dissent_result,
    assemble_dissent_input,
    assemble_meta_review_input,
    list_proposals,
    persist_proposals,
    render_yaml_snippet,
    run_meta_review_agent,
    run_meta_review_dissent,
    transition_proposal,
)
from viqtrader.config.loader import Config  # noqa: E402
from viqtrader.store.db import connect  # noqa: E402
from viqtrader.store.migrate import migrate  # noqa: E402

DEFAULT_DAYS = 90  # 拍板口径：默认复盘窗口 90 天（v1 手动触发，无硬性季度闸）


def _window(days: int) -> tuple[date, date]:
    window_end = date.today()
    return window_end - timedelta(days=days), window_end


def _provider(config: Config, agent: str, case_prefix: str) -> CassetteProvider:
    inner = OpenAICompatProvider.from_config(config)
    return CassetteProvider(
        inner,
        cassette_dir=REPO_ROOT / "tests" / "fixtures" / "llm_cassettes",
        agent=agent,
        case=f"{case_prefix}_{date.today().isoformat()}",
        mode="record",
    )


def _cmd_run(conn, config, days: int) -> int:  # type: ignore[no-untyped-def]
    window_start, window_end = _window(days)
    review_input = assemble_meta_review_input(
        conn, config, window_start=window_start, window_end=window_end
    )
    provider = _provider(config, "meta_review", "meta_review")
    _run_id, run = run_meta_review_agent(conn, config, provider, review_input=review_input)
    if run.status != "SUCCESS" or run.validation.parsed is None:
        errors = "; ".join(run.validation.errors)
        print(f"[{run.status}] 元复盘失败: {errors}", file=sys.stderr)
        return 1

    inserted, skipped = persist_proposals(
        conn, window_start=window_start, window_end=window_end, output=run.validation.parsed
    )
    output = run.validation.parsed
    print(f"窗口总结：{output.window_summary}")
    if output.sample_caveat:
        print(f"样本量声明：{output.sample_caveat}")
    print(f"\n提案落库 {len(inserted)} 条（draft），幂等跳过 {skipped} 条：")
    for pid in inserted:
        print(f"  {pid}")
    print(f"盒带: {provider.path}")
    if inserted:
        print("\n下一步：--dissent 反方审查 → --list 查看 → --approve/--reject 拍板")
    return 0


def _cmd_dissent(conn, config, days: int) -> int:  # type: ignore[no-untyped-def]
    window_start, window_end = _window(days)
    drafts = [
        p["proposal_id"]
        for p in list_proposals(conn, window_start=window_start, window_end=window_end)
        if p["status"] == "draft"
    ]
    if not drafts:
        print("窗口内无 draft 状态提案，无需反方审查。")
        return 0
    dissent_input = assemble_dissent_input(conn, config, proposal_ids=drafts)
    if dissent_input is None:
        print("提案装配失败（记录缺失）。", file=sys.stderr)
        return 1

    provider = _provider(config, "meta_review_dissent", "meta_review_dissent")
    run_id, run = run_meta_review_dissent(conn, config, provider, dissent_input=dissent_input)
    if run.status != "SUCCESS" or run.validation.parsed is None:
        errors = "; ".join(run.validation.errors)
        print(f"[{run.status}] 反方审查失败: {errors}", file=sys.stderr)
        return 1

    output_id = apply_dissent_result(conn, run_id, dissent_input.proposal_ids)
    result = run.validation.parsed
    print(f"总体意见：{result.overall}")
    print("\n逐提案判定：")
    for verdict in result.verdicts:
        mark = "放行" if verdict.approved else "打回"
        print(f"  [{verdict.proposal_index}] {mark}：{verdict.verdict}")
        for concern in verdict.concerns:
            print(f"      顾虑: {concern}")
        for question in verdict.questions:
            print(f"      待答: {question}")
    print(f"\n提案已更新为 dissent_reviewed（dissent_output_id={output_id}）")
    print(f"盒带: {provider.path}")
    print("\n下一步：--list 查看 → --approve/--reject 拍板")
    return 0


def _cmd_list(conn, days: int, as_json: bool) -> int:  # type: ignore[no-untyped-def]
    window_start, window_end = _window(days)
    proposals = list_proposals(conn, window_start=window_start, window_end=window_end)
    if as_json:
        print(json.dumps(proposals, ensure_ascii=False, indent=2))
        return 0
    if not proposals:
        print("窗口内无提案。")
        return 0
    for p in proposals:
        print(f"{p['proposal_id']}  [{p['status']}]  {p['kind']}（{p['confidence']}）")
        print(f"  《{p['title']}》 {p['rationale']}")
        for ref in p["evidence_refs"]:
            print(f"    证据: {ref}")
    return 0


def _cmd_transition(conn, config, proposal_id: str, action: str) -> int:  # type: ignore[no-untyped-def]
    try:
        if action == "applied":
            # 人工已合并 user.yaml：记录当前生效配置快照哈希收口
            old, new = transition_proposal(
                conn, proposal_id, action, applied_config_hash=str(config.content_hash())
            )
        else:
            old, new = transition_proposal(conn, proposal_id, action)
    except LookupError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if old == new:
        print(f"提案已是 {new} 状态，幂等跳过: {proposal_id}")
        return 0
    print(f"{proposal_id}: {old} → {new}")

    if action == "approve":
        row = conn.execute(
            "SELECT kind, payload_json FROM learning_proposal WHERE proposal_id = ?",
            [proposal_id],
        ).fetchone()
        snippet = render_yaml_snippet(str(row[0]), json.loads(row[1]))
        if snippet is not None:
            print("\nYAML 片段建议（人工合并到 config\\user.yaml）：")
            print(snippet)
        print("\n合并配置后执行：--applied 该 proposal_id 收口（记录生效配置快照哈希）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="元复盘：学习提案产出/审查/人工拍板（P7）")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--run", action="store_true", help="跑元复盘，提案落库为 draft")
    group.add_argument("--dissent", action="store_true", help="对窗口内 draft 提案做反方审查")
    group.add_argument("--list", action="store_true", help="列出窗口内提案")
    group.add_argument("--approve", metavar="PROPOSAL_ID", help="批准提案（输出 YAML 建议）")
    group.add_argument("--reject", metavar="PROPOSAL_ID", help="拒绝提案")
    group.add_argument("--applied", metavar="PROPOSAL_ID", help="确认已合并配置，收口提案")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help="复盘窗口天数（默认 90）")
    parser.add_argument("--json", action="store_true", help="--list 以 JSON 输出")
    args = parser.parse_args()

    if args.days <= 0:
        print("--days 必须为正整数", file=sys.stderr)
        return 2

    config = Config.load(REPO_ROOT / "config")
    conn = connect(config)
    migrate(conn)

    if args.run:
        return _cmd_run(conn, config, args.days)
    if args.dissent:
        return _cmd_dissent(conn, config, args.days)
    if args.list:
        return _cmd_list(conn, args.days, args.json)
    if args.approve:
        return _cmd_transition(conn, config, args.approve, "approve")
    if args.reject:
        return _cmd_transition(conn, config, args.reject, "reject")
    return _cmd_transition(conn, config, args.applied, "applied")


if __name__ == "__main__":
    raise SystemExit(main())
