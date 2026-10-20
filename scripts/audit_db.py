"""P7 库况体检 CLI（audit_db）。

全库截面体检（只读离线，不触网不写库）：financial / valuation / 勾稽三块 +
随机抽样对账。三态判定 OK / WARN / FAIL，阈值外置 config `audit:` 节。

用法（仓库根目录）：
    .venv\\Scripts\\python.exe scripts\\audit_db.py
    .venv\\Scripts\\python.exe scripts\\audit_db.py --strict         # FAIL 时退出码 1
    .venv\\Scripts\\python.exe scripts\\audit_db.py --json           # 机器可读 JSON
    .venv\\Scripts\\python.exe scripts\\audit_db.py --samples 20 --seed 7

退出码：体检完成 0 / --strict 且有 FAIL（或数据库不可打开）1 / 参数错误 2。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from viqtrader.config.loader import Config  # noqa: E402
from viqtrader.data.audit import AuditReport, audit_database  # noqa: E402
from viqtrader.store.db import connect, db_path_from_config  # noqa: E402


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.4g}"
    return str(value)


def _print_human(report: AuditReport) -> None:
    state_mark = {"OK": "[ OK ]", "WARN": "[WARN]", "FAIL": "[FAIL]"}
    for section, findings in report.by_section().items():
        print(f"\n== {section} ==")
        for f in findings:
            print(f"{state_mark[f.state]} {f.check}: {f.message}")

    if report.samples:
        print("\n== 随机抽样对账 ==")
        print(
            f"{'代码':<8} {'报告期':<12} {'公告日':<12} {'营收':>12} {'净利':>12} "
            f"{'ROE':>7} {'FCF':>12} {'BPS':>7}"
        )
        for s in report.samples:
            fin = s.financial
            print(
                f"{s.code:<8} {_fmt(fin.get('report_period')):<12} "
                f"{_fmt(fin.get('ann_date')):<12} "
                f"{_fmt(fin.get('revenue')):>12} {_fmt(fin.get('net_profit')):>12} "
                f"{_fmt(fin.get('roe')):>7} {_fmt(fin.get('fcf')):>12} "
                f"{_fmt(fin.get('bps')):>7}"
            )
        print(
            f"{'代码':<8} {'交易日':<12} {'PE_TTM':>8} {'PB':>8} {'PS_TTM':>8} "
            f"{'股息率':>8} {'市值':>14} {'PE分位':>8}"
        )
        for s in report.samples:
            val = s.valuation
            print(
                f"{s.code:<8} {_fmt(val.get('trade_date')):<12} "
                f"{_fmt(val.get('pe_ttm')):>8} {_fmt(val.get('pb')):>8} "
                f"{_fmt(val.get('ps_ttm')):>8} {_fmt(val.get('dv_ratio')):>8} "
                f"{_fmt(val.get('market_cap')):>14} {_fmt(val.get('pe_percentile')):>8}"
            )

    print(
        f"\n汇总：OK {report.ok_count} / WARN {report.warn_count} / FAIL {report.fail_count}"
        f"（最差状态 {report.worst_state}）"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="库况体检 audit_db（只读离线）")
    parser.add_argument(
        "--strict", action="store_true", help="存在 FAIL 时以退出码 1 结束（默认仅告警）"
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出报告（机器可读）")
    parser.add_argument("--samples", type=int, default=None, help="随机抽样对账条数（缺省取配置）")
    parser.add_argument("--seed", type=int, default=None, help="抽样随机种子（缺省取配置）")
    args = parser.parse_args()

    if args.samples is not None and args.samples < 0:
        print("--samples 不能为负", file=sys.stderr)
        return 2

    config = Config.load(REPO_ROOT / "config")
    db_path = db_path_from_config(config)
    if not db_path.exists():
        print(f"数据库不存在：{db_path}（请先执行 scripts\\sync_market.py）", file=sys.stderr)
        return 1
    try:
        conn = connect(config, read_only=True)
    except Exception as exc:
        print(f"数据库不可打开：{exc}", file=sys.stderr)
        return 1

    try:
        report = audit_database(conn, config, sample_size=args.samples, seed=args.seed)
    finally:
        conn.close()

    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        _print_human(report)

    if args.strict and report.fail_count > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
