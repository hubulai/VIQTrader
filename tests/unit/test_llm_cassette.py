"""P4.1 盒带测试：录制→离线重放一致、零网络、重放落空 fail-fast、不泄密。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from viqtrader.agents.llm.base import Message
from viqtrader.agents.llm.cassette import CassetteMissError, CassetteProvider, fingerprint
from viqtrader.agents.llm.fake import FakeProvider
from viqtrader.agents.llm.openai_compat import OpenAICompatProvider
from viqtrader.agents.llm.pricing import Pricing
from viqtrader.config.loader import Config


class _BoomProvider:
    """重放模式下若被调用即报错——证明未触达底层（零网络）。"""

    def complete(self, messages: object, model: str, **params: object) -> object:
        raise AssertionError("重放模式不应触达底层 provider")


def test_record_then_replay_identical_and_zero_network(tmp_path: Path) -> None:
    msgs = [Message("system", "s"), Message("user", "u")]
    inner = FakeProvider(content="估值结论", token_in=10, token_out=5)
    recorder = CassetteProvider(
        inner, cassette_dir=tmp_path, agent="valuation", case="maotai", mode="record"
    )
    first = recorder.complete(msgs, "deepseek-chat", temperature=0.1)
    assert recorder.path.exists()

    replayer = CassetteProvider(
        _BoomProvider(), cassette_dir=tmp_path, agent="valuation", case="maotai", mode="replay"
    )
    second = replayer.complete(msgs, "deepseek-chat", temperature=0.1)
    assert second.to_dict() == first.to_dict()
    assert inner.calls and len(inner.calls) == 1  # 底层仅录制时被调一次


def test_replay_missing_file_fails_fast(tmp_path: Path) -> None:
    with pytest.raises(CassetteMissError):
        CassetteProvider(
            FakeProvider(content="x"),
            cassette_dir=tmp_path,
            agent="valuation",
            case="absent",
            mode="replay",
        )


def test_replay_unknown_fingerprint_fails_fast(tmp_path: Path) -> None:
    recorder = CassetteProvider(
        FakeProvider(content="x"), cassette_dir=tmp_path, agent="valuation", case="c", mode="record"
    )
    recorder.complete([Message("user", "u")], "deepseek-chat")

    replayer = CassetteProvider(
        _BoomProvider(), cassette_dir=tmp_path, agent="valuation", case="c", mode="replay"
    )
    with pytest.raises(CassetteMissError):
        replayer.complete([Message("user", "u")], "other-model")


def test_cassette_does_not_store_credentials(tmp_path: Path, config: Config) -> None:
    def fake_post(url: str, body: bytes, headers: dict[str, str], timeout: float) -> bytes:
        return json.dumps(
            {
                "model": "deepseek-chat",
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }
        ).encode("utf-8")

    real = OpenAICompatProvider(
        base_url="https://x/v1",
        api_key="sk-TOPSECRET",
        pricing=Pricing.from_config(config),
        http_post=fake_post,
    )
    provider = CassetteProvider(
        real, cassette_dir=tmp_path, agent="valuation", case="leak", mode="record"
    )
    provider.complete([Message("user", "hello")], "deepseek-chat")

    raw = provider.path.read_text(encoding="utf-8")
    assert "sk-TOPSECRET" not in raw
    assert "Authorization" not in raw


def test_fingerprint_is_order_insensitive_and_ignores_none() -> None:
    msgs = [Message("user", "u")]
    a = fingerprint(msgs, "m", {"temperature": 0.1, "max_tokens": None, "top_p": 0.9})
    b = fingerprint(msgs, "m", {"top_p": 0.9, "temperature": 0.1})
    assert a == b
    assert a != fingerprint(msgs, "m2", {"top_p": 0.9, "temperature": 0.1})
