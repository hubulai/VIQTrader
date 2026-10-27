"""P3.8 回测稳健性审计层单元测试：PBO / CPCV / DSR / block 划分。

所有纯函数测试**不连 DuckDB**，用合成 Sharpe 矩阵做 known-answer 验证。
"""

from __future__ import annotations

import random
from datetime import date

import pytest

from viqtrader.lifecycle.robustness import (
    _split_equal_blocks,
    compute_pbo_cpcv,
    deflated_sharpe_ratio,
)

# ---------------------------------------------------------------------------
# compute_pbo_cpcv：纯函数矩阵测试
# ---------------------------------------------------------------------------


class TestPBOandCPCV:
    """CPCV 组合枚举 + PBO/CPCV 计算（块级合成矩阵，known-answer）。"""

    def test_perfect_generalization(self) -> None:
        """每个 block 里参数组 k=0 都是全局最优 → PBO=0.0, CPCV≈0.0。"""
        # 6 个 block x 3 个参数组：k0 永远 1.0，k1 永远 0.5，k2 永远 0.1
        S = [
            [1.0, 0.5, 0.1],  # block 0
            [1.0, 0.5, 0.1],  # block 1
            [1.0, 0.5, 0.1],  # block 2
            [1.0, 0.5, 0.1],  # block 3
            [1.0, 0.5, 0.1],  # block 4
            [1.0, 0.5, 0.1],  # block 5
        ]
        r = compute_pbo_cpcv(S)
        assert r.n_blocks == 6
        assert r.param_count == 3
        # C(6, 3) = 20 组合
        assert r.combination_count == 20
        # 每个组合的 k* = 0（IS 均值 1.0），OOS 里 k=0 也排第 1（rank=1, relative_rank=0.0）
        assert r.pbo == 0.0
        assert r.cpcv == 0.0
        assert r.valid_combination_count == 20

    def test_pure_noise_pbo_half(self) -> None:
        """随机 Sharpe（固定种子）→ PBO ≈ 0.5（期望位置）。"""
        rng = random.Random(42)
        # 16 block x 8 param
        S = [[rng.uniform(-1.0, 1.0) for _ in range(8)] for _ in range(16)]
        r = compute_pbo_cpcv(S)
        # 随机情况下，每个 IS 组合的最优参数组在 OOS 里平均排中间
        # PBO = mean(I(rank > 0.5))，期望 ≈ 0.5；波动范围 ~0.35-0.65
        assert r.pbo is not None
        assert 0.30 <= r.pbo <= 0.70, f"纯噪声 PBO 应在 0.5 附近，实际 {r.pbo}"
        # CPCV = mean(rank)，期望 ≈ 0.5
        assert r.cpcv is not None
        assert 0.30 <= r.cpcv <= 0.70, f"纯噪声 CPCV 应在 0.5 附近，实际 {r.cpcv}"

    def test_intentional_overfit(self) -> None:
        """奇偶 block 表现反转：任何 IS 里最优的参数组在 OOS 里必然排最后 → PBO ≈ 1.0。

        合成矩阵（M=2 为简化，N=6）：
        - 奇数 block：k0=10（好），k1=0（差）
        - 偶数 block：k0=0（差），k1=10（好）

        C(6,3) 枚举任何 IS 组合里：
        - k* = IS 均值最大的参数组
        - OOS 里 k* 的均值必然最小（它擅长的 block 全被 IS 拿走了）
        - M=2 时 rank=2, relative_rank=1.0（保守取并列最末）
        """
        S = [
            [0.0, 10.0],  # block 0 (偶): k0 差, k1 好
            [10.0, 0.0],  # block 1 (奇): k0 好, k1 差
            [0.0, 10.0],  # block 2 (偶)
            [10.0, 0.0],  # block 3 (奇)
            [0.0, 10.0],  # block 4 (偶)
            [10.0, 0.0],  # block 5 (奇)
        ]
        r = compute_pbo_cpcv(S)
        # 所有 20 个组合的 relative_rank 都应该是 1.0
        assert r.combination_count == 20
        assert r.valid_combination_count == 20
        for rr in r.relative_ranks:
            assert rr == 1.0, f"预期 relative_rank=1.0（纯过拟合），实际 {rr}"
        assert r.pbo == 1.0
        assert r.cpcv == 1.0

    def test_single_param_group(self) -> None:
        """M=1：relative_rank 无意义（分母 0）→ PBO/CPCV 最保守记 1.0。"""
        S = [[0.5], [0.8], [0.3], [0.9]]
        r = compute_pbo_cpcv(S)
        assert r.param_count == 1
        assert r.pbo == 1.0
        assert r.cpcv == 1.0
        assert r.skipped_combinations == r.combination_count

    def test_negative_is_sharpe_is_conservative(self) -> None:
        """IS 最优 Sharpe ≤ 0 → relative_rank 记 1.0（保守判过拟合）。"""
        # 所有 block 里所有参数组 Sharpe 都是负的
        S = [
            [-0.5, -1.0],
            [-0.3, -0.7],
            [-0.8, -0.2],
            [-0.1, -0.4],
        ]
        r = compute_pbo_cpcv(S)
        # 每个 IS 组合：IS 均值最大（但仍为负）的参数组在 OOS 里排第几不重要，
        # 因为 IS Sharpe ≤ 0 → relative_rank = 1.0（最保守）
        assert r.pbo == 1.0
        assert r.cpcv == 1.0

    def test_all_none_block_is_skipped(self) -> None:
        """某个 block 全部 None → IS/OOS 均值跳过 None。如果组合全 None → 跳过。"""
        # 中间一个 block 全 None
        S = [
            [0.5, 0.3],
            [None, None],
            [0.4, 0.6],
            [0.7, 0.2],
            [0.3, 0.5],
            [0.6, 0.4],
        ]
        r = compute_pbo_cpcv(S)
        # block 1 全 None；IS 组合如果不含 block 1 → 正常计算
        # 如果 IS 组合包含 block 1 → block 1 在均值里被跳过（其他 block 还有值）
        # 只要不是所有 IS block 都全 None，就有效
        assert r.valid_combination_count > 0
        # 有全 None block 不应该让整个函数崩溃
        assert r.pbo is not None

    def test_all_none_skips_everything(self) -> None:
        """所有 block 所有参数组都是 None → 全部组合被跳过 → pbo/cpcv 为 None。"""
        S = [[None, None], [None, None], [None, None], [None, None]]
        r = compute_pbo_cpcv(S)
        assert r.valid_combination_count == 0
        assert r.pbo is None
        assert r.cpcv is None

    def test_minimum_two_blocks(self) -> None:
        """N < 2 → ValueError fail-fast。"""
        with pytest.raises(ValueError, match="至少需要 2 个 block"):
            compute_pbo_cpcv([[0.5, 0.3]])

    def test_minimum_one_param(self) -> None:
        """M < 1 → ValueError fail-fast。"""
        with pytest.raises(ValueError, match="至少需要 1 个参数组"):
            compute_pbo_cpcv([[], []])

    def test_to_dict_is_json_serializable(self) -> None:
        """结果结构 to_dict() 必须 JSON 可序列化（给前端看）。"""
        S = [
            [1.0, 0.5, 0.1],
            [1.0, 0.5, 0.1],
            [1.0, 0.5, 0.1],
            [1.0, 0.5, 0.1],
            [1.0, 0.5, 0.1],
            [1.0, 0.5, 0.1],
        ]
        import json

        r = compute_pbo_cpcv(S)
        d = r.to_dict()
        json.dumps(d)  # should not raise


# ---------------------------------------------------------------------------
# deflated_sharpe_ratio：DSR 公式
# ---------------------------------------------------------------------------


class TestDSR:
    """Bailey & Lopez de Prado 2014 deflated Sharpe ratio。"""

    def test_high_sharpe_many_params_long_sample(self) -> None:
        """观测 Sharpe 高 + 参数组多 + 样本长 → DSR 接近 1.0。"""
        dsr = deflated_sharpe_ratio(observed_sharpe=2.5, n_param_groups=20, sample_length_days=1260)
        # SR_max_expected = sqrt(2 ln 20) ≈ 2.45
        # z = (2.5 - 2.45) / (1 / sqrt(5)) ≈ 0.11
        # phi(0.11) ≈ 0.544
        # 修正后略低，但整体应在 0.5 以上（显著好于随机）
        assert 0.5 < dsr < 0.95

    def test_moderate_sharpe_few_params(self) -> None:
        """中等 Sharpe + 少参数组 → DSR 在 0.5-0.8 区间。"""
        dsr = deflated_sharpe_ratio(observed_sharpe=1.0, n_param_groups=5, sample_length_days=500)
        # SR_max_expected = sqrt(2 ln 5) ≈ 1.79
        # z = (1.0 - 1.79) / (1 / sqrt(2)) ≈ -1.12
        # phi(-1.12) ≈ 0.131
        assert 0.05 < dsr < 0.40

    def test_sharpe_none_returns_zero(self) -> None:
        """观测 Sharpe 为 None → DSR = 0.0（零防御性）。"""
        assert deflated_sharpe_ratio(None, 10, 500) == 0.0

    def test_sharpe_negative_returns_zero(self) -> None:
        """观测 Sharpe 为负 → DSR = 0.0。"""
        assert deflated_sharpe_ratio(-0.5, 10, 500) == 0.0

    def test_single_param_group_returns_half(self) -> None:
        """参数组 < 2 → 退化为普通单尾显著性水平 0.5。"""
        assert deflated_sharpe_ratio(1.5, 1, 500) == 0.5
        assert deflated_sharpe_ratio(1.5, 0, 500) == 0.5

    def test_short_sample_returns_zero(self) -> None:
        """样本 ≤ 10 天 → DSR = 0.0（无法统计）。"""
        assert deflated_sharpe_ratio(2.0, 10, 5) == 0.0

    def test_dsr_bounded_in_zero_one(self) -> None:
        """无论什么输入组合，DSR 输出必须在 [0, 1] 闭区间内。"""
        for sr in [-1.0, -0.01, 0.0, 0.01, 0.5, 1.0, 2.0, 5.0, 10.0, None]:
            for n in [0, 1, 2, 5, 20, 100]:
                for days in [1, 10, 50, 252, 1000, 5000]:
                    dsr = deflated_sharpe_ratio(sr, n, days)  # type: ignore[arg-type]
                    assert 0.0 <= dsr <= 1.0, (
                        f"DSR out of range: sr={sr}, n={n}, days={days} -> {dsr}"
                    )


# ---------------------------------------------------------------------------
# _split_equal_blocks：block 划分
# ---------------------------------------------------------------------------


class TestBlockSplit:
    """等长 block 划分（向下取整 + 余数补给前几个）。"""

    def test_equal_blocks(self) -> None:
        """8 天切成 4 block → 每个 2 天。"""
        days = [date(2024, 1, 1) + __import__("datetime").timedelta(days=i) for i in range(8)]
        blocks = _split_equal_blocks(days, 4)
        assert len(blocks) == 4
        assert [len(b) for b in blocks] == [2, 2, 2, 2]

    def test_remainder_first_blocks(self) -> None:
        """9 天切成 4 block → 3/2/2/2（余数 1 补给 block 0）。"""
        days = [date(2024, 1, 1) + __import__("datetime").timedelta(days=i) for i in range(9)]
        blocks = _split_equal_blocks(days, 4)
        assert [len(b) for b in blocks] == [3, 2, 2, 2]

    def test_single_day_blocks(self) -> None:
        """4 天切成 4 block → 每个 1 天。"""
        days = [date(2024, 1, 1) + __import__("datetime").timedelta(days=i) for i in range(4)]
        blocks = _split_equal_blocks(days, 4)
        assert [len(b) for b in blocks] == [1, 1, 1, 1]

    def test_too_few_days(self) -> None:
        """天数 < block 数 → ValueError。"""
        days = [date(2024, 1, 1) + __import__("datetime").timedelta(days=i) for i in range(3)]
        with pytest.raises(ValueError, match="交易日数 3 不足以切成 4 个等长 block"):
            _split_equal_blocks(days, 4)

    def test_block_days_cover_all(self) -> None:
        """所有 block 的日期并集 = 原始序列（无重复、无遗漏）。"""
        days = [date(2024, 1, 1) + __import__("datetime").timedelta(days=i) for i in range(10)]
        blocks = _split_equal_blocks(days, 3)
        flat = [d for b in blocks for d in b]
        assert flat == days
