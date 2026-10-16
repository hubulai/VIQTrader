"""Prompt 版本化管理（P4.3）：prompt 即代码，git 版本化 + manifest 映射。

- 文本存 `texts/<name>.<version>.md`；`manifest.yaml` 映射 `name → {version, path}`。
- `render(name, **vars)` 用 `string.Template` 占位（`$var`）——避开 JSON 花括号冲突。
- `version(name)` 返回 `<name>.<version>`，注入 `agent_run.prompt_version` 供回溯。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import Any, cast

import yaml

PACKAGE_DIR = Path(__file__).resolve().parent
TEXTS_DIR = PACKAGE_DIR / "texts"
MANIFEST_FILE = PACKAGE_DIR / "manifest.yaml"


class PromptError(Exception):
    """prompt 缺失 / manifest 非法。"""


@dataclass(frozen=True, slots=True)
class PromptMeta:
    name: str
    version: str
    path: Path

    @property
    def version_id(self) -> str:
        return f"{self.name}.{self.version}"


class PromptRegistry:
    """加载 manifest 并取用版本化 prompt 文本。"""

    def __init__(
        self,
        manifest_file: Path | None = None,
        texts_dir: Path | None = None,
    ) -> None:
        self._manifest_file = manifest_file or MANIFEST_FILE
        self._texts_dir = texts_dir or TEXTS_DIR
        self._meta = self._load_manifest()

    def _load_manifest(self) -> dict[str, PromptMeta]:
        raw = cast(
            "dict[str, Any]",
            yaml.safe_load(self._manifest_file.read_text(encoding="utf-8")),
        )
        prompts = raw.get("prompts", {}) if isinstance(raw, dict) else {}
        if not isinstance(prompts, dict):
            raise PromptError(f"manifest 非法（prompts 必须是映射）: {self._manifest_file}")
        meta: dict[str, PromptMeta] = {}
        for name, spec in prompts.items():
            if not isinstance(spec, dict) or "version" not in spec or "path" not in spec:
                raise PromptError(f"manifest 条目非法（需 version + path）: {name}")
            meta[str(name)] = PromptMeta(
                str(name), str(spec["version"]), self._texts_dir / str(spec["path"])
            )
        return meta

    def names(self) -> list[str]:
        return sorted(self._meta)

    def meta(self, name: str) -> PromptMeta:
        try:
            return self._meta[name]
        except KeyError:
            raise PromptError(f"未登记的 prompt: {name}（已登记 {self.names()}）") from None

    def version(self, name: str) -> str:
        return self.meta(name).version_id

    def text(self, name: str) -> str:
        meta = self.meta(name)
        if not meta.path.exists():
            raise PromptError(f"prompt 文本缺失: {meta.path}")
        return meta.path.read_text(encoding="utf-8")

    def render(self, prompt_name: str, **variables: Any) -> str:
        """渲染 prompt；variables 对应文本内的 $占位符（含 $name 等业务字段）。"""
        return Template(self.text(prompt_name)).substitute(**variables)


__all__ = ["PromptError", "PromptMeta", "PromptRegistry"]
