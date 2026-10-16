-- P3.6 波段轨策略生命周期：策略定义 + 阶段状态 + 迁移流水（设计文档 5.3，只增不改）
-- 阶段命名统一设计文档 5.3：INCUBATING / PAPER / PROBATION / PRODUCTION / RETIRED
-- （default.yaml 早期注释中的 incubation/paper/trial/main 为别名，以此为准）
-- 价值轨不适用此生命周期（走 thesis 证伪），registry 层对 track != 'swing' 拒绝注册

-- 策略定义（版本化：同一策略名可有多版本，各自独立生命周期）
CREATE TABLE strategy_definition (
    strategy_id   TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    track         TEXT NOT NULL,                 -- swing（v1 仅波段轨适用生命周期）
    version       TEXT NOT NULL,
    params_json   TEXT,                          -- 策略参数组（JSON）
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    config_hash   TEXT NOT NULL,
    code_version  TEXT NOT NULL,
    UNIQUE (name, version)
);

-- 策略阶段当前态（每策略一行）
CREATE TABLE strategy_stage (
    strategy_id     TEXT PRIMARY KEY REFERENCES strategy_definition(strategy_id),
    stage           TEXT NOT NULL,               -- INCUBATING/PAPER/PROBATION/PRODUCTION/RETIRED
    capital_pct     DOUBLE NOT NULL DEFAULT 0,   -- 占波段子账本的资金配额比例（记录+联动更新，不做下单硬阻断）
    kpi_json        TEXT,                        -- 最近一次 KPI 快照（JSON）
    entered_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 阶段迁移流水（全量留痕，含 config_hash + code_version 审计红线）
CREATE TABLE strategy_stage_history (
    history_id      TEXT PRIMARY KEY,
    strategy_id     TEXT NOT NULL REFERENCES strategy_definition(strategy_id),
    from_stage      TEXT,                        -- 首次注册为 NULL
    to_stage        TEXT NOT NULL,
    reason          TEXT NOT NULL,               -- 判定依据（KPI 数值摘要 / 人工 / Walk-Forward 门禁）
    kpi_json        TEXT,                        -- 判定时的 KPI 快照
    decided_by      TEXT NOT NULL DEFAULT 'engine',  -- engine / human
    decided_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    config_hash     TEXT NOT NULL,
    code_version    TEXT NOT NULL
);
