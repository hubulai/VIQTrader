"""契约测试共享 fixture。

默认对被测 FakeAdapter 跑全套契约。后续真实适配器（QMT/AkShare）在自己的
测试目录下提供同名 fixture 覆盖 adapter / sample_code / sample_range，
并复用 test_adapter_contract.py 中的同一套契约用例。
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import pytest

from tests.contract.fake_adapter import SAMPLE_CODE_A, FakeAdapter

if TYPE_CHECKING:
    from viqtrader.data.base import DataAdapter


@pytest.fixture
def adapter() -> DataAdapter:
    return FakeAdapter()


@pytest.fixture
def sample_code() -> str:
    return SAMPLE_CODE_A


@pytest.fixture
def sample_range() -> tuple[date, date]:
    return date(2024, 1, 1), date(2024, 1, 31)
