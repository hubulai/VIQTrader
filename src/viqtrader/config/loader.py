"""三层配置加载：default.yaml < user.yaml < 环境变量（仅机密与路径覆盖）。

- default.yaml 入库版本控制，user.yaml 为本地覆盖（gitignore）
- .env 仅承载机密（token）与少量路径覆盖（VIQ_ 前缀），不参与快照
- 快照 = merge(default, user) 的规范化 YAML 的 sha256
- 热加载：访问时检测文件 mtime 变化自动重载
"""

from __future__ import annotations

import copy
import hashlib
import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

_ENV_PREFIX = "VIQ_"
_LAYER_FILES = ("default.yaml", "user.yaml")


class ConfigError(Exception):
    pass


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """递归合并：override 覆盖 base；dict 递归，其余类型直接替换。"""
    merged = dict(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _apply_env_overrides(cfg: dict[str, Any]) -> dict[str, Any]:
    """VIQ_SECTION__KEY=value 形式的运行时覆盖（仅用于路径类，快照不含此层）。"""
    out = copy.deepcopy(cfg)  # 深拷贝，防止污染快照口径的 merged
    for env_key, env_val in os.environ.items():
        if not env_key.startswith(_ENV_PREFIX):
            continue
        path = env_key[len(_ENV_PREFIX) :].lower().split("__")
        if len(path) < 2:
            continue
        node = out
        for part in path[:-1]:
            nxt = node.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                node[part] = nxt
            node = nxt
        node[path[-1]] = env_val
    return out


def _scan_mtimes(config_dir: Path) -> dict[Path, float]:
    """两层 YAML 的当前 mtime；user.yaml 出现/消失也算变化。"""
    mtimes: dict[Path, float] = {}
    for name in _LAYER_FILES:
        path = config_dir / name
        if path.exists():
            mtimes[path] = path.stat().st_mtime
    return mtimes


def _canonical_dump(cfg: dict[str, Any]) -> str:
    """规范化序列化：键排序、固定格式，保证同内容同 hash。"""
    return yaml.safe_dump(cfg, sort_keys=True, allow_unicode=True, default_flow_style=False)


class Config:
    """只读配置视图 + 热加载 + 快照 hash。引擎与 Agent 统一经此取参。"""

    def __init__(
        self,
        config_dir: Path,
        merged_snapshot: dict[str, Any],
        effective: dict[str, Any],
        mtimes: dict[Path, float],
    ) -> None:
        self._config_dir = config_dir
        self._snapshot_src = merged_snapshot  # default+user 合并结果（快照口径）
        self._effective = effective  # 运行时生效值（含 env 覆盖）
        self._mtimes = mtimes

    @property
    def config_dir(self) -> Path:
        """配置目录（P6 参数编辑页定位 user.yaml 用，只读）。"""
        return self._config_dir

    # ---------- 加载 ----------

    @classmethod
    def load(cls, config_dir: Path, env_path: Path | None = None) -> Config:
        if env_path is None:
            env_path = config_dir.parent / ".env"
        if env_path.exists():
            load_dotenv(env_path)

        default_file = config_dir / "default.yaml"
        user_file = config_dir / "user.yaml"
        if not default_file.exists():
            raise ConfigError(f"缺少 default.yaml: {default_file}")

        default_cfg = yaml.safe_load(default_file.read_text(encoding="utf-8")) or {}
        user_cfg: dict[str, Any] = {}
        if user_file.exists():
            user_cfg = yaml.safe_load(user_file.read_text(encoding="utf-8")) or {}

        merged = _deep_merge(default_cfg, user_cfg)
        effective = _apply_env_overrides(merged)

        return cls(config_dir, merged, effective, _scan_mtimes(config_dir))

    # ---------- 访问 ----------

    def get(self, dotted: str, default: Any = None) -> Any:
        self.reload_if_changed()
        node: Any = self._effective
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def require(self, dotted: str) -> Any:
        sentinel = object()
        value = self.get(dotted, sentinel)
        if value is sentinel:
            raise ConfigError(f"配置缺失: {dotted}")
        return value

    def as_dict(self) -> dict[str, Any]:
        self.reload_if_changed()
        return dict(self._effective)

    # ---------- 快照 ----------

    def canonical_yaml(self) -> str:
        """快照口径：default+user 合并结果（不含 env 覆盖与机密）。"""
        return _canonical_dump(self._snapshot_src)

    def content_hash(self) -> str:
        return hashlib.sha256(self.canonical_yaml().encode("utf-8")).hexdigest()

    # ---------- 热加载 ----------

    def reload_if_changed(self) -> bool:
        if _scan_mtimes(self._config_dir) != self._mtimes:
            fresh = self.load(self._config_dir)
            self._snapshot_src = fresh._snapshot_src
            self._effective = fresh._effective
            self._mtimes = fresh._mtimes
            return True
        return False
