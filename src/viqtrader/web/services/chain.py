"""链归属通用函数（P6 重构）：account.mode → account_id 映射。

Advisor 红线：**用 account.mode 过滤，绝不要硬编码 account_id 字符串**
（用户可能在 account 表改名，mode 列是设计用来标识价值链/波段链的）。
"""

from __future__ import annotations

import duckdb


def account_id_by_mode(conn: duckdb.DuckDBPyConnection, mode: str) -> str | None:
    """按 mode ('value' / 'swing') 查 account_id。

    Returns:
        account_id 或 None（账本未初始化）。
    """
    row = conn.execute("SELECT account_id FROM account WHERE mode = ? LIMIT 1", [mode]).fetchone()
    return str(row[0]) if row else None


def ensure_account(conn: duckdb.DuckDBPyConnection, mode: str) -> str:
    """断言链账本存在，否则 raise。"""
    acc = account_id_by_mode(conn, mode)
    if acc is None:
        raise RuntimeError(f"账本未初始化：account.mode = '{mode}' 不存在")
    return acc
