"""P1.8 研报文档库入库管线。

职责：扫描 inbox_dir 下的 PDF → 提取文本 → 内容 SHA256 去重 → document 表落库。
LLM 结构化解析（document_analysis / analyst_forecast）属 P4.8 研报解析 Agent，
本管线只负责入库，parse_status 默认 'pending' 等待 P4.8 消费。

设计要点：
- 内容 hash 取「全文文本」的 SHA256（不含文件名/路径）→ 同一份研报改名后仍能去重
- 元数据从文件名正则解析（机构/标的代码/行业/报告周期/发布日期），解析不到留空
- PDF 提取经 PdfExtractor Protocol 缝隙，默认 pdfplumber（文本+表格），测试可注入假实现
- doc_type：含「行业/策略」→ 行业研报；命中 6 位股票代码 → 个股研报；否则 其他

分层契约：data 层只 import store，不 import engine / agents。
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import duckdb

__all__ = [
    "DocIngestResult",
    "DocMeta",
    "DocumentIngestor",
    "PdfExtractor",
    "extract_metadata_from_filename",
]

# 允许的扩展名（大小写不敏感）
_PDF_EXTS = frozenset({".pdf"})


# ---------------------------------------------------------------------------
# PDF 提取缝隙 Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class PdfExtractor(Protocol):
    """PDF 文本提取缝隙：默认 pdfplumber，测试可注入假实现。"""

    def extract_text(self, file_path: str) -> str:
        """返回 PDF 全文文本（页面间以换行拼接）。"""
        ...


def _default_extractor() -> PdfExtractor:
    """默认 pdfplumber 提取器（懒加载；未安装时报清晰错误）。"""
    try:
        import pdfplumber
    except ImportError as exc:
        raise RuntimeError(
            "pdfplumber 未安装：请运行 .venv\\Scripts\\python.exe -m pip install pdfplumber"
        ) from exc
    return cast_to_extractor(pdfplumber)


def cast_to_extractor(pdfplumber_mod: Any) -> PdfExtractor:
    """把 pdfplumber 模块适配为 PdfExtractor（pdfplumber.open + pages.extract_text）。"""

    class _PdfplumberExtractor:
        def extract_text(self, file_path: str) -> str:
            texts: list[str] = []
            with pdfplumber_mod.open(file_path) as pdf:
                for page in pdf.pages:
                    t = page.extract_text()
                    if t:
                        texts.append(t)
            return "\n".join(texts)

    return _PdfplumberExtractor()


# ---------------------------------------------------------------------------
# 文件名元数据解析
# ---------------------------------------------------------------------------

# 机构：【国泰君安】 / [中金公司] / (华泰证券)
_ORG_PATTERNS = [
    re.compile(r"【([^】]+)】"),
    re.compile(r"\[([^\]]+)\]"),
    re.compile(r"\(([^)]+)\)"),
]
# A股 6 位代码 / 港股 5 位：前后不能是数字（兼容与中文相邻）
_CODE_PATTERN = re.compile(r"(?<!\d)(\d{5,6})(?!\d)")
# 报告周期：2024三季报 / 2024Q3 / 2024年报 / 2024中报 / 2024一季报
_PERIOD_PATTERN = re.compile(r"(20\d{2})\s*(年)?\s*(三季报|中报|年报|一季报|半年报|季报|[Qq]\d)")
# 发布日期：20241015 / 2024-10-15
_DATE_PATTERN = re.compile(r"(20\d{2})[-/]?(\d{2})[-/]?(\d{2})")


@dataclass(frozen=True)
class DocMeta:
    """从文件名解析出的元数据；无法解析的字段为 None。"""

    doc_type: str = "其他"  # 个股研报 / 行业研报 / 其他
    code: str | None = None
    industry: str | None = None
    org: str | None = None
    publish_date: date | None = None
    period: str | None = None  # 2024Q3 / 2024年报 等
    name: str | None = None  # 从文件名剥离机构/代码后剩余的标题


def extract_metadata_from_filename(filename: str) -> DocMeta:
    """从 PDF 文件名解析研报元数据；无任何匹配时返回默认 DocMeta。"""
    stem = Path(filename).stem

    org: str | None = None
    for pat in _ORG_PATTERNS:
        m = pat.search(stem)
        if m:
            org = m.group(1).strip()
            break

    # 报告周期
    period = None
    pm = _PERIOD_PATTERN.search(stem)
    if pm:
        year, _, tag = pm.groups()
        period = f"{year}{tag}"

    # 发布日期（在代码匹配前从 stem 中剥离，避免日期数字被误识别为股票代码）
    publish_date = None
    dm = _DATE_PATTERN.search(stem)
    if dm:
        try:
            publish_date = date(int(dm.group(1)), int(dm.group(2)), int(dm.group(3)))
        except ValueError:
            publish_date = None

    # 代码：在剥离日期与周期的副本上匹配，避免误识别
    code_stem = _DATE_PATTERN.sub("", stem)
    code_stem = _PERIOD_PATTERN.sub("", code_stem)
    code_match = _CODE_PATTERN.search(code_stem)
    code = code_match.group(1) if code_match else None

    industry = None
    ind_match = re.search(r"(行业|策略|宏观|专题)", stem)
    if ind_match:
        industry = ind_match.group(1)

    # doc_type 判定
    if industry:
        doc_type = "行业研报"
    elif code:
        doc_type = "个股研报"
    else:
        doc_type = "其他"

    # 标题：去掉机构括号、代码、日期后的剩余文本
    raw_name = stem
    for pat in _ORG_PATTERNS:
        raw_name = pat.sub("", raw_name)
    raw_name = _CODE_PATTERN.sub("", raw_name)
    raw_name = _PERIOD_PATTERN.sub("", raw_name)
    raw_name = _DATE_PATTERN.sub("", raw_name)
    raw_name = re.sub(r"[-_\s]+", " ", raw_name).strip(" -_")
    name: str | None = raw_name or None

    return DocMeta(
        doc_type=doc_type,
        code=code,
        industry=industry,
        org=org,
        publish_date=publish_date,
        period=period,
        name=name,
    )


# ---------------------------------------------------------------------------
# 入库结果
# ---------------------------------------------------------------------------


@dataclass
class DocIngestResult:
    file_path: str
    status: str  # inserted / duplicate / skipped / failed
    doc_id: str | None = None
    content_hash: str | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# 入库器
# ---------------------------------------------------------------------------


class DocumentIngestor:
    """研报 PDF 入库管线。"""

    def __init__(
        self,
        conn: duckdb.DuckDBPyConnection,
        extractor: PdfExtractor | None = None,
    ) -> None:
        self._conn = conn
        self._extractor = extractor

    def _get_extractor(self) -> PdfExtractor:
        if self._extractor is None:
            self._extractor = _default_extractor()
        return self._extractor

    def ingest(self, inbox_dir: str | Path) -> list[DocIngestResult]:
        """扫描 inbox_dir 下所有 PDF 并入库；返回每份文件的处理结果。"""
        inbox = Path(inbox_dir)
        if not inbox.exists():
            return []
        results: list[DocIngestResult] = []
        for fpath in sorted(inbox.iterdir()):
            if not fpath.is_file() or fpath.suffix.lower() not in _PDF_EXTS:
                continue
            results.append(self._ingest_one(fpath))
        return results

    def ingest_file(self, file_path: str | Path) -> DocIngestResult:
        """入库单个 PDF 文件。"""
        return self._ingest_one(Path(file_path))

    # -- 内部 --------------------------------------------------------------

    def _ingest_one(self, fpath: Path) -> DocIngestResult:
        path_str = str(fpath.resolve())
        try:
            text = self._get_extractor().extract_text(path_str)
        except Exception as exc:
            return DocIngestResult(
                file_path=path_str, status="failed", error=f"PDF 提取失败: {exc}"
            )

        if not text.strip():
            return DocIngestResult(file_path=path_str, status="skipped", error="PDF 无文本内容")

        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()

        # 去重：content_hash UNIQUE
        existing = self._conn.execute(
            "SELECT doc_id FROM document WHERE content_hash = ?", [content_hash]
        ).fetchone()
        if existing:
            return DocIngestResult(
                file_path=path_str,
                status="duplicate",
                doc_id=existing[0],
                content_hash=content_hash,
            )

        meta = extract_metadata_from_filename(fpath.name)
        doc_id = str(uuid.uuid4())
        self._conn.execute(
            """
            INSERT INTO document
                (doc_id, file_path, content_hash, doc_type, code, industry,
                 org, publish_date, parse_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')
            """,
            [
                doc_id,
                path_str,
                content_hash,
                meta.doc_type,
                meta.code,
                meta.industry,
                meta.org,
                meta.publish_date,
            ],
        )
        return DocIngestResult(
            file_path=path_str,
            status="inserted",
            doc_id=doc_id,
            content_hash=content_hash,
        )
