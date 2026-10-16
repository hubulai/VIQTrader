"""P3.7 前视偏差 AST linter 单测：LA001-LA006 正反例 + 豁免 + strict + 序列化。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from viqtrader.engine.backtest.lookahead import (
    ERROR,
    WARN,
    lint_file,
    lint_source,
)


def _ids(report: object) -> list[str]:
    return [f.rule_id for f in report.findings]  # type: ignore[attr-defined]


def _finding(report: object, rule_id: str):
    for f in report.findings:  # type: ignore[attr-defined]
        if f.rule_id == rule_id:
            return f
    raise AssertionError(f"未找到 {rule_id}，实际：{_ids(report)}")


# ---------------------------------------------------------------------------
# LA001 / LA002 shift
# ---------------------------------------------------------------------------


def test_shift_negative_positional_and_keyword() -> None:
    code = "x = close.shift(-1)\ny = close.shift(periods=-3)\n"
    r = lint_source(code)
    assert [f.rule_id for f in r.findings] == ["LA001", "LA001"]
    assert all(f.severity == ERROR for f in r.findings)
    assert not r.passed
    assert r.blocking_findings and len(r.blocking_findings) == 2


def test_shift_safe_forms_pass() -> None:
    """正位移 / 无参 shift() / shift(0) 都是安全历史回看。"""
    code = "a = s.shift(1)\nb = s.shift()\nc = s.shift(periods=0)\nd = s.shift(5)\n"
    r = lint_source(code)
    assert r.findings == []
    assert r.passed


def test_shift_dynamic_warns_but_passes_by_default() -> None:
    r = lint_source("y = s.shift(n)\n")
    assert _finding(r, "LA002").severity == WARN
    assert r.passed  # 普通模式 WARN 不阻断
    assert not lint_source("y = s.shift(n)\n", strict=True).passed  # strict 阻断


def test_bare_function_shift_not_flagged() -> None:
    """规则只管 .shift 方法调用；自定义同名函数不误伤。"""
    assert lint_source("y = shift(-1)\n").findings == []


# ---------------------------------------------------------------------------
# LA003 未来回填
# ---------------------------------------------------------------------------


def test_bfill_family_errors() -> None:
    code = (
        "a = s.bfill()\n"
        "b = s.backfill()\n"
        "c = df.fillna(method='bfill')\n"
        "d = df.fillna('backfill')\n"
        "e = s.interpolate(limit_direction='both')\n"
        "f = s.interpolate(limit_direction='backward')\n"
    )
    r = lint_source(code)
    assert _ids(r).count("LA003") == 6
    assert not r.passed


def test_forward_fill_and_interpolate_safe() -> None:
    code = (
        "a = s.ffill()\n"
        "b = df.fillna(method='pad')\n"
        "c = s.interpolate()\n"
        "d = s.interpolate(limit_direction='forward')\n"
        "e = df.fillna(0)\n"
    )
    assert lint_source(code).findings == []


# ---------------------------------------------------------------------------
# LA004 rolling 居中
# ---------------------------------------------------------------------------


def test_rolling_center_errors() -> None:
    code = "a = s.rolling(20, center=True).mean()\nb = s.rolling(20, 5, True).std()\n"
    r = lint_source(code)
    assert _ids(r) == ["LA004", "LA004"]


def test_rolling_right_aligned_safe() -> None:
    code = (
        "a = s.rolling(20).mean()\n"
        "b = s.rolling(20, min_periods=5).std()\n"
        "c = s.rolling(20, center=False).max()\n"
    )
    assert lint_source(code).findings == []


# ---------------------------------------------------------------------------
# LA005 前向索引
# ---------------------------------------------------------------------------


def test_future_index_warns() -> None:
    code = "future1 = bars[i + 1]\nfuture2 = df.iloc[i + 2, :]\nfuture3 = seq[i:i+3]\n"
    r = lint_source(code)
    assert _ids(r) == ["LA005", "LA005", "LA005"]
    assert all(f.severity == WARN for f in r.findings)
    assert r.passed
    assert not lint_source(code, strict=True).passed


def test_backward_index_safe() -> None:
    """负下标=最近历史、i-N=回看，绝对常量/字符串键与时点无关，均安全。"""
    code = "a = bars[-1]\nb = bars[i - 1]\nc = df.iloc[5]\nd = seq[3:10]\ne = df['close']\n"
    assert lint_source(code).findings == []


def test_future_index_operand_order_and_nested_chain() -> None:
    """N+i 与 i+N 两种顺序都命中；链式调用 shift + iloc 双命中。"""
    r = lint_source("x = bars[i + 1]\ny = df.iloc[2 + i]\n")
    assert _ids(r) == ["LA005", "LA005"]
    r2 = lint_source("y = df['close'].shift(-1).iloc[i + 2]\n")
    # 同行内外层走访顺序不做承诺，按规则 id 集合断言
    assert sorted(_ids(r2)) == ["LA001", "LA005"]


# ---------------------------------------------------------------------------
# LA006 数据绕过
# ---------------------------------------------------------------------------


def test_data_bypass_imports_and_open() -> None:
    code = (
        "import duckdb\n"
        "import sqlite3\n"
        "import requests\n"
        "from urllib.request import urlopen\n"
        "from http import client\n"
        "f = open('x.csv')\n"
    )
    r = lint_source(code)
    assert _ids(r).count("LA006") == 6
    assert not r.passed


def test_normal_strategy_imports_safe() -> None:
    code = (
        "from datetime import date\n"
        "from viqtrader.engine.types import OrderIntent, OrderSide, OrderType\n"
        "from decimal import Decimal\n"
    )
    assert lint_source(code).findings == []


# ---------------------------------------------------------------------------
# 豁免
# ---------------------------------------------------------------------------


def test_waiver_valid_marks_waived_and_passes() -> None:
    code = "y = s.shift(-1)  # viq-lookahead: ignore LA001 该序列已反向，负位移实为回看\n"
    r = lint_source(code)
    f = _finding(r, "LA001")
    assert f.waived
    assert f.waiver_reason == "该序列已反向，负位移实为回看"
    assert r.passed
    assert r.waivers and r.waivers[0].rule_id == "LA001"
    # strict 下豁免的 ERROR 同样放行
    assert lint_source(code, strict=True).passed


def test_waiver_without_reason_errors() -> None:
    code = "y = s.shift(-1)  # viq-lookahead: ignore LA001\n"
    r = lint_source(code)
    assert _finding(r, "EXEMPT-NO-REASON").severity == ERROR
    # LA001 未被豁免（理由无效），仍然阻断
    assert not r.passed


def test_waiver_unknown_rule_errors() -> None:
    code = "y = s.shift(-1)  # viq-lookahead: ignore LA999 写错规则号\n"
    r = lint_source(code)
    assert _finding(r, "EXEMPT-UNKNOWN-RULE").severity == ERROR
    assert not r.passed


def test_waiver_unused_is_warn_strict_blocks() -> None:
    code = "x = 1  # viq-lookahead: ignore LA001 该行没有任何违规\n"
    r = lint_source(code)
    f = _finding(r, "EXEMPT-UNUSED")
    assert f.severity == WARN
    assert r.passed  # 普通模式死豁免仅告警
    assert not lint_source(code, strict=True).passed  # strict 下阻断


def test_waiver_wrong_line_does_not_waive() -> None:
    """豁免只对同一行生效；挂在上一行不能豁免下一行的违规。"""
    code = "# viq-lookahead: ignore LA001 挂错行的豁免\ny = s.shift(-1)\n"
    r = lint_source(code)
    assert not _finding(r, "LA001").waived
    assert not r.passed
    assert _finding(r, "EXEMPT-UNUSED")  # 挂空的豁免同时被审计出来


# ---------------------------------------------------------------------------
# 综合 / 边界
# ---------------------------------------------------------------------------


def test_clean_strategy_passes() -> None:
    code = (
        "class MaStrategy:\n"
        "    def on_bar(self, ctx):\n"
        "        bars = ctx.bars('600519', 20)\n"
        "        if len(bars) < 20:\n"
        "            return []\n"
        "        last = bars[-1]\n"
        "        if last.close is None:\n"
        "            return []\n"
        "        return [ctx.make_intent('acc_swing', 'swing', '600519', 'SH', 'BUY', 100)]\n"
    )
    r = lint_source(code, strict=True)
    assert r.passed
    assert r.findings == []


def test_syntax_error_raises_value_error() -> None:
    with pytest.raises(ValueError, match="语法错误"):
        lint_source("def f(:\n")


def test_to_dict_json_serializable() -> None:
    code = "y = s.shift(-1)  # viq-lookahead: ignore LA001 理由文本\n"
    r = lint_source(code, strict=True, filename="demo.py")
    payload = json.dumps(r.to_dict(), ensure_ascii=False)
    data = json.loads(payload)
    assert data["filename"] == "demo.py"
    assert data["strict"] is True
    assert data["passed"] is True
    assert data["findings"][0]["waived"] is True
    assert data["waivers"][0]["reason"].startswith("理由文本")


def test_lint_file_tolerates_utf8_bom(tmp_path: Path) -> None:
    """Windows 编辑器的 UTF-8 BOM 文件 Python 可运行，linter 同样应接受。"""
    p = tmp_path / "s.py"
    p.write_bytes("y = s.shift(-1)\n".encode("utf-8-sig"))
    r = lint_file(p)
    assert _ids(r) == ["LA001"]


def test_findings_sorted_by_line() -> None:
    code = "z = s.shift(-1)\na = s.bfill()\n"
    r = lint_source(code)
    linenos = [f.lineno for f in r.findings]
    assert linenos == sorted(linenos)
