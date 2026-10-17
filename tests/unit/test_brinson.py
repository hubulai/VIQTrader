"""P4.6 Brinson 归因定量层单测（手工核对三效应 + 恒等式 + 现金段/新建仓口径）。"""

from datetime import date

import pytest

from viqtrader.attribution.brinson import (
    CASH_SEGMENT,
    NewPositionItem,
    SegmentInput,
    compute_brinson,
)

START = date(2026, 1, 1)
END = date(2026, 1, 31)


def _food() -> SegmentInput:
    return SegmentInput("食品饮料", 0.5, 0.10, 0.4, 0.08)


def _pharma() -> SegmentInput:
    return SegmentInput("医药生物", 0.3, 0.04, 0.6, 0.02)


class TestThreeEffectsHandChecked:
    """手工核对：食品/医药/现金三段（rb=0.044、rp=0.062、active=0.018）。"""

    def test_segment_effects(self) -> None:
        result = compute_brinson(
            account_id="acc_value",
            start=START,
            end=END,
            segments=[_food(), _pharma()],
            cash_weight=0.2,
        )
        assert result is not None
        assert result.portfolio_return == pytest.approx(0.062)
        assert result.benchmark_return == pytest.approx(0.044)
        assert result.active_return == pytest.approx(0.018)

        seg = {s.segment: s for s in result.segments}
        food = seg["食品饮料"]
        # allocation=(0.5-0.4)*(0.08-0.044)；selection=0.4*(0.10-0.08)；interaction=0.1*0.02
        assert food.allocation == pytest.approx(0.0036)
        assert food.selection == pytest.approx(0.008)
        assert food.interaction == pytest.approx(0.002)
        assert food.total == pytest.approx(0.0136)

        pharma = seg["医药生物"]
        # allocation=(-0.3)*(-0.024)；selection=0.6*0.02；interaction=(-0.3)*0.02
        assert pharma.allocation == pytest.approx(0.0072)
        assert pharma.selection == pytest.approx(0.012)
        assert pharma.interaction == pytest.approx(-0.006)
        assert pharma.total == pytest.approx(0.0132)

        cash = seg[CASH_SEGMENT]
        assert cash.weight_portfolio == pytest.approx(0.2)
        assert cash.return_portfolio == 0.0
        assert cash.weight_benchmark == 0.0
        assert cash.return_benchmark == pytest.approx(0.044)  # 约定 = rb
        assert cash.allocation == pytest.approx(0.0)  # rb_cash = rb 恒 0
        assert cash.selection == pytest.approx(0.0)
        assert cash.interaction == pytest.approx(-0.0088)  # 现金拖累 = -wp*rb

    def test_totals(self) -> None:
        result = compute_brinson(
            account_id="acc_value",
            start=START,
            end=END,
            segments=[_food(), _pharma()],
            cash_weight=0.2,
        )
        assert result is not None
        assert result.total_allocation == pytest.approx(0.0108)
        assert result.total_selection == pytest.approx(0.02)
        assert result.total_interaction == pytest.approx(-0.0128)


class TestIdentity:
    """恒等式：Σ三效应 = rp - rb（多组输入逐一校验）。"""

    @pytest.mark.parametrize(
        ("segments", "cash_weight"),
        [
            ([_food(), _pharma()], 0.2),  # 两段 + 现金
            ([SegmentInput("全市场", 1.0, 0.05, 1.0, 0.03)], 0.0),  # 单段满仓
            (
                [
                    SegmentInput("银行", 0.0, 0.01, 0.3, 0.01),  # 基准独有段（rp 填 rb_i）
                    SegmentInput("食品饮料", 0.7, 0.12, 0.5, 0.06),
                    SegmentInput("医药生物", 0.2, -0.02, 0.2, 0.04),
                ],
                0.1,
            ),
        ],
    )
    def test_sum_effects_equals_active_return(
        self, segments: list[SegmentInput], cash_weight: float
    ) -> None:
        result = compute_brinson(
            account_id="acc_swing",
            start=START,
            end=END,
            segments=segments,
            cash_weight=cash_weight,
        )
        assert result is not None
        total = result.total_allocation + result.total_selection + result.total_interaction
        assert total == pytest.approx(result.active_return, abs=1e-12)

    def test_benchmark_only_segment_zero_selection(self) -> None:
        """基准独有段（wp=0，装配层按约定 rp 填 rb_i）：selection/interaction 为 0。"""
        result = compute_brinson(
            account_id="acc_value",
            start=START,
            end=END,
            segments=[
                SegmentInput("银行", 0.0, 0.01, 0.5, 0.01),
                SegmentInput("食品饮料", 1.0, 0.06, 0.5, 0.06),
            ],
        )
        assert result is not None
        bank = next(s for s in result.segments if s.segment == "银行")
        assert bank.selection == 0.0
        assert bank.interaction == 0.0
        # allocation = (0-0.5)*(0.01 - rb)，rb = 0.5*0.01+0.5*0.06 = 0.035
        assert bank.allocation == pytest.approx(-0.5 * (0.01 - 0.035))

    def test_single_segment_pure_selection(self) -> None:
        """单段满仓（wp=wb=1）：配置/交互为 0，超额全部来自选择效应。"""
        result = compute_brinson(
            account_id="acc_value",
            start=START,
            end=END,
            segments=[SegmentInput("全市场", 1.0, 0.05, 1.0, 0.03)],
        )
        assert result is not None
        assert result.total_allocation == pytest.approx(0.0)
        assert result.total_interaction == pytest.approx(0.0)
        assert result.total_selection == pytest.approx(0.02)
        assert result.active_return == pytest.approx(0.02)


class TestCashSegment:
    def test_no_cash_segment_when_weight_zero(self) -> None:
        """cash_weight=0 时不注入现金段。"""
        result = compute_brinson(
            account_id="acc_value",
            start=START,
            end=END,
            segments=[SegmentInput("全市场", 1.0, 0.05, 1.0, 0.03)],
            cash_weight=0.0,
        )
        assert result is not None
        assert CASH_SEGMENT not in {s.segment for s in result.segments}

    def test_cash_segment_sorted_last(self) -> None:
        """行业段按名称排序，现金段固定排最后（输出确定性）。"""
        result = compute_brinson(
            account_id="acc_value",
            start=START,
            end=END,
            segments=[_food(), _pharma()],  # 输入顺序：食品在前
            cash_weight=0.2,
        )
        assert result is not None
        names = [s.segment for s in result.segments]
        assert names[-1] == CASH_SEGMENT
        assert names[:-1] == sorted(names[:-1])


class TestNewPositionsAndIndex:
    def test_new_positions_carried_through(self) -> None:
        """区间新建仓不参与分解，单列透传。"""
        new_positions = [NewPositionItem("600519", "2026-01-15")]
        result = compute_brinson(
            account_id="acc_value",
            start=START,
            end=END,
            segments=[_food(), _pharma()],
            cash_weight=0.2,
            new_positions=new_positions,
        )
        assert result is not None
        assert result.new_positions == tuple(new_positions)

    def test_index_excess_reference(self) -> None:
        """指数口径超额 = rp - index_return（参考字段，不参与分解）。"""
        result = compute_brinson(
            account_id="acc_value",
            start=START,
            end=END,
            segments=[_food(), _pharma()],
            cash_weight=0.2,
            index_return=0.03,
        )
        assert result is not None
        assert result.index_return == 0.03
        assert result.index_excess == pytest.approx(0.062 - 0.03)

    def test_index_excess_none_without_index(self) -> None:
        result = compute_brinson(
            account_id="acc_value",
            start=START,
            end=END,
            segments=[_food(), _pharma()],
            cash_weight=0.2,
        )
        assert result is not None
        assert result.index_return is None
        assert result.index_excess is None


class TestValidation:
    def test_empty_segments_returns_none(self) -> None:
        """无行业段（纯现金组合）无可归因对象，返回 None 区别于归因值 0。"""
        assert (
            compute_brinson(
                account_id="acc_value", start=START, end=END, segments=[], cash_weight=1.0
            )
            is None
        )

    def test_portfolio_weight_sum_must_be_one(self) -> None:
        with pytest.raises(ValueError, match="组合权重和"):
            compute_brinson(
                account_id="acc_value",
                start=START,
                end=END,
                segments=[_food(), _pharma()],
                cash_weight=0.0,  # Σwp=0.8 ≠ 1
            )

    def test_benchmark_weight_sum_must_be_one(self) -> None:
        with pytest.raises(ValueError, match="基准权重和"):
            compute_brinson(
                account_id="acc_value",
                start=START,
                end=END,
                segments=[
                    SegmentInput("食品饮料", 0.6, 0.10, 0.4, 0.08),
                    SegmentInput("医药生物", 0.4, 0.04, 0.4, 0.02),  # Σwb=0.8 ≠ 1
                ],
            )

    def test_negative_weight_rejected(self) -> None:
        with pytest.raises(ValueError, match="权重非法"):
            compute_brinson(
                account_id="acc_value",
                start=START,
                end=END,
                segments=[SegmentInput("食品饮料", -0.1, 0.10, 1.1, 0.08)],
                cash_weight=1.1,
            )

    def test_negative_cash_rejected(self) -> None:
        with pytest.raises(ValueError, match="现金权重非法"):
            compute_brinson(
                account_id="acc_value",
                start=START,
                end=END,
                segments=[SegmentInput("全市场", 1.1, 0.05, 1.0, 0.03)],
                cash_weight=-0.1,
            )

    def test_invalid_period_rejected(self) -> None:
        with pytest.raises(ValueError, match="归因区间非法"):
            compute_brinson(
                account_id="acc_value",
                start=END,
                end=START,
                segments=[SegmentInput("全市场", 1.0, 0.05, 1.0, 0.03)],
            )
