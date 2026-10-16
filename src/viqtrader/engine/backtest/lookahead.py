"""P3.7 前视偏差 AST linter（代码级第二道防线，纯 ast 零执行 / 零 DB）。

定位边界（2026-10-16 裁决）：
- linter 只负责"代码文本"静态检查，不执行策略、不做类型推断；
- 数据时点的机制防线仍是 P3.2 StrategyContext 的 SQL `trade_date < T`，
  linter 与机制防线互补，不互相替代；
- 独立 API + 入口强制：BacktestEngine 不自动 lint（测试中策略多为函数内局部类，
  按模块文件检查会误伤），由回测入口（CLI scripts/lint_strategy.py / notebook /
  P4 LLM 编排）对策略源码显式调用 lint_source/lint_file；P4 生成必须 strict=True。

规则集（只收录静态可可靠判定的模式，不做无类型信息的取值方向语义猜测）：
- LA001 shift-negative  ERROR  .shift(-N) / shift(periods=-N) 负常量周期
- LA002 shift-dynamic   WARN   shift 的周期数非常量，无法静态证明方向
- LA003 bfill           ERROR  bfill/backfill/fillna(method=bfill|backfill)/
                                interpolate(limit_direction=backward|both)
- LA004 rolling-center  ERROR  rolling(..., center=True) 居中窗口含未来 bar
- LA005 future-index    WARN   下标为"变量 + 正偏移"：x[i+N] / .iloc[i+N, :]
                                / x[i:i+N] 切片边界
- LA006 data-bypass     ERROR  import duckdb/sqlite3/requests/urllib/http/socket/
                                aiohttp/httpx 或直接 open()，绕过 ctx 自取数据

豁免（误报人工放行 + 留痕）：行尾注释
    # viq-lookahead: ignore LA001 理由必须非空
- 仅对同一行的同一规则生效；豁免未知规则 id 报 EXEMPT-UNKNOWN-RULE（ERROR）；
- 理由为空报 EXEMPT-NO-REASON（ERROR）；
- 豁免注释未命中任何发现报 EXEMPT-UNUSED（WARN，重构后残留的死豁免）；
- 豁免管理类发现自身不可被豁免。

通过判定：无未豁免 ERROR 即 passed；strict=True 时未豁免 WARN 也阻断
（P4 LLM 入口强制 strict）。
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

ERROR = "ERROR"
WARN = "WARN"

# ---------------------------------------------------------------------------
# 规则注册表
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Rule:
    rule_id: str
    severity: str
    title: str


RULES: dict[str, Rule] = {
    "LA001": Rule("LA001", ERROR, "shift 使用负周期（向后看未来数据）"),
    "LA002": Rule("LA002", WARN, "shift 周期为非常量，无法证明不看未来"),
    "LA003": Rule("LA003", ERROR, "bfill/backfill 类用未来值回填"),
    "LA004": Rule("LA004", ERROR, "rolling(center=True) 居中窗口含未来 bar"),
    "LA005": Rule("LA005", WARN, "下标为变量+正偏移，可能是前向索引"),
    "LA006": Rule("LA006", ERROR, "直接 IO/连库绕过 ctx 数据时点防线"),
}

# 豁免管理类发现（不属于数据规则，不可被豁免）
EXEMPT_NO_REASON = "EXEMPT-NO-REASON"
EXEMPT_UNKNOWN_RULE = "EXEMPT-UNKNOWN-RULE"
EXEMPT_UNUSED = "EXEMPT-UNUSED"

_FORBIDDEN_MODULES = frozenset(
    {
        "duckdb",
        "sqlite3",
        "requests",
        "urllib",
        "http",
        "socket",
        "aiohttp",
        "httpx",
    }
)

_WAIVER_RE = re.compile(r"#\s*viq-lookahead\s*:\s*ignore\s+(LA\d{3})(?:\s+(.*\S))?\s*$")


# ---------------------------------------------------------------------------
# 报告结构（全部可 JSON 序列化）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Finding:
    rule_id: str
    severity: str
    message: str
    lineno: int
    col: int
    line_text: str
    waived: bool = False
    waiver_reason: str | None = None


@dataclass(frozen=True, slots=True)
class Waiver:
    """源码中解析出的豁免注释（留痕载体，含未匹配的死豁免）。"""

    lineno: int
    rule_id: str
    reason: str | None  # None = 注释未给理由（同时产 EXEMPT-NO-REASON）


@dataclass(slots=True)
class LintReport:
    filename: str
    strict: bool
    findings: list[Finding] = field(default_factory=list)
    waivers: list[Waiver] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """无未豁免 ERROR；strict 时未豁免 WARN 也阻断。"""
        return not self.blocking_findings

    @property
    def blocking_findings(self) -> list[Finding]:
        """导致不通过的未豁免发现。"""
        out: list[Finding] = []
        for f in self.findings:
            if f.waived:
                continue
            if f.severity == ERROR or (self.strict and f.severity == WARN):
                out.append(f)
        return out

    def to_dict(self) -> dict[str, object]:
        return {
            "filename": self.filename,
            "strict": self.strict,
            "passed": self.passed,
            "findings": [
                {
                    "rule_id": f.rule_id,
                    "severity": f.severity,
                    "message": f.message,
                    "lineno": f.lineno,
                    "col": f.col,
                    "line_text": f.line_text,
                    "waived": f.waived,
                    "waiver_reason": f.waiver_reason,
                }
                for f in self.findings
            ],
            "waivers": [
                {"lineno": w.lineno, "rule_id": w.rule_id, "reason": w.reason} for w in self.waivers
            ],
        }


# ---------------------------------------------------------------------------
# AST 访问器
# ---------------------------------------------------------------------------


class _LookaheadVisitor(ast.NodeVisitor):
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines
        self.raw: list[Finding] = []

    def _line(self, lineno: int) -> str:
        idx = lineno - 1
        return self._lines[idx].strip() if 0 <= idx < len(self._lines) else ""

    def _emit(self, rule_id: str, node: ast.AST, message: str) -> None:
        rule = RULES[rule_id]
        lineno = getattr(node, "lineno", 0)
        self.raw.append(
            Finding(
                rule_id=rule_id,
                severity=rule.severity,
                message=message,
                lineno=lineno,
                col=getattr(node, "col_offset", 0),
                line_text=self._line(lineno),
            )
        )

    # -- import ------------------------------------------------------------

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            top = alias.name.split(".")[0]
            if top in _FORBIDDEN_MODULES:
                self._emit(
                    "LA006",
                    node,
                    f"策略内禁止 import {alias.name}：数据只能经 StrategyContext 获取，"
                    "直接连库/网络会绕过 trade_date < T 时点防线",
                )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module:
            top = node.module.split(".")[0]
            if top in _FORBIDDEN_MODULES:
                self._emit(
                    "LA006",
                    node,
                    f"策略内禁止 from {node.module} import ...：数据只能经 "
                    "StrategyContext 获取，直接连库/网络会绕过时点防线",
                )
        self.generic_visit(node)

    # -- 调用 --------------------------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:
        # 内建 open()：直接文件 IO
        if isinstance(node.func, ast.Name) and node.func.id == "open":
            self._emit(
                "LA006",
                node,
                "策略内禁止直接 open()：外部数据须经数据层入 ctx，"
                "读文件无法保证 point-in-time 可见性",
            )
        if isinstance(node.func, ast.Attribute):
            attr = node.func.attr
            if attr == "shift":
                self._check_shift(node)
            elif attr in {"bfill", "backfill"}:
                self._emit(
                    "LA003",
                    node,
                    f".{attr}() 用未来值回填（未来信息泄漏）；前向填充请用 ffill()",
                )
            elif attr == "fillna":
                self._check_fillna(node)
            elif attr == "interpolate":
                self._check_interpolate(node)
            elif attr == "rolling":
                self._check_rolling(node)
        self.generic_visit(node)

    def _check_shift(self, node: ast.Call) -> None:
        periods: ast.AST | None = None
        if node.args:
            periods = node.args[0]
        for kw in node.keywords:
            if kw.arg == "periods":
                periods = kw.value
        if periods is None:
            return  # 无参 shift() 默认 periods=1（向前 lag，安全）
        value = _signed_int_constant(periods)
        if value is not None:
            if value < 0:
                self._emit(
                    "LA001",
                    node,
                    f"shift({value}) 为负周期：引用未来行；回看历史应用正周期",
                )
            return
        self._emit(
            "LA002",
            node,
            "shift 周期为非常量表达式，无法静态证明非负",
        )

    def _check_fillna(self, node: ast.Call) -> None:
        method: ast.AST | None = None
        if node.args:
            method = node.args[0]
        for kw in node.keywords:
            if kw.arg == "method":
                method = kw.value
        if isinstance(method, ast.Constant) and method.value in {"bfill", "backfill"}:
            self._emit(
                "LA003",
                node,
                f"fillna(method={method.value!r}) 用未来值回填",
            )

    def _check_interpolate(self, node: ast.Call) -> None:
        for kw in node.keywords:
            if kw.arg != "limit_direction":
                continue
            if isinstance(kw.value, ast.Constant) and kw.value.value in {
                "backward",
                "both",
            }:
                self._emit(
                    "LA003",
                    node,
                    f"interpolate(limit_direction={kw.value.value!r}) 会用未来值插值",
                )

    def _check_rolling(self, node: ast.Call) -> None:
        center: ast.AST | None = None
        if len(node.args) >= 3:
            center = node.args[2]  # rolling(window, min_periods, center)
        for kw in node.keywords:
            if kw.arg == "center":
                center = kw.value
        if isinstance(center, ast.Constant) and center.value is True:
            self._emit(
                "LA004",
                node,
                "rolling(center=True) 居中窗口包含未来 bar；用默认的右对齐窗口",
            )

    # -- LA005 下标 --------------------------------------------------------

    def visit_Subscript(self, node: ast.Subscript) -> None:
        self._check_slice(node.slice)
        self.generic_visit(node)

    def _check_slice(self, sl: ast.AST) -> None:
        if isinstance(sl, ast.Tuple):
            for elt in sl.elts:
                self._check_index_expr(elt)
        elif isinstance(sl, ast.Slice):
            # x[i:i+N]：上下界含"变量+正偏移"都判前向
            for bound in (sl.lower, sl.upper):
                if bound is not None:
                    self._check_index_expr(bound)
        else:
            self._check_index_expr(sl)

    def _check_index_expr(self, expr: ast.AST) -> None:
        if (
            isinstance(expr, ast.BinOp)
            and isinstance(expr.op, ast.Add)
            and _is_name_plus_positive_int(expr)
        ):
            self._emit(
                "LA005",
                expr,
                "下标为 变量+正偏移：若该变量是时间/行索引则构成前向引用",
            )


def _signed_int_constant(node: ast.AST) -> int | None:
    """提取符号 int 常量；非 int 常量或非常量返回 None。"""
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        if isinstance(node.operand, ast.Constant) and isinstance(node.operand.value, int):
            return -node.operand.value
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value
    return None


def _is_name_plus_positive_int(binop: ast.BinOp) -> bool:
    left, right = binop.left, binop.right
    return (
        isinstance(left, ast.Name)
        and isinstance(right, ast.Constant)
        and isinstance(right.value, int)
        and right.value > 0
    ) or (
        isinstance(right, ast.Name)
        and isinstance(left, ast.Constant)
        and isinstance(left.value, int)
        and left.value > 0
    )


# ---------------------------------------------------------------------------
# 豁免解析与报告组装
# ---------------------------------------------------------------------------


def _parse_waivers(lines: list[str]) -> tuple[list[Waiver], list[Finding]]:
    waivers: list[Waiver] = []
    exempt_findings: list[Finding] = []
    for i, raw_line in enumerate(lines, start=1):
        match = _WAIVER_RE.search(raw_line)
        if not match:
            continue
        rule_id, reason = match.group(1), match.group(2)
        waivers.append(Waiver(lineno=i, rule_id=rule_id, reason=reason))
        if rule_id not in RULES:
            exempt_findings.append(
                Finding(
                    rule_id=EXEMPT_UNKNOWN_RULE,
                    severity=ERROR,
                    message=f"豁免了未知规则 {rule_id}（拼写错误会导致假豁免）",
                    lineno=i,
                    col=0,
                    line_text=raw_line.strip(),
                )
            )
        if reason is None or not reason.strip():
            exempt_findings.append(
                Finding(
                    rule_id=EXEMPT_NO_REASON,
                    severity=ERROR,
                    message="豁免注释必须给出非空理由（viq-lookahead: ignore LAxxx 理由）",
                    lineno=i,
                    col=0,
                    line_text=raw_line.strip(),
                )
            )
    return waivers, exempt_findings


def _apply_waivers(
    findings: list[Finding],
    waivers: list[Waiver],
    lines: list[str],
) -> tuple[list[Finding], list[Finding]]:
    """把豁免标记到同 (lineno, rule_id) 的数据发现上；返回 (标记后发现, 死豁免发现)。"""

    def line_text(lineno: int) -> str:
        idx = lineno - 1
        return lines[idx].strip() if 0 <= idx < len(lines) else ""

    valid = [w for w in waivers if w.rule_id in RULES and w.reason and w.reason.strip()]
    matched: set[int] = set()  # 已被消费的 waiver 索引
    tagged = [
        Finding(
            rule_id=f.rule_id,
            severity=f.severity,
            message=f.message,
            lineno=f.lineno,
            col=f.col,
            line_text=f.line_text,
        )
        for f in findings
    ]
    for idx, f in enumerate(tagged):
        for w_idx, w in enumerate(valid):
            if w_idx in matched:
                continue
            if w.lineno == f.lineno and w.rule_id == f.rule_id:
                tagged[idx] = Finding(
                    rule_id=f.rule_id,
                    severity=f.severity,
                    message=f.message,
                    lineno=f.lineno,
                    col=f.col,
                    line_text=f.line_text,
                    waived=True,
                    waiver_reason=w.reason.strip() if w.reason else None,
                )
                matched.add(w_idx)
                break

    dead: list[Finding] = []
    for w_idx, w in enumerate(valid):
        if w_idx not in matched:
            dead.append(
                Finding(
                    rule_id=EXEMPT_UNUSED,
                    severity=WARN,
                    message=f"行内豁免 {w.rule_id} 未命中任何发现（死豁免，请删除或修正行号）",
                    lineno=w.lineno,
                    col=0,
                    line_text=line_text(w.lineno),
                )
            )
    return tagged, dead


# ---------------------------------------------------------------------------
# 公共 API
# ---------------------------------------------------------------------------


def lint_source(
    source: str,
    *,
    filename: str = "<strategy>",
    strict: bool = False,
) -> LintReport:
    """对策略源码做前视偏差静态检查（不执行代码）。

    Args:
        source: Python 源码文本。
        filename: 报告中显示的文件名（仅展示用）。
        strict: True 时 WARN 也阻断（P4 LLM 生成策略入口强制）。

    Raises:
        ValueError: 源码存在语法错误（无法解析，fail-fast）。
    """
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as exc:
        raise ValueError(f"策略源码存在语法错误，无法 lint: {filename}: {exc}") from exc

    lines = source.splitlines()
    visitor = _LookaheadVisitor(lines)
    visitor.visit(tree)
    waivers, exempt_findings = _parse_waivers(lines)
    tagged, dead = _apply_waivers(visitor.raw, waivers, lines)
    return LintReport(
        filename=filename,
        strict=strict,
        findings=sorted(tagged + exempt_findings + dead, key=lambda f: f.lineno),
        waivers=waivers,
    )


def lint_file(path: str | Path, *, strict: bool = False) -> LintReport:
    """读取策略文件并 lint（只做文本读取，不执行代码）。

    用 utf-8-sig：Windows 编辑器保存的 UTF-8 BOM 文件 Python 本身可运行，
    linter 不应误判（BOM 存在时剥离，无 BOM 时行为与 utf-8 相同）。
    """
    p = Path(path)
    source = p.read_text(encoding="utf-8-sig")
    return lint_source(source, filename=str(p), strict=strict)


def format_report(report: LintReport) -> str:
    """人类可读文本（供 CLI / 日志 / 回测入口错误信息直接展示）。"""
    if not report.findings:
        return f"{report.filename}: 前视偏差检查通过（strict={report.strict}）"
    lines = [f"{report.filename} 前视偏差检查（strict={report.strict}）："]
    for f in report.findings:
        if f.waived:
            tag = "[已豁免]"
        elif f.severity == ERROR or (report.strict and f.severity == WARN):
            tag = "[阻断]"
        else:
            tag = "[告警]"
        lines.append(f"  {f.lineno}:{f.col} {f.severity} {f.rule_id} {tag} {f.message}")
        if f.line_text:
            lines.append(f"      | {f.line_text}")
        if f.waived and f.waiver_reason:
            lines.append(f"      | 豁免理由：{f.waiver_reason}")
    lines.append(f"结论：{'通过' if report.passed else '不通过'}")
    return "\n".join(lines)
