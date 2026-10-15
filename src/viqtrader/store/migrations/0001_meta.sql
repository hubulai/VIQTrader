-- 平台元数据表：参数快照（审计与回放的地基）
-- 风险事件 / Agent 决策均以 config_hash 关联此表，可回溯当时生效的全部参数

CREATE TABLE IF NOT EXISTS config_snapshot (
    config_hash  TEXT PRIMARY KEY,
    yaml         TEXT NOT NULL,
    recorded_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    note         TEXT
);
