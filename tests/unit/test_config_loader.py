from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
import yaml

from viqtrader.config.loader import Config, ConfigError

DEFAULT_YAML = """\
platform:
  env: sim
  data_dir: /default/dir
value_track:
  gate:
    pe_ttm_max: 15.0
    nested:
      deep: 1
"""

USER_YAML = """\
platform:
  data_dir: /user/dir
value_track:
  gate:
    pe_ttm_max: 12.0
"""


@pytest.fixture()
def config_dir(tmp_path: Path) -> Path:
    d = tmp_path / "config"
    d.mkdir()
    (d / "default.yaml").write_text(DEFAULT_YAML, encoding="utf-8")
    return d


def test_default_only(config_dir: Path) -> None:
    cfg = Config.load(config_dir, env_path=config_dir / "no.env")
    assert cfg.get("platform.env") == "sim"
    assert cfg.get("value_track.gate.pe_ttm_max") == 15.0
    assert cfg.get("value_track.gate.nested.deep") == 1


def test_user_overrides_default(config_dir: Path) -> None:
    (config_dir / "user.yaml").write_text(USER_YAML, encoding="utf-8")
    cfg = Config.load(config_dir, env_path=config_dir / "no.env")
    # user.yaml 覆盖同名字段
    assert cfg.get("platform.data_dir") == "/user/dir"
    assert cfg.get("value_track.gate.pe_ttm_max") == 12.0
    # 未覆盖字段保留 default 值；深合并不丢兄弟节点
    assert cfg.get("platform.env") == "sim"
    assert cfg.get("value_track.gate.nested.deep") == 1


def test_env_override_runtime_only(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIQ_PLATFORM__DATA_DIR", "/env/dir")
    cfg = Config.load(config_dir, env_path=config_dir / "no.env")
    assert cfg.get("platform.data_dir") == "/env/dir"
    # env 覆盖不进入快照口径
    assert "/env/dir" not in cfg.canonical_yaml()


def test_snapshot_hash_changes_with_user_yaml(config_dir: Path) -> None:
    cfg1 = Config.load(config_dir, env_path=config_dir / "no.env")
    hash1 = cfg1.content_hash()
    (config_dir / "user.yaml").write_text(USER_YAML, encoding="utf-8")
    cfg2 = Config.load(config_dir, env_path=config_dir / "no.env")
    assert cfg2.content_hash() != hash1
    # 同内容同 hash（确定性）
    cfg3 = Config.load(config_dir, env_path=config_dir / "no.env")
    assert cfg3.content_hash() == cfg2.content_hash()


def test_hot_reload(config_dir: Path) -> None:
    cfg = Config.load(config_dir, env_path=config_dir / "no.env")
    assert cfg.get("value_track.gate.pe_ttm_max") == 15.0

    user_file = config_dir / "user.yaml"
    user_file.write_text(USER_YAML, encoding="utf-8")
    future = time.time() + 5
    os.utime(user_file, (future, future))

    assert cfg.reload_if_changed() is True
    assert cfg.get("value_track.gate.pe_ttm_max") == 12.0
    assert cfg.reload_if_changed() is False


def test_require_raises_on_missing(config_dir: Path) -> None:
    cfg = Config.load(config_dir, env_path=config_dir / "no.env")
    with pytest.raises(ConfigError):
        cfg.require("no.such.key")


def test_missing_default_yaml(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        Config.load(tmp_path, env_path=tmp_path / "no.env")


def test_canonical_yaml_roundtrip(config_dir: Path) -> None:
    cfg = Config.load(config_dir, env_path=config_dir / "no.env")
    parsed = yaml.safe_load(cfg.canonical_yaml())
    assert parsed["platform"]["env"] == "sim"
