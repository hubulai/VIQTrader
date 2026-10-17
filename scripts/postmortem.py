"""案例尸检 CLI（P5.3）：LLM 草拟 → 人工确认 → 入 case_library。

v1 无任何自动入库路径：--draft 只产草稿文件（含 LLM 输出与事实字段），
人工审阅后用 --confirm 才把草稿写入 case_library（P4.7 学习注入侧消费）。

用法（仓库根目录）：
    .venv\\Scripts\\python.exe scripts\\postmortem.py --scan
    .venv\\Scripts\\python.exe scripts\\postmortem.py --draft swing_pick <signal_id>
    .venv\\Scripts\\python.exe scripts\\postmortem.py --confirm <草稿文件路径>

--draft 真实 LLM 响应自动以盒带归档 tests/fixtures/llm_cassettes/postmortem/，
重放命中指纹即离线复现。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from viqtrader.agents.llm.cassette import CassetteProvider  # noqa: E402
from viqtrader.agents.llm.openai_compat import OpenAICompatProvider  # noqa: E402
from viqtrader.agents.shared.postmortem_agent import (  # noqa: E402
    assemble_case_input,
    case_id_for,
    run_postmortem_agent,
    scan_postmortem_candidates,
)
from viqtrader.config.loader import Config  # noqa: E402
from viqtrader.store.db import connect  # noqa: E402
from viqtrader.store.migrate import migrate  # noqa: E402


def _drafts_dir(config) -> Path:  # type: ignore[no-untyped-def]
    data_dir = Path(str(config.get("platform.data_dir", REPO_ROOT / "data")))
    drafts = data_dir / "postmortem_drafts"
    drafts.mkdir(parents=True, exist_ok=True)
    return drafts


def _cmd_scan(conn, drafts_dir: Path) -> int:  # type: ignore[no-untyped-def]
    candidates = scan_postmortem_candidates(conn, drafts_dir=drafts_dir)
    if not candidates:
        print("无可尸检决策（需先有已判定标签，且未入库、无草稿）。")
        return 0
    print(f"可尸检决策 {len(candidates)} 个：")
    for c in candidates:
        print(f"  {c.decision_type} {c.decision_ref} {c.code}（已判定标签 {c.label_count} 枚）")
    print("\n下一步：--draft <decision_type> <decision_ref> 生成草稿")
    return 0


def _cmd_draft(conn, config, drafts_dir: Path, decision_type: str, decision_ref: str) -> int:  # type: ignore[no-untyped-def]
    case_input = assemble_case_input(conn, decision_type, decision_ref)
    if case_input is None:
        print(f"决策不存在: {decision_type} {decision_ref}", file=sys.stderr)
        return 2
    if not case_input.outcome_labels:
        print(f"{decision_ref} 尚无已判定标签，先跑 scripts\\label_outcomes.py", file=sys.stderr)
        return 2

    inner = OpenAICompatProvider.from_config(config)
    provider = CassetteProvider(
        inner,
        cassette_dir=REPO_ROOT / "tests" / "fixtures" / "llm_cassettes",
        agent="postmortem",
        case=f"postmortem_{date.today().isoformat()}",
        mode="record",
    )
    run_id, run = run_postmortem_agent(conn, config, provider, case_input=case_input)
    if run.status != "SUCCESS" or run.validation.parsed is None:
        errors = "; ".join(run.validation.errors)
        print(f"[{run.status}] 尸检草稿生成失败: {errors}", file=sys.stderr)
        return 1

    output = run.validation.parsed
    draft = {
        "decision_type": case_input.decision_type,
        "decision_ref": case_input.decision_ref,
        "code": case_input.code,
        "industry": case_input.industry,
        "thesis_type": case_input.thesis_type,
        "run_id": run_id,
        "title": output.title,
        "failure_mode": output.failure_mode,
        "postmortem": output.postmortem,
        "tags": list(output.tags),
        "data_refs": list(output.data_refs),
    }
    draft_file = drafts_dir / f"{decision_type}-{decision_ref}.json"
    draft_file.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"草稿已生成（未入库）: {draft_file}")
    print(f"  标题: {output.title}")
    print(f"  失败模式: {output.failure_mode or '—（成功案例）'}")
    print(f"  尸检结论: {output.postmortem}")
    print(f"盒带: {provider.path}")
    print("\n审阅无误后执行：--confirm 该文件路径 入库")
    return 0


def _cmd_confirm(conn, draft_path: Path) -> int:  # type: ignore[no-untyped-def]
    if not draft_path.exists():
        print(f"草稿文件不存在: {draft_path}", file=sys.stderr)
        return 2
    try:
        draft = json.loads(draft_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"草稿 JSON 解析失败: {exc}", file=sys.stderr)
        return 2

    case_id = case_id_for(str(draft["decision_type"]), str(draft["decision_ref"]))
    exists = conn.execute("SELECT 1 FROM case_library WHERE case_id = ?", [case_id]).fetchone()
    if exists:
        print(f"案例已入库，幂等跳过: {case_id}")
        return 0
    conn.execute(
        """
        INSERT INTO case_library (
            case_id, code, industry, thesis_type, failure_mode, title, postmortem, tags
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            case_id,
            draft["code"],
            draft.get("industry"),
            draft.get("thesis_type"),
            draft.get("failure_mode"),
            draft["title"],
            draft["postmortem"],
            json.dumps(draft.get("tags", []), ensure_ascii=False),
        ],
    )
    confirmed = draft_path.with_suffix(".confirmed.json")
    draft_path.rename(confirmed)
    print(f"案例已入库: {case_id}（草稿归档为 {confirmed.name}）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="案例尸检：扫描 / 草拟 / 确认入库（P5.3）")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--scan", action="store_true", help="列出可尸检决策")
    group.add_argument(
        "--draft",
        nargs=2,
        metavar=("DECISION_TYPE", "DECISION_REF"),
        help="生成尸检草稿（如 swing_pick <signal_id>）",
    )
    group.add_argument("--confirm", metavar="DRAFT_FILE", help="确认草稿并入 case_library")
    args = parser.parse_args()

    config = Config.load(REPO_ROOT / "config")
    conn = connect(config)
    migrate(conn)
    drafts_dir = _drafts_dir(config)

    if args.scan:
        return _cmd_scan(conn, drafts_dir)
    if args.draft:
        return _cmd_draft(conn, config, drafts_dir, args.draft[0], args.draft[1])
    return _cmd_confirm(conn, Path(args.confirm))


if __name__ == "__main__":
    raise SystemExit(main())
