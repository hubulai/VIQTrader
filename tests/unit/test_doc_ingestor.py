"""P1.8 研报入库管线单测。

用假 PdfExtractor 注入文本，不依赖真 PDF；测试去重、元数据解析、入库结果。
"""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

import duckdb
import pytest

from viqtrader.data.docs.ingestor import (
    DocumentIngestor,
    extract_metadata_from_filename,
)
from viqtrader.store import migrate as _migrate


@pytest.fixture
def conn() -> duckdb.DuckDBPyConnection:
    c = duckdb.connect(":memory:")
    _migrate.migrate(c)
    return c


class _FakeExtractor:
    """可注入的假 PDF 提取器：按文件名返回预置文本。"""

    def __init__(self, texts: dict[str, str]) -> None:
        self._texts = texts
        self.calls: list[str] = []

    def extract_text(self, file_path: str) -> str:
        self.calls.append(file_path)
        name = Path(file_path).name
        if name not in self._texts:
            return ""
        return self._texts[name]


def _write_pdf(path: Path, text: str = "") -> Path:
    """写一个伪 PDF（提取由假 extractor 处理，文件内容任意）。"""
    path.write_bytes(b"%PDF-1.4 fake")
    return path


# ---------------------------------------------------------------------------
# 文件名元数据解析
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fname", "expected"),
    [
        (
            "【国泰君安】贵州茅台-600519-2024三季报点评.pdf",
            {"org": "国泰君安", "code": "600519", "doc_type": "个股研报", "period": "2024三季报"},
        ),
        (
            "[中金公司]2024年食品饮料行业深度报告.pdf",
            {"org": "中金公司", "industry": "行业", "doc_type": "行业研报"},
        ),
        (
            "宁德时代-300750-2024Q3业绩预告20241015.pdf",
            {"code": "300750", "doc_type": "个股研报", "publish_date": date(2024, 10, 15)},
        ),
        ("腾讯控股00700投资分析.pdf", {"code": "00700", "doc_type": "个股研报"}),
        ("随便一个文件.pdf", {"doc_type": "其他", "code": None}),
    ],
)
def test_extract_metadata(fname: str, expected: dict) -> None:
    meta = extract_metadata_from_filename(fname)
    for key, val in expected.items():
        assert getattr(meta, key) == val, f"{key}: {getattr(meta, key)} != {val}"


def test_metadata_name_strips_noise() -> None:
    meta = extract_metadata_from_filename("【国君】贵州茅台-600519-三季报点评.pdf")
    assert "国君" not in (meta.name or "")
    assert "600519" not in (meta.name or "")
    assert "贵州茅台" in (meta.name or "")


# ---------------------------------------------------------------------------
# 入库主流程
# ---------------------------------------------------------------------------


def test_ingest_single_file(conn: duckdb.DuckDBPyConnection, tmp_path: Path) -> None:
    pdf = _write_pdf(tmp_path / "【国君】茅台-600519.pdf", "fake text")
    ingestor = DocumentIngestor(conn, extractor=_FakeExtractor({pdf.name: "研报正文内容"}))
    result = ingestor.ingest_file(pdf)

    assert result.status == "inserted"
    assert result.doc_id and result.content_hash
    assert result.content_hash == hashlib.sha256("研报正文内容".encode()).hexdigest()

    row = conn.execute(
        "SELECT doc_type, code, org, parse_status FROM document WHERE doc_id = ?",
        [result.doc_id],
    ).fetchone()
    assert row[0] == "个股研报"
    assert row[1] == "600519"
    assert row[2] == "国君"
    assert row[3] == "pending"


def test_ingest_dedup_by_content_hash(conn: duckdb.DuckDBPyConnection, tmp_path: Path) -> None:
    """同一份内容改名后第二次入库应判 duplicate，不写第二行。"""
    pdf1 = _write_pdf(tmp_path / "a.pdf")
    pdf2 = _write_pdf(tmp_path / "b.pdf")
    ingestor = DocumentIngestor(
        conn, extractor=_FakeExtractor({"a.pdf": "相同正文", "b.pdf": "相同正文"})
    )

    r1 = ingestor.ingest_file(pdf1)
    r2 = ingestor.ingest_file(pdf2)

    assert r1.status == "inserted"
    assert r2.status == "duplicate"
    assert r2.doc_id == r1.doc_id  # 指向同一条记录
    assert conn.execute("SELECT COUNT(*) FROM document").fetchone()[0] == 1


def test_ingest_different_content_not_dedup(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    pdf1 = _write_pdf(tmp_path / "a.pdf")
    pdf2 = _write_pdf(tmp_path / "b.pdf")
    ingestor = DocumentIngestor(
        conn, extractor=_FakeExtractor({"a.pdf": "正文A", "b.pdf": "正文B"})
    )

    assert ingestor.ingest_file(pdf1).status == "inserted"
    assert ingestor.ingest_file(pdf2).status == "inserted"
    assert conn.execute("SELECT COUNT(*) FROM document").fetchone()[0] == 2


def test_ingest_empty_text_skipped(conn: duckdb.DuckDBPyConnection, tmp_path: Path) -> None:
    pdf = _write_pdf(tmp_path / "empty.pdf")
    ingestor = DocumentIngestor(conn, extractor=_FakeExtractor({"empty.pdf": "   \n  "}))
    r = ingestor.ingest_file(pdf)
    assert r.status == "skipped"
    assert conn.execute("SELECT COUNT(*) FROM document").fetchone()[0] == 0


def test_ingest_extractor_failure(conn: duckdb.DuckDBPyConnection, tmp_path: Path) -> None:
    class _BoomExtractor:
        def extract_text(self, file_path: str) -> str:
            raise RuntimeError("corrupt pdf")

    pdf = _write_pdf(tmp_path / "bad.pdf")
    ingestor = DocumentIngestor(conn, extractor=_BoomExtractor())
    r = ingestor.ingest_file(pdf)
    assert r.status == "failed"
    assert "corrupt pdf" in (r.error or "")


def test_ingest_directory(conn: duckdb.DuckDBPyConnection, tmp_path: Path) -> None:
    _write_pdf(tmp_path / "a.pdf")
    _write_pdf(tmp_path / "b.pdf")
    _write_pdf(tmp_path / "readme.txt")  # 非 PDF 应被忽略
    ingestor = DocumentIngestor(conn, extractor=_FakeExtractor({"a.pdf": "A", "b.pdf": "B"}))

    results = ingestor.ingest(tmp_path)
    assert len(results) == 2
    assert all(r.status == "inserted" for r in results)


def test_ingest_nonexistent_dir(conn: duckdb.DuckDBPyConnection) -> None:
    ingestor = DocumentIngestor(conn, extractor=_FakeExtractor({}))
    assert ingestor.ingest("/no/such/dir") == []


def test_content_hash_is_text_not_path(conn: duckdb.DuckDBPyConnection, tmp_path: Path) -> None:
    """hash 基于文本内容，与文件路径无关。"""
    pdf = _write_pdf(tmp_path / "x.pdf")
    ingestor = DocumentIngestor(conn, extractor=_FakeExtractor({"x.pdf": "固定文本"}))
    r = ingestor.ingest_file(pdf)
    assert r.content_hash == hashlib.sha256("固定文本".encode()).hexdigest()


def test_inserted_doc_id_unique(conn: duckdb.DuckDBPyConnection, tmp_path: Path) -> None:
    pdf1 = _write_pdf(tmp_path / "a.pdf")
    pdf2 = _write_pdf(tmp_path / "b.pdf")
    ingestor = DocumentIngestor(conn, extractor=_FakeExtractor({"a.pdf": "A", "b.pdf": "B"}))
    r1 = ingestor.ingest_file(pdf1)
    r2 = ingestor.ingest_file(pdf2)
    assert r1.doc_id != r2.doc_id
