"""P1.5 自算估值引擎（纯计算，离线可用）。

不依赖 QMT 终端、不触网、不从 store 层读库。输入是调用方（sync.py）
从 DuckDB 查好的 5 张表 DataFrame。输出是 fact_valuation 表的行集。

关键设计：
- 前视偏差防控：所有财务/分红数据按 ann_date / ex_date ≤ trade_date 过滤。
- 财务指标：先在 financial 表按 code+ann_date 排序预计算 rolling 4 季 EPS/营收，
  再用 pandas.merge_asof 按 trade_date 对齐（direction='backward'）一次取齐。
- 股息率：HK 市场扣 hk_connect.dividend_tax_h_share 税率，A 股不扣。
- PE 历史分位：对每组 code 的 pe_ttm 做 rolling(窗口).rank(pct=True)。
- 总股本暂缺，market_cap 与 ps_ttm 留 NaN；接口预留。
- 分母为 0 → 指标留 NaN（与公开行情软件对齐）。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

# data 层只 import 下层：config / store / alerting
from viqtrader.config.loader import Config

# ---------------------------------------------------------------------------
# fact_valuation 列契约（与 0002_domain_schema.sql 严格对齐）
# ---------------------------------------------------------------------------
FACT_VALUATION_COLUMNS: tuple[str, ...] = (
    "code",
    "trade_date",
    "pe_ttm",
    "pb",
    "ps_ttm",
    "dv_ratio",
    "market_cap",
    "pe_percentile",
)

# 估值引擎输出的主键（与 fact_valuation 主键一致）
FACT_VALUATION_KEY: tuple[str, ...] = ("code", "trade_date")


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _safe_div(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    """安全除法：分母为 0 / NaN 时返回 NaN 而非 0。"""
    out = numerator / denominator.replace(0, np.nan)
    return out.where(denominator.notna(), np.nan)


def _prepare_financial(fin_df: pd.DataFrame) -> pd.DataFrame:
    """在 financial 表上按 code+ann_date 排序、预计算滚动 4 季 EPS/营收。

    返回新 DataFrame，新增列：
        - eps_ttm: 最近 4 季 eps 之和
        - ps_ttm_raw: 最近 4 季 revenue 之和（尚未除以总股本）
        - bps_latest: 当期 bps（直接透传，便于 merge_asof 取最近一期）
    """
    if fin_df.empty:
        return fin_df.assign(eps_ttm=np.nan, ps_ttm_raw=np.nan, bps_latest=np.nan)

    df = fin_df.sort_values(["code", "ann_date"], kind="stable").reset_index(drop=True)

    df["eps_ttm"] = df.groupby("code", sort=False)["eps"].transform(
        lambda s: s.rolling(4, min_periods=1).sum()
    )
    df["ps_ttm_raw"] = df.groupby("code", sort=False)["revenue"].transform(
        lambda s: s.rolling(4, min_periods=1).sum()
    )
    df["bps_latest"] = df["bps"]

    return df


def _prepare_dividend(div_df: pd.DataFrame) -> pd.DataFrame:
    """过滤只保留现金分红，按 code+ex_date 排序，便于 merge_asof 取最近一次。"""
    if div_df.empty:
        return div_df.copy()

    cash = div_df[div_df["dividend_type"] == "现金"].copy()
    cash = cash.sort_values(["code", "ex_date"], kind="stable").reset_index(drop=True)
    return cash


# ---------------------------------------------------------------------------
# 核心计算
# ---------------------------------------------------------------------------


def compute(
    bar_df: pd.DataFrame,
    fin_df: pd.DataFrame,
    div_df: pd.DataFrame,
    fx_df: pd.DataFrame,  # P1.5 未用到，P2 港股通换汇时启用
    dim_df: pd.DataFrame,
    *,
    config: Config | dict[str, Any],
) -> pd.DataFrame:
    """计算估值指标，返回 fact_valuation 列契约的 DataFrame。

    参数:
        bar_df:  fact_daily_bar 表（未复权日线）
        fin_df:  fact_financial 表
        div_df:  fact_dividend 表
        fx_df:   fact_fx_rate 表（P1.5 占位，暂不参与计算）
        dim_df:  dim_stock 表
        config:  Config 对象或等同 dict，读取 data.valuation.* 与 hk_connect.*

    返回:
        DataFrame，列严格等于 FACT_VALUATION_COLUMNS，按 (code, trade_date) 主键。
        输入为空或无交集时返回空 DataFrame（保留正确列与 dtype）。
    """
    # --- 0. 读配置（dict 兼容单测注入） ---
    if isinstance(config, Config):
        tax_rate: float = float(config.get("hk_connect.dividend_tax_h_share", 0.20))
        pctl_window: int = int(config.get("data.valuation.pe_percentile_window_days", 250))
        pctl_min_samples: int = int(config.get("data.valuation.pe_percentile_min_samples", 60))
    else:
        hk_cfg = config.get("hk_connect", {})
        val_cfg = config.get("data", {}).get("valuation", {})
        tax_rate = float(hk_cfg.get("dividend_tax_h_share", 0.20))
        pctl_window = int(val_cfg.get("pe_percentile_window_days", 250))
        pctl_min_samples = int(val_cfg.get("pe_percentile_min_samples", 60))

    # --- 1. 日线为基准，左连 dim_stock 拿 market ---
    if bar_df.empty:
        return _empty_valuation_frame()

    dim_cols = ["code", "market"]
    bars = bar_df.merge(
        dim_df[dim_cols] if not dim_df.empty else pd.DataFrame(columns=dim_cols),
        on="code",
        how="left",
    )
    # merge_asof 要求 left_on 在整个 DataFrame 上单调，
    # 所以先按 trade_date 排序；by="code" 让它在组内正确 asof
    bars["trade_date"] = pd.to_datetime(bars["trade_date"])
    bars = bars.sort_values("trade_date", kind="stable").reset_index(drop=True)

    # --- 2. 财务：预计算 + merge_asof 对齐 ---
    fin_prep = _prepare_financial(fin_df)
    if not fin_prep.empty:
        fin_prep["ann_date"] = pd.to_datetime(fin_prep["ann_date"])
        # merge_asof 要求 right_on 在整个 DataFrame 上单调
        fin_prep = fin_prep.sort_values("ann_date", kind="stable").reset_index(drop=True)

        bars = pd.merge_asof(
            bars,
            fin_prep[["code", "ann_date", "eps_ttm", "ps_ttm_raw", "bps_latest"]],
            left_on="trade_date",
            right_on="ann_date",
            by="code",
            direction="backward",
        )
    else:
        bars["eps_ttm"] = np.nan
        bars["ps_ttm_raw"] = np.nan
        bars["bps_latest"] = np.nan

    # --- 3. 分红：merge_asof 拿最近一次现金分红 ---
    div_prep = _prepare_dividend(div_df)
    if not div_prep.empty:
        div_prep["ex_date"] = pd.to_datetime(div_prep["ex_date"])
        div_prep = div_prep.sort_values("ex_date", kind="stable").reset_index(drop=True)

        bars = pd.merge_asof(
            bars,
            div_prep[["code", "ex_date", "dividend_per_share"]],
            left_on="trade_date",
            right_on="ex_date",
            by="code",
            direction="backward",
        )
    else:
        bars["dividend_per_share"] = np.nan

    # --- 4. 计算估值指标 ---
    close = bars["close"]
    eps_ttm = bars["eps_ttm"]
    bps = bars["bps_latest"]
    dps = bars["dividend_per_share"]

    # PE-TTM = close / eps_ttm
    bars["pe_ttm"] = _safe_div(close, eps_ttm)

    # PB = close / bps
    bars["pb"] = _safe_div(close, bps)

    # PS-TTM = close / ps_ttm；总股本暂缺（dim_stock 无 shares_outstanding），
    # 先用 ps_ttm_raw 占位但最终留 NaN；等未来补 total_shares 后启用。
    bars["ps_ttm"] = np.nan

    # 股息率 = dps / close；HK 市场扣税
    is_hk = bars["market"].fillna("") == "HK"
    dps_adj = dps.where(~is_hk, dps * (1.0 - tax_rate))
    bars["dv_ratio"] = _safe_div(dps_adj, close)

    # 总市值暂缺（dim_stock 无 shares_outstanding），留 NaN
    bars["market_cap"] = np.nan

    # --- 5. PE 历史分位 ---
    bars["pe_percentile"] = _rolling_rank_pct(
        bars, window=pctl_window, min_samples=pctl_min_samples
    )

    # --- 6. 裁剪到 fact_valuation 列契约 ---
    result = bars[list(FACT_VALUATION_COLUMNS)].copy()
    # trade_date 统一回 DATE（datetime64[ns] 是 DuckDB DATE 兼容格式）
    result["trade_date"] = pd.to_datetime(result["trade_date"])
    return result.reset_index(drop=True)


# ---------------------------------------------------------------------------
# PE 历史分位：rolling rank(pct=True)
# ---------------------------------------------------------------------------


def _rolling_rank_pct(df: pd.DataFrame, *, window: int, min_samples: int) -> pd.Series:
    """在每个 code 组内，对 pe_ttm 做 rolling(window).rank(pct=True)。

    窗口内有效样本 < min_samples 时返回 NaN（pandas rolling.rank 的
    min_periods 参数即为此用）。
    """
    pe = df["pe_ttm"]
    # rolling 不支持 rank，用 groupby.transform 对每组序列独立做 rolling().apply()
    return pe.groupby(df["code"], sort=False).transform(
        lambda s: s.rolling(window, min_periods=min_samples).apply(
            lambda w: pd.Series(w).rank(pct=True).iloc[-1], raw=False
        )
    )


# ---------------------------------------------------------------------------
# 空帧构造（无数据 / 测试入口复用）
# ---------------------------------------------------------------------------


def _empty_valuation_frame() -> pd.DataFrame:
    """构造符合 fact_valuation 列契约的空 DataFrame（含正确 dtype）。"""
    dtypes: dict[str, str] = {
        "code": "object",
        "trade_date": "datetime64[ns]",
        "pe_ttm": "float64",
        "pb": "float64",
        "ps_ttm": "float64",
        "dv_ratio": "float64",
        "market_cap": "float64",
        "pe_percentile": "float64",
    }
    return pd.DataFrame({col: pd.Series(dtype=dtypes[col]) for col in FACT_VALUATION_COLUMNS})
