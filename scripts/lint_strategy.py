"""P3.7 前视偏差 linter 命令行：对策略源码文件做静态检查（入口强制门禁）。

手写策略入回测前、P4 LLM 生成策略后，强制过此门禁（P4 用 --strict）。
退出码：0 = 全部通过；1 = 至少一个文件有阻断发现或无法解析。

Usage:
    .venv\\Scripts\\python.exe scripts\\lint_strategy.py [--strict] <策略文件1.py> [...]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from viqtrader.engine.backtest.lookahead import format_report, lint_file


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="策略前视偏差静态门禁")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="WARN 也阻断（P4 LLM 生成策略入口必须开启）",
    )
    parser.add_argument("paths", nargs="+", help="策略源码文件（一个或多个）")
    args = parser.parse_args(argv)

    failed = 0
    for raw_path in args.paths:
        path = Path(raw_path)
        print(f"== {path} ==")
        try:
            report = lint_file(path, strict=args.strict)
        except FileNotFoundError:
            print(f"  [错误] 文件不存在: {path}", file=sys.stderr)
            failed += 1
            continue
        except ValueError as exc:  # 语法错误无法解析
            print(f"  [错误] {exc}", file=sys.stderr)
            failed += 1
            continue
        print(format_report(report))
        if not report.passed:
            failed += 1
        print()

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
