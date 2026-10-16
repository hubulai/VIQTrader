-- P4.5 波段轨 LLM 建议层：波段信号表（设计文档 4.2 / 5.2，只增不改）
-- 定位：LLM 选股/解读产物是建议，不产 OrderIntent、不走策略生命周期（那是代码型
-- 量化策略的路径）；status='pending' 等 P6 控制台人工确认后才进执行管线。
-- 与价值轨 watchlist（存档研究→人工拍板）同构。
-- 一行承载顺序流水线双 Agent 产物：波段选股 → 信号解读（1:1）。

CREATE TABLE swing_signal (
    signal_id              TEXT PRIMARY KEY,
    code                   TEXT NOT NULL,
    market                 TEXT NOT NULL,                  -- SH / SZ
    name                   TEXT NOT NULL,
    signal_date            DATE NOT NULL,                  -- 初筛日（最近交易日）
    -- 确定性初筛快照（零 LLM，供审计与回放）
    screen_features_json   TEXT NOT NULL,
    -- 波段选股 Agent 产物
    signal_type            TEXT,                           -- breakout/pullback/reversal/range/none
    pattern_desc           TEXT,
    entry_ref              DOUBLE,                         -- 进场参考位（须为给定 K 线真实价位）
    stop_ref               DOUBLE,                         -- 止损参考位
    target_ref             DOUBLE,                         -- 目标参考位
    picker_risks_json      TEXT,
    picker_run_id          TEXT,
    -- 信号解读 Agent 产物
    interpretation         TEXT,                           -- 人话翻译
    invalidation_json      TEXT,                           -- 信号失效条件
    interpreter_risks_json TEXT,
    win_rate_note          TEXT,                           -- 历史胜率佐证；空库为「待积累」，禁止编数字
    interpreter_run_id     TEXT,
    -- 人工确认闸（v1 无任何自动路径）
    status                 TEXT NOT NULL DEFAULT 'pending', -- pending/confirmed/rejected/expired
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (code, signal_date)                              -- 每只标的每个交易日至多一条信号
);
