"""批量 / 单份解析真实研报（P4.8）。

用法（仓库根目录）：
    .venv\\Scripts\\python.exe scripts\\parse_docs.py --all --limit 5
    .venv\\Scripts\\python.exe scripts\\parse_docs.py --doc-id <uuid>

前置条件：研报 PDF 已放入 data/docs/inbox 并经 P1.8 管线入库（parse_status='pending'）。
真实 LLM 响应自动以盒带归档到 tests/fixtures/llm_cassettes/doc_parse/，可离线复现。
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
from viqtrader.agents.shared.doc_parser import parse_document, parse_pending  # noqa: E402
from viqtrader.config.loader import Config  # noqa: E402
from viqtrader.store.db import connect  # noqa: E402
from viqtrader.store.migrate import migrate  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="解析研报 PDF → 结构化 JSON（document_analysis）")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--doc-id", help="解析单份（按 document.doc_id）")
    group.add_argument("--all", action="store_true", help="批量扫描 parse_status='pending'")
    parser.add_argument("--limit", type=int, default=None, help="--all 时的份数上限")
    args = parser.parse_args()

    config = Config.load(REPO_ROOT / "config")
    conn = connect(config)
    migrate(conn)

    inner = OpenAICompatProvider.from_config(config)
    provider = CassetteProvider(
        inner,
        cassette_dir=REPO_ROOT / "tests" / "fixtures" / "llm_cassettes",
        agent="doc_parse",
        case=date.today().isoformat(),
        mode="record",
    )

    if args.doc_id:
        results = [parse_document(conn, config, provider, doc_id=args.doc_id)]
    else:
        results = parse_pending(conn, config, provider, limit=args.limit)

    n_parsed = sum(1 for r in results if r.status == "parsed")
    n_skipped = sum(1 for r in results if r.status == "skipped")
    n_failed = sum(1 for r in results if r.status == "failed")
    for r in results:
        tail = f"  error: {r.error}" if r.error else ""
        print(f"[{r.status}] {r.doc_id}{tail}")
    print(f"合计 {len(results)} 份：parsed {n_parsed} / skipped {n_skipped} / failed {n_failed}")
    print(f"盒带: {provider.path}")
    return 0 if n_failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
