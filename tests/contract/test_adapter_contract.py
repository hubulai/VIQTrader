"""DataAdapter 契约测试套件：任何适配器实现必须通过同一套用例。

fixture（adapter / sample_code / sample_range）由被测目录的 conftest 提供，
默认实现为 tests/contract/fake_adapter.py 的 FakeAdapter。
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from tests.contract.fake_adapter import SAMPLE_CODE_HK, FakeAdapter
from viqtrader.data import contract
from viqtrader.data.base import Capability, DataAdapter
from viqtrader.data.contract import ContractViolation


def test_protocol_conformance(adapter: DataAdapter) -> None:
    assert isinstance(adapter, DataAdapter)


def test_capabilities_declared(adapter: DataAdapter) -> None:
    caps = adapter.capabilities()
    assert isinstance(caps, frozenset)
    assert caps, "能力集合不能为空"
    assert set(caps) <= set(Capability)


def test_stock_list(adapter: DataAdapter) -> None:
    if Capability.STOCK_LIST not in adapter.capabilities():
        pytest.skip("能力未声明")
    contract.check_stock_list(adapter)


def test_daily_bar(adapter: DataAdapter, sample_code: str, sample_range: tuple[date, date]) -> None:
    if Capability.DAILY_BAR not in adapter.capabilities():
        pytest.skip("能力未声明")
    contract.check_daily_bar(adapter, sample_code, *sample_range)


def test_daily_bar_hk(adapter: DataAdapter, sample_range: tuple[date, date]) -> None:
    """港股通标的行情同样受契约约束（双市场覆盖）。"""
    if Capability.DAILY_BAR not in adapter.capabilities():
        pytest.skip("能力未声明")
    contract.check_daily_bar(adapter, SAMPLE_CODE_HK, *sample_range)


def test_financial(adapter: DataAdapter, sample_code: str) -> None:
    if Capability.FINANCIAL not in adapter.capabilities():
        pytest.skip("能力未声明")
    contract.check_financial(adapter, sample_code)


def test_dividend(adapter: DataAdapter, sample_code: str) -> None:
    if Capability.DIVIDEND not in adapter.capabilities():
        pytest.skip("能力未声明")
    contract.check_dividend(adapter, sample_code)


def test_disclosure(
    adapter: DataAdapter, sample_code: str, sample_range: tuple[date, date]
) -> None:
    if Capability.DISCLOSURE not in adapter.capabilities():
        pytest.skip("能力未声明")
    # fake 的公告披露日为 2024-04-03，扩大窗口覆盖
    start, _ = sample_range
    contract.check_disclosure(adapter, sample_code, start, date(2024, 12, 31))


def test_fx_rate(adapter: DataAdapter, sample_range: tuple[date, date]) -> None:
    if Capability.FX_RATE not in adapter.capabilities():
        pytest.skip("能力未声明")
    contract.check_fx_rate(adapter, "HKD/CNY", *sample_range)


def test_hk_connect_history(adapter: DataAdapter) -> None:
    if Capability.HK_CONNECT_HISTORY not in adapter.capabilities():
        pytest.skip("能力未声明")
    contract.check_hk_connect_history(adapter)


def test_industry_map(adapter: DataAdapter) -> None:
    if Capability.INDUSTRY_MAP not in adapter.capabilities():
        pytest.skip("能力未声明")
    contract.check_industry_map(adapter)


def test_index_bar(adapter: DataAdapter, sample_range: tuple[date, date]) -> None:
    if Capability.INDEX_BAR not in adapter.capabilities():
        pytest.skip("能力未声明")
    contract.check_index_bar(adapter, "000300", *sample_range)


def test_share_structure(adapter: DataAdapter) -> None:
    if Capability.SHARE_STRUCTURE not in adapter.capabilities():
        pytest.skip("能力未声明")
    contract.check_share_structure(adapter)


def test_empty_result(adapter: DataAdapter) -> None:
    contract.check_empty_result(adapter)


def test_unsupported_raises(adapter: DataAdapter) -> None:
    contract.check_unsupported_raises(adapter)


# ---------- 套件自检：坏实现必须被抓出来 ----------


def test_suite_detects_broken_schema() -> None:
    """缺列的坏实现必须触发 ContractViolation（防止套件形同虚设）。"""

    class BrokenAdapter(FakeAdapter):
        def fetch_daily_bar(self, code: str, start: date, end: date) -> pd.DataFrame:
            df = super().fetch_daily_bar(code, start, end)
            return df.drop(columns=["adj_factor"])

    with pytest.raises(ContractViolation, match="缺少契约列"):
        contract.check_daily_bar(BrokenAdapter(), "600519", date(2024, 1, 1), date(2024, 1, 31))


def test_suite_detects_bad_unsupported() -> None:
    """未声明能力却不抛异常的坏实现必须被抓出。"""

    class BadAdapter(FakeAdapter):
        def capabilities(self) -> frozenset[Capability]:
            return frozenset(Capability) - {Capability.DIVIDEND}

    with pytest.raises(ContractViolation, match="UnsupportedCapabilityError"):
        contract.check_unsupported_raises(BadAdapter())
