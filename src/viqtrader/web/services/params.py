"""参数编辑服务（P6，拍板⑤）：user.yaml 全文编辑 + 双校验 + 快照入库。

- 形态：全文文本编辑（不做表单化），保留用户注释；
- 双校验：YAML 语法 → 拷贝 default.yaml 到临时目录与候选 user.yaml 合并，
  Config.load 全链路加载校验通过方可保存；
- 保存：原子写（tmp + replace）；快照由路由层 record_snapshot 入库；
- 影响提示：静态文案映射 IMPACT_HINTS（按变更的顶层节给出中文提示）。
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

import duckdb
import yaml

from viqtrader.config.loader import Config


class ParamsError(Exception):
    """YAML 语法错误 / 顶层非映射 / 配置合并加载失败。"""


@dataclass(frozen=True, slots=True)
class SnapshotRow:
    config_hash: str
    recorded_at: str
    note: str | None
    is_current: bool


IMPACT_HINTS: dict[str, str] = {
    "platform": "数据目录/库文件等基础路径——变更后需重启各进程",
    "data": "数据源与同步参数——影响下一交易日 sync_daily",
    "value_track": "价值轨阈值（能力圈/安全边际/狩猎清单）——影响每周六初筛与每日盯价",
    "swing_track": "波段轨策略与参数——影响每日 swing_signal 初筛",
    "risk": "风控阈值——影响止损扫描与下单规则校验",
    "web": "控制台监听与执行超时——需重启 run_console.py 生效",
    "llm": "模型与超时——影响下一次 Agent 调用",
    "alerting": "告警路由——影响每日 DAG 告警聚合",
    "orchestrator": "编排参数（财报月/复盘账户）——影响每日 DAG",
    "accounts": "账户初始资金——仅执行侧初始化时生效",
}


def read_user_yaml(config: Config) -> str:
    """user.yaml 原文；不存在返回空串（编辑器显示空白）。"""
    path = config.config_dir / "user.yaml"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def validate_user_yaml(config: Config, text: str) -> None:
    """双校验：YAML 语法 → 与 default.yaml 合并后 Config 可完整加载。"""
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ParamsError(f"YAML 语法错误：{exc}") from exc
    if parsed is not None and not isinstance(parsed, dict):
        raise ParamsError("user.yaml 顶层必须是键值映射（mapping）")
    with tempfile.TemporaryDirectory(prefix="viq_params_") as tmp:
        tmp_dir = Path(tmp)
        shutil.copy2(config.config_dir / "default.yaml", tmp_dir / "default.yaml")
        (tmp_dir / "user.yaml").write_text(text, encoding="utf-8")
        try:
            Config.load(tmp_dir)
        except Exception as exc:
            raise ParamsError(f"配置合并校验失败：{exc}") from exc


def changed_top_sections(old_text: str, new_text: str) -> list[str]:
    """新旧 user.yaml 的顶层节差异（影响提示依据）。

    任一侧解析失败按空映射处理（保守：另一侧全部节视为变更）。
    """

    def _tops(text: str) -> dict[str, object]:
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError:
            return {}
        return data if isinstance(data, dict) else {}

    old, new = _tops(old_text), _tops(new_text)
    return sorted(k for k in set(old) | set(new) if old.get(k) != new.get(k))


def save_user_yaml(config: Config, text: str) -> list[str]:
    """校验 → 原子写入 user.yaml。返回变更的顶层节（供影响提示）。"""
    validate_user_yaml(config, text)
    changed = changed_top_sections(read_user_yaml(config), text)
    path = config.config_dir / "user.yaml"
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return changed


def list_snapshots(
    conn: duckdb.DuckDBPyConnection, config: Config, limit: int = 20
) -> list[SnapshotRow]:
    """最近配置快照（新→旧），标记当前生效 hash。"""
    current = config.content_hash()
    rows = conn.execute(
        "SELECT config_hash, CAST(recorded_at AS TEXT), note "
        "FROM config_snapshot ORDER BY recorded_at DESC LIMIT ?",
        [limit],
    ).fetchall()
    return [
        SnapshotRow(
            str(r[0]), str(r[1]), str(r[2]) if r[2] is not None else None, str(r[0]) == current
        )
        for r in rows
    ]
