"""P1.7 黄金数据集冻结脚本。

从 AkShare 新浪源（QMT 就绪前的临时方案）拉取 A股 + 港股通各代表股的 3 年日线，
写成 parquet 到 tests/golden/ 并生成 MANIFEST.json。

正式 QMT 适配器就绪后，用同一脚本（替换数据源实现）重跑即可刷新 hash；
MANIFEST.json 会记录数据源标识以便区分版本。

Usage:
    .venv\\Scripts\\python.exe scripts/freeze_golden.py
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

import pandas as pd

GOLDEN_DIR = Path(__file__).resolve().parent.parent / "tests" / "golden"
START = date(2022, 1, 1)
END = date(2024, 12, 31)

# 代表股：A股价值型/波段型 + 港股通各几只（共 8 只）
# sh=上海, sz=深圳, HK=港股
SYMBOLS: list[dict[str, str]] = [
    {"market": "SH", "code": "600519", "name": "贵州茅台", "style": "value"},
    {"market": "SH", "code": "600900", "name": "长江电力", "style": "value"},
    {"market": "SH", "code": "601088", "name": "中国神华", "style": "value"},
    {"market": "SZ", "code": "000858", "name": "五粮液", "style": "value"},
    {"market": "SZ", "code": "300750", "name": "宁德时代", "style": "swing"},
    {"market": "SZ", "code": "002594", "name": "比亚迪", "style": "swing"},
    {"market": "HK", "code": "00700", "name": "腾讯控股", "style": "hk_connect"},
    {"market": "HK", "code": "03690", "name": "美团-W", "style": "hk_connect"},
]


# ---------------------------------------------------------------------------
# AkShare 取数（新浪源，绕过东财 push2 域）
# ---------------------------------------------------------------------------


def _fetch_a_share_daily(market: str, code: str) -> pd.DataFrame:
    """A 股日线：stock_zh_a_daily（新浪 qfq 前复权）。"""
    import akshare as ak  # 延迟 import

    prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj"}[market]
    df = ak.stock_zh_a_daily(
        symbol=f"{prefix}{code}",
        start_date=START.strftime("%Y%m%d"),
        end_date=END.strftime("%Y%m%d"),
        adjust="qfq",
    )
    return df


def _fetch_hk_daily(code: str) -> pd.DataFrame:
    """港股日线：stock_hk_daily（新浪 qfq 前复权）。

    stock_hk_daily 无日期范围参数，返回全量 → 本地切片。
    """
    import akshare as ak  # 延迟 import

    df = ak.stock_hk_daily(symbol=code, adjust="qfq")
    # 切片到 [START, END]
    df["date"] = pd.to_datetime(df["date"])
    mask = df["date"].between(pd.Timestamp(START), pd.Timestamp(END))
    return df.loc[mask].reset_index(drop=True)


# ---------------------------------------------------------------------------
# 归一化到 fact_daily_bar schema
# ---------------------------------------------------------------------------


def _map_a_share(raw: pd.DataFrame, code: str, market: str) -> pd.DataFrame:
    """新浪 stock_zh_a_daily → fact_daily_bar。

    raw columns: date, open, high, low, close, volume, amount,
                 outstanding_share, turnover
    turnover 已是流通盘换手率（小数）。
    """
    out = pd.DataFrame(
        {
            "code": code,
            "trade_date": pd.to_datetime(raw["date"]),
            "open": raw["open"].astype("float64"),
            "high": raw["high"].astype("float64"),
            "low": raw["low"].astype("float64"),
            "close": raw["close"].astype("float64"),
            "volume": raw["volume"].astype("float64"),
            "amount": raw["amount"].astype("float64"),
            "adj_factor": pd.NA,  # qfq 已复权，不用 adj_factor
            "turnover_rate": raw["turnover"].astype("float64"),
        }
    )
    out["code"] = out["code"].astype("string")
    return out


def _map_hk(raw: pd.DataFrame, code: str) -> pd.DataFrame:
    """新浪 stock_hk_daily → fact_daily_bar（HK）。

    raw columns: date, open, high, low, close, volume, amount
    新浪港股无 turnover / adj_factor → 留空。
    """
    count = len(raw)
    out = pd.DataFrame(
        {
            "code": [code] * count,
            "trade_date": pd.to_datetime(raw["date"]),
            "open": raw["open"].astype("float64"),
            "high": raw["high"].astype("float64"),
            "low": raw["low"].astype("float64"),
            "close": raw["close"].astype("float64"),
            "volume": raw["volume"].astype("float64"),
            "amount": raw["amount"].astype("float64"),
            "adj_factor": pd.Series([pd.NA] * count, dtype="object"),
            "turnover_rate": pd.Series([pd.NA] * count, dtype="object"),
        }
    )
    out["code"] = out["code"].astype("string")
    return out


# ---------------------------------------------------------------------------
# Parquet 写出 + hash 计算
# ---------------------------------------------------------------------------


def _write_parquet(df: pd.DataFrame, path: Path) -> str:
    """写 parquet，返回文件的 SHA256 hex。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False, compression="zstd")
    return _sha256(path)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# MANIFEST
# ---------------------------------------------------------------------------


@dataclass
class FileEntry:
    path: str
    sha256: str
    rows: int
    date_min: str
    date_max: str
    source: str
    market: str
    code: str
    style: str


def _build_manifest(entries: list[FileEntry]) -> dict:
    return {
        "generated_at": pd.Timestamp.now().isoformat(),
        "start_date": START.isoformat(),
        "end_date": END.isoformat(),
        "data_source": "akshare-sina-temporary",
        "notes": (
            "临时方案：用 AkShare 新浪源日线接口冻结。"
            "QMT 适配器就绪后应替换数据源重跑以获得更可靠的行情。"
        ),
        "files": [asdict(e) for e in entries],
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main() -> None:
    import akshare as ak

    print(f"AkShare version: {ak.__version__}")
    print(f"Golden dir: {GOLDEN_DIR}")
    print(f"Date range: {START} ~ {END}")
    print(f"Symbols: {len(SYMBOLS)}")
    print()

    entries: list[FileEntry] = []

    for sym in SYMBOLS:
        market = sym["market"]
        code = sym["code"]
        name = sym["name"]
        style = sym["style"]
        fname = f"{market}_{code}_daily_bar.parquet"
        fpath = GOLDEN_DIR / fname

        print(f"[{market} {code} {name}] fetching ... ", end="", flush=True)
        t0 = time.time()
        try:
            if market == "HK":
                raw = _fetch_hk_daily(code)
                df = _map_hk(raw, code)
            else:
                raw = _fetch_a_share_daily(market, code)
                df = _map_a_share(raw, code, market)
        except Exception as exc:
            print(f"FAIL ({type(exc).__name__}: {exc})")
            continue

        elapsed = time.time() - t0
        sha = _write_parquet(df, fpath)
        date_min = df["trade_date"].min().date().isoformat()
        date_max = df["trade_date"].max().date().isoformat()
        print(f"{len(df):4d} rows [{date_min} ~ {date_max}] sha={sha[:16]}... ({elapsed:.1f}s)")

        entries.append(
            FileEntry(
                path=fname,
                sha256=sha,
                rows=len(df),
                date_min=date_min,
                date_max=date_max,
                source="akshare-sina",
                market=market,
                code=code,
                style=style,
            )
        )
        time.sleep(0.5)  # 限速，新浪源对过快请求较敏感

    manifest = _build_manifest(entries)
    manifest_path = GOLDEN_DIR / "MANIFEST.json"
    with manifest_path.open("w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)

    print()
    print(f"Manifest written: {manifest_path}")
    print(f"Total: {len(entries)} symbols frozen")


if __name__ == "__main__":
    main()
