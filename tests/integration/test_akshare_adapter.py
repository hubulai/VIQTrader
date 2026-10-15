"""AkShare 适配器联网集成测试（pytest -m integration 才运行）。

对真实 AkShareAdapter 调用 tests/contract 的契约检查入口，验证真实源数据符合契约。
网络/接口不可用时跳过并如实标注（不伪造通过）；契约违规仍会失败。
"""

from __future__ import annotations

from datetime import date

import pytest

from viqtrader.data import contract
from viqtrader.data.adapters.akshare import AkShareAdapter
from viqtrader.data.base import Capability, DataAdapter, DataSourceError

pytestmark = pytest.mark.integration

_HK_CODE = "00700"  # 腾讯控股（港股通）
_FX_PAIR = "HKD/CNY"
_FX_START = date(2024, 1, 2)
_FX_END = date(2024, 1, 31)

# 已知本机网络不可达的接口（如东方财富 push2 行情子域），跳过而非失败。
_NETWORK_ERROR_NAMES = {
    "ConnectionError",
    "RemoteDisconnected",
    "ProtocolError",
    "Timeout",
    "ReadTimeout",
    "ConnectTimeout",
    "SSLError",
    "JSONDecodeError",
}


def _is_network_error(exc: BaseException) -> bool:
    seen: set[int] = set()
    node: BaseException | None = exc
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        if type(node).__name__ in _NETWORK_ERROR_NAMES:
            return True
        node = node.__cause__ or node.__context__
    return False


def _guard(call: object) -> None:
    """执行契约检查：契约违规必失败；纯网络/接口不可用则跳过并标注原因。"""
    try:
        call()  # type: ignore[operator]
    except DataSourceError as exc:
        if _is_network_error(exc):
            pytest.skip(f"数据源接口当前不可达（已知不可用）: {exc}")
        raise


@pytest.fixture
def adapter() -> DataAdapter:
    return AkShareAdapter(retries=3, retry_wait=2.0)


def test_protocol_conformance(adapter: DataAdapter) -> None:
    assert isinstance(adapter, DataAdapter)


def test_capabilities_declared(adapter: DataAdapter) -> None:
    assert adapter.capabilities() == frozenset(
        {Capability.STOCK_LIST, Capability.FINANCIAL, Capability.DIVIDEND, Capability.FX_RATE}
    )


def test_financial(adapter: DataAdapter) -> None:
    _guard(lambda: contract.check_financial(adapter, _HK_CODE))


def test_dividend(adapter: DataAdapter) -> None:
    _guard(lambda: contract.check_dividend(adapter, _HK_CODE))


def test_fx_rate(adapter: DataAdapter) -> None:
    _guard(lambda: contract.check_fx_rate(adapter, _FX_PAIR, _FX_START, _FX_END))


def test_stock_list(adapter: DataAdapter) -> None:
    # 港股通名单接口（push2 行情子域）在本机不可达时跳过，非契约违规。
    _guard(lambda: contract.check_stock_list(adapter))


def test_empty_result(adapter: DataAdapter) -> None:
    _guard(lambda: contract.check_empty_result(adapter))


def test_unsupported_raises(adapter: DataAdapter) -> None:
    _guard(lambda: contract.check_unsupported_raises(adapter))


def test_financial_has_real_rows(adapter: DataAdapter) -> None:
    try:
        df = adapter.fetch_financial(_HK_CODE)
    except DataSourceError as exc:
        if _is_network_error(exc):
            pytest.skip(f"港股财务接口当前不可达: {exc}")
        raise
    assert not df.empty
    assert df["code"].unique().tolist() == [_HK_CODE]
    assert df["report_period"].notna().all()
