"""估值引擎离线单测（不触网、不点 QMT）。

覆盖：公式正确性、港股通税后股息率 vs A 股税前、前视偏差防控、
分母为 0 留 NaN、PE 历史分位样本门槛。
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from viqtrader.data import valuation as v

# ---------------------------------------------------------------------------
# 构造输入 DataFrame 的 fixtures
# ---------------------------------------------------------------------------


def _trade_dates(n_days: int, start: date = date(2022, 1, 3)) -> list[date]:
    """生成连续交易日序列（简单跳过周末也可以，估值引擎不依赖此细节）。"""
    out: list[date] = []
    d = start
    while len(out) < n_days:
        if d.weekday() < 5:  # 周一到周五
            out.append(d)
        d += timedelta(days=1)
    return out


@pytest.fixture()
def dim_df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "code": "600519",
                "market": "SH",
                "name": "贵州茅台",
                "industry": "白酒",
                "list_date": date(2001, 8, 27),
                "is_hk_connect": False,
                "hk_connect_in_date": None,
                "hk_connect_out_date": None,
            },
            {
                "code": "00700",
                "market": "HK",
                "name": "腾讯控股",
                "industry": "互联网",
                "list_date": date(2004, 6, 16),
                "is_hk_connect": True,
                "hk_connect_in_date": date(2014, 11, 17),
                "hk_connect_out_date": None,
            },
        ]
    )


@pytest.fixture()
def bar_df() -> pd.DataFrame:
    """3 年日线，两只标的，close 恒定便于手算。"""
    dates = _trade_dates(750)  # ≈ 3 年
    rows: list[dict[str, object]] = []
    for dt in dates:
        rows.append(
            {
                "code": "600519",
                "trade_date": dt,
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
                "volume": 1000,
                "amount": 100000.0,
                "adj_factor": 1.0,
                "turnover_rate": 0.001,
            }
        )
        rows.append(
            {
                "code": "00700",
                "trade_date": dt,
                "open": 300.0,
                "high": 302.0,
                "low": 298.0,
                "close": 300.0,
                "volume": 2000,
                "amount": 600000.0,
                "adj_factor": 1.0,
                "turnover_rate": 0.002,
            }
        )
    return pd.DataFrame(rows)


@pytest.fixture()
def fin_df() -> pd.DataFrame:
    """8 季财务（2022Q1 ~ 2023Q4），ann_date 滞后报告期约 2 个月。"""
    rows: list[dict[str, object]] = []
    # A 股 600519
    eps_list_a = [0.8, 0.9, 1.0, 1.1, 1.2, 1.3, 1.4, 1.5]
    rp_a = [
        date(2022, 3, 31),
        date(2022, 6, 30),
        date(2022, 9, 30),
        date(2022, 12, 31),
        date(2023, 3, 31),
        date(2023, 6, 30),
        date(2023, 9, 30),
        date(2023, 12, 31),
    ]
    ann_a = [
        date(2022, 4, 28),
        date(2022, 8, 30),
        date(2022, 10, 28),
        date(2023, 2, 28),
        date(2023, 4, 28),
        date(2023, 8, 30),
        date(2023, 10, 28),
        date(2024, 2, 28),
    ]
    for rp, ann, eps in zip(rp_a, ann_a, eps_list_a, strict=True):
        rows.append(
            {
                "code": "600519",
                "report_period": rp,
                "ann_date": ann,
                "revenue": 100.0,
                "net_profit": 50.0,
                "roe": 0.20,
                "roa": 0.10,
                "gross_margin": 0.50,
                "net_margin": 0.30,
                "debt_ratio": 0.30,
                "ocf": 40.0,
                "fcf": 30.0,
                "eps": eps,
                "bps": 25.0,
                "total_assets": 500.0,
                "total_equity": 300.0,
            }
        )
    # 港股 00700
    eps_list_h = [2.0, 2.2, 2.5, 2.8, 3.0, 3.2, 3.5, 3.8]
    for rp, ann, eps in zip(rp_a, ann_a, eps_list_h, strict=True):
        rows.append(
            {
                "code": "00700",
                "report_period": rp,
                "ann_date": ann,
                "revenue": 200.0,
                "net_profit": 60.0,
                "roe": 0.18,
                "roa": 0.09,
                "gross_margin": 0.45,
                "net_margin": 0.25,
                "debt_ratio": 0.35,
                "ocf": 50.0,
                "fcf": 35.0,
                "eps": eps,
                "bps": 60.0,
                "total_assets": 800.0,
                "total_equity": 600.0,
            }
        )
    return pd.DataFrame(rows)


@pytest.fixture()
def div_df() -> pd.DataFrame:
    rows = [
        {
            "code": "600519",
            "ex_date": date(2024, 6, 10),
            "pay_date": date(2024, 7, 10),
            "dividend_per_share": 1.50,
            "dividend_type": "现金",
        },
        {
            "code": "00700",
            "ex_date": date(2024, 8, 15),
            "pay_date": date(2024, 9, 15),
            "dividend_per_share": 6.00,
            "dividend_type": "现金",
        },
    ]
    return pd.DataFrame(rows)


@pytest.fixture()
def fx_df() -> pd.DataFrame:
    # P1.5 未使用，占位即可
    return pd.DataFrame([{"pair": "HKD/CNY", "trade_date": date(2024, 1, 2), "rate": 0.92}])


@pytest.fixture()
def cfg_dict() -> dict:
    """纯 dict 配置（避免 Config.load 触碰文件系统）。"""
    return {
        "hk_connect": {"dividend_tax_h_share": 0.20, "dividend_tax_red_chip": 0.28},
        "data": {
            "valuation": {
                "pe_percentile_window_days": 250,
                "pe_percentile_min_samples": 60,
            }
        },
    }


# ---------------------------------------------------------------------------
# 主入口：所有共享计算都从这来
# ---------------------------------------------------------------------------


@pytest.fixture()
def result(bar_df, fin_df, div_df, fx_df, dim_df, cfg_dict) -> pd.DataFrame:
    return v.compute(bar_df, fin_df, div_df, fx_df, dim_df, config=cfg_dict)


def _series(df: pd.DataFrame, code: str, col: str) -> pd.Series:
    return df[df["code"] == code].sort_values("trade_date")[col].reset_index(drop=True)


# ---------------------------------------------------------------------------
# 1. 公式正确性（手工核一个具体数值）
# ---------------------------------------------------------------------------


class TestFormula:
    def test_pe_ttm_a_share(self, result) -> None:
        # trade_date=2024-03-15 对应 ann_date=2024-02-28 的财报；
        # 最近 4 季 eps = 1.2+1.3+1.4+1.5 = 5.4
        # close = 100.0，pe_ttm = 100 / 5.4 ≈ 18.518519
        day = pd.Timestamp("2024-03-15")
        row = result[(result["code"] == "600519") & (result["trade_date"] == day)]
        assert not row.empty
        pe = row["pe_ttm"].iloc[0]
        assert pe == pytest.approx(100.0 / 5.4, rel=1e-6)

    def test_pb_a_share(self, result) -> None:
        # bps 恒定 25.0，pb = 100 / 25 = 4.0
        vals = _series(result, "600519", "pb").dropna()
        assert vals.empty is False
        assert (vals == 4.0).all(), f"pb 应恒为 4.0，实际 {vals.unique()}"

    def test_pe_ttm_hk(self, result) -> None:
        # hk 最近 4 季 eps = 3.0+3.2+3.5+3.8 = 13.5，close=300 → pe = 300/13.5 ≈ 22.2222
        day = pd.Timestamp("2024-03-15")
        row = result[(result["code"] == "00700") & (result["trade_date"] == day)]
        pe = row["pe_ttm"].iloc[0]
        assert pe == pytest.approx(300.0 / 13.5, rel=1e-6)


# ---------------------------------------------------------------------------
# 2. 港股通扣 20% 税 vs A 股不扣
# ---------------------------------------------------------------------------


class TestDividendTax:
    def test_a_share_no_tax(self, result) -> None:
        # 分红 ex_date=2024-06-10；trade_date=2024-07-01 已过除权，dps=1.50
        day = pd.Timestamp("2024-07-01")
        row = result[(result["code"] == "600519") & (result["trade_date"] == day)]
        dv = row["dv_ratio"].iloc[0]
        # A 股不扣税：1.50 / 100 = 0.015
        assert dv == pytest.approx(0.015, rel=1e-6)

    def test_hk_connect_with_tax(self, result) -> None:
        # ex_date=2024-08-15；trade_date=2024-09-02（周一，已过除权）
        day = pd.Timestamp("2024-09-02")
        row = result[(result["code"] == "00700") & (result["trade_date"] == day)]
        dv = row["dv_ratio"].iloc[0]
        # 港股通扣 20%：6.00 * 0.80 / 300 = 0.016
        assert dv == pytest.approx(0.016, rel=1e-6)

    def test_hk_tax_from_config(self, bar_df, fin_df, div_df, fx_df, dim_df) -> None:
        # 改税率为 0.10 验证 dv_ratio 跟着变
        cfg = {
            "hk_connect": {"dividend_tax_h_share": 0.10},
            "data": {
                "valuation": {
                    "pe_percentile_window_days": 250,
                    "pe_percentile_min_samples": 60,
                }
            },
        }
        res = v.compute(bar_df, fin_df, div_df, fx_df, dim_df, config=cfg)
        day = pd.Timestamp("2024-09-02")
        row = res[(res["code"] == "00700") & (res["trade_date"] == day)]
        dv = row["dv_ratio"].iloc[0]
        assert dv == pytest.approx(6.00 * 0.90 / 300.0, rel=1e-6)


# ---------------------------------------------------------------------------
# 3. 前视偏差防控：ann_date > trade_date 的财务数据必须被排除
# ---------------------------------------------------------------------------


class TestForwardBias:
    def test_future_ann_date_excluded(self, bar_df, div_df, fx_df, dim_df, cfg_dict) -> None:
        # 构造一份 fin_df，其中有一个"未来财报"（ann_date 在所有 trade_date 之后）
        # 这份财报的 eps_ttm 会非常大，若被错误纳入，pe_ttm 会显著变化
        fin_future = pd.DataFrame(
            [
                {
                    "code": "600519",
                    "report_period": date(2099, 12, 31),
                    "ann_date": date(2099, 12, 31),
                    "eps": 100.0,
                    "bps": 999.0,
                    "revenue": 9999.0,
                    "net_profit": 900.0,
                    "roe": 0.99,
                    "roa": 0.99,
                    "gross_margin": 0.99,
                    "net_margin": 0.99,
                    "debt_ratio": 0.01,
                    "ocf": 900.0,
                    "fcf": 900.0,
                    "total_assets": 9999.0,
                    "total_equity": 9999.0,
                },
            ]
        )
        from viqtrader.data import valuation as v2

        result_future = v2.compute(bar_df, fin_future, div_df, fx_df, dim_df, config=cfg_dict)

        # 2099 的财报 ann_date 在所有日线之后 → 全量 pe_ttm / pb 都应该是 NaN
        # （因为没有任何 ann_date <= trade_date 的财报）
        vals = _series(result_future, "600519", "pe_ttm")
        assert vals.dropna().empty, "未来财报不应被纳入（前视偏差）"
        vals_pb = _series(result_future, "600519", "pb")
        assert vals_pb.dropna().empty

    def test_ann_date_strict_le_trade_date(self, result) -> None:
        # 在 ann_date=2024-02-28 之前（比如 02-15），还看不到 2023Q4 财报，
        # 可用的最近 4 季是 2022Q4(1.1)+2023Q1(1.2)+2023Q2(1.3)+2023Q3(1.4)=5.0
        day = pd.Timestamp("2024-02-15")
        row = result[(result["code"] == "600519") & (result["trade_date"] == day)]
        pe = row["pe_ttm"].iloc[0]
        assert pe == pytest.approx(100.0 / 5.0, rel=1e-6)


# ---------------------------------------------------------------------------
# 4. 分母为 0 → NaN（不是 0）
# ---------------------------------------------------------------------------


class TestDivisionByZero:
    def test_eps_zero_gives_nan(self, bar_df, div_df, fx_df, dim_df, cfg_dict) -> None:
        fin = pd.DataFrame(
            [
                {
                    "code": "600519",
                    "report_period": date(2022, 3, 31),
                    "ann_date": date(2022, 4, 28),
                    "eps": 0.0,
                    "bps": 10.0,
                    "revenue": 10.0,
                    "net_profit": 0.0,
                    "roe": 0.0,
                    "roa": 0.0,
                    "gross_margin": 0.0,
                    "net_margin": 0.0,
                    "debt_ratio": 0.0,
                    "ocf": 0.0,
                    "fcf": 0.0,
                    "total_assets": 100.0,
                    "total_equity": 100.0,
                },
            ]
        )
        from viqtrader.data import valuation as v3

        res = v3.compute(bar_df, fin, div_df, fx_df, dim_df, config=cfg_dict)
        vals = _series(res, "600519", "pe_ttm").dropna()
        # 分母为 0 → pe_ttm 应为 NaN，不应该有有效值（rolling 4 季含 0 求和后 close/总和）
        # 实际上如果 4 季求和也可能是非零（rolling 含后面的非零值）——但这里只有 1 季，
        # rolling min_periods=1，eps_ttm=0。所以 close / 0 = NaN。
        # 但 close / 0 会产生 inf（不是 NaN）。需要验证代码是否把 0 替换成了 NaN。
        # _safe_div 里 denominator.replace(0, np.nan) 就是做这个。
        vals2 = vals[vals.notna()]
        # 只要不含 inf 就算过
        assert not np.isinf(vals2.values).any(), "分母为 0 时应返回 NaN 而非 inf"
        assert vals.isna().all(), f"eps=0 时 pe_ttm 应为 NaN，实际 {vals.unique()}"


# ---------------------------------------------------------------------------
# 5. PE 历史分位：样本足够 [0,1] 区间；不足 NaN
# ---------------------------------------------------------------------------


class TestPePercentile:
    def test_in_range_when_enough_samples(self, result) -> None:
        # 每只标的 750 日线 > 60，pe_percentile 应有有效值且在 [0,1]
        pe_pct = _series(result, "600519", "pe_percentile").dropna()
        assert len(pe_pct) > 100, f"应有足够样本，实际 {len(pe_pct)}"
        assert (pe_pct >= 0).all() and (pe_pct <= 1).all()

    def test_nan_when_too_few_samples(self, bar_df, fin_df, div_df, fx_df, dim_df) -> None:
        # min_samples=800，window=800；日线只有 750 < 800 → pe_percentile 全 NaN
        cfg = {
            "hk_connect": {"dividend_tax_h_share": 0.20},
            "data": {
                "valuation": {
                    "pe_percentile_window_days": 800,
                    "pe_percentile_min_samples": 800,
                }
            },
        }
        from viqtrader.data import valuation as v4

        res = v4.compute(bar_df, fin_df, div_df, fx_df, dim_df, config=cfg)
        vals = _series(res, "600519", "pe_percentile")
        assert vals.isna().all(), f"样本不足时应全 NaN，实际 {vals.notna().sum()} 个有效值"


# ---------------------------------------------------------------------------
# 6. 输出列集与 fact_valuation schema 完全一致（拼写漂移防护）
# ---------------------------------------------------------------------------


def test_output_columns_align_with_schema(result) -> None:
    assert tuple(result.columns) == v.FACT_VALUATION_COLUMNS, (
        f"输出列漂移: 预期 {v.FACT_VALUATION_COLUMNS}，实际 {tuple(result.columns)}"
    )
    assert tuple(result.columns[:2]) == v.FACT_VALUATION_KEY, "前两列应是 (code, trade_date) 主键"


# ---------------------------------------------------------------------------
# 7. 空输入容错
# ---------------------------------------------------------------------------


def test_empty_bar_returns_empty_frame(fin_df, div_df, fx_df, dim_df, cfg_dict) -> None:
    from viqtrader.data import valuation as v5

    res = v5.compute(pd.DataFrame(), fin_df, div_df, fx_df, dim_df, config=cfg_dict)
    assert res.empty
    assert tuple(res.columns) == v5.FACT_VALUATION_COLUMNS
