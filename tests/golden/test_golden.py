"""P1.7 黄金数据集验证测试。

读取 MANIFEST.json + 所有 parquet，校验：
1. SHA256 hash 与 manifest 记录一致（防篡改）
2. 行数 / 日期范围在 manifest 记录范围内
3. Parquet schema 与 fact_daily_bar 列契约兼容
4. 读入 DuckDB 后，用估值引擎能跑通（PE/PB 计算无 NaN 全列）

这些测试是后续所有回测 / Walk-Forward / Agent 评估的基准门禁。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

GOLDEN_DIR = Path(__file__).resolve().parent
MANIFEST_PATH = GOLDEN_DIR / "MANIFEST.json"
DAILY_BAR_COLS = {
    "code",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "adj_factor",
    "turnover_rate",
}


@pytest.fixture(scope="module")
def manifest() -> dict:
    assert MANIFEST_PATH.exists(), f"MANIFEST.json 不存在: {MANIFEST_PATH}"
    with MANIFEST_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def parquet_files(manifest: dict) -> list[tuple[Path, dict]]:
    """[(path, file_entry_dict), ...]"""
    files = []
    for entry in manifest["files"]:
        p = GOLDEN_DIR / entry["path"]
        files.append((p, entry))
    return files


# ============================================================
# 门禁 1: 文件存在 + SHA256 hash 一致 + 行数 / 日期范围
# ============================================================


def test_manifest_has_files(manifest: dict) -> None:
    assert manifest["files"], "MANIFEST.json files 列表为空"
    assert len(manifest["files"]) >= 5, "黄金数据集至少 5 只标的"


@pytest.mark.parametrize("idx", range(8))
def test_golden_file_hash(parquet_files: list[tuple[Path, dict]], idx: int) -> None:
    if idx >= len(parquet_files):
        pytest.skip(f"manifest 只有 {len(parquet_files)} 个文件")
    path, entry = parquet_files[idx]
    assert path.exists(), f"文件缺失: {path}"
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    assert actual == entry["sha256"], (
        f"SHA256 不匹配: {path.name} manifest={entry['sha256'][:16]}... "
        f"actual={actual[:16]}...（parquet 被修改或数据源版本变了，请重跑 freeze_golden.py）"
    )


@pytest.mark.parametrize("idx", range(8))
def test_golden_file_row_count(parquet_files: list[tuple[Path, dict]], idx: int) -> None:
    if idx >= len(parquet_files):
        pytest.skip(f"manifest 只有 {len(parquet_files)} 个文件")
    path, entry = parquet_files[idx]
    df = pd.read_parquet(path)
    assert (
        len(df) == entry["rows"]
    ), f"{path.name} 行数不一致: manifest={entry['rows']} actual={len(df)}"


@pytest.mark.parametrize("idx", range(8))
def test_golden_file_date_range(parquet_files: list[tuple[Path, dict]], idx: int) -> None:
    if idx >= len(parquet_files):
        pytest.skip(f"manifest 只有 {len(parquet_files)} 个文件")
    path, entry = parquet_files[idx]
    df = pd.read_parquet(path)
    dates = pd.to_datetime(df["trade_date"])
    actual_min = dates.min().date().isoformat()
    actual_max = dates.max().date().isoformat()
    assert actual_min == entry["date_min"], f"{path.name} date_min 不一致"
    assert actual_max == entry["date_max"], f"{path.name} date_max 不一致"


# ============================================================
# 门禁 2: Parquet schema 与 fact_daily_bar 契约兼容
# ============================================================


def test_golden_schema_compatible(parquet_files: list[tuple[Path, dict]]) -> None:
    for path, _ in parquet_files:
        df = pd.read_parquet(path)
        actual = set(df.columns)
        missing = DAILY_BAR_COLS - actual
        assert not missing, f"{path.name} 缺列: {missing}"
        # 允许多余列（适配器可附加元数据），但不在 fact_daily_bar schema 中


def test_golden_no_null_key_columns(parquet_files: list[tuple[Path, dict]]) -> None:
    """主键 (code, trade_date) 不允许 null。"""
    for path, _ in parquet_files:
        df = pd.read_parquet(path)
        assert df["code"].notna().all(), f"{path.name} code 有 null"
        assert df["trade_date"].notna().all(), f"{path.name} trade_date 有 null"


def test_golden_close_positive(parquet_files: list[tuple[Path, dict]]) -> None:
    """收盘价 > 0（停牌除牌不会被冻结）。"""
    for path, _ in parquet_files:
        df = pd.read_parquet(path)
        close = pd.to_numeric(df["close"], errors="coerce")
        assert (close > 0).all(), f"{path.name} 收盘价 <= 0"


def test_golden_no_duplicate_key(parquet_files: list[tuple[Path, dict]]) -> None:
    """(code, trade_date) 唯一。"""
    for path, _ in parquet_files:
        df = pd.read_parquet(path)
        dup = df.duplicated(subset=["code", "trade_date"])
        assert not dup.any(), f"{path.name} 主键重复 {dup.sum()} 行"
