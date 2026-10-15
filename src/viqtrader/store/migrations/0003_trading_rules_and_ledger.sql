-- P2 引擎层迁移：交易规则 + 双轨账本（设计文档第四节，只增不改）
-- 注：DuckDB 的 ALTER TABLE ADD COLUMN 不支持外键约束，
--     trade_log.intent_id 的引用关系由应用层保证（engine/execution/service 写入前校验）

-- 订单意图（核心表，记录所有下单意图的全生命周期）
CREATE TABLE order_intent (
    intent_id      TEXT PRIMARY KEY,
    account_id     TEXT NOT NULL REFERENCES account(account_id),
    mode           TEXT NOT NULL,                -- value / swing
    code           TEXT NOT NULL,
    market         TEXT NOT NULL,                -- SH / SZ / BJ / HK
    side           TEXT NOT NULL,                -- BUY / SELL
    order_type     TEXT NOT NULL,                -- MARKET / LIMIT / STOP / STOP_LIMIT
    qty            INTEGER NOT NULL,
    price          DOUBLE,                       -- 限价（MARKET 单为 NULL）
    stop_price     DOUBLE,                       -- 止损触发价
    time_in_force  TEXT NOT NULL DEFAULT 'GTC',
    status         TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING/REJECTED/FILLED/CANCELLED
    rejection_reason TEXT,
    thesis_id      TEXT,                         -- 价值轨关联买入逻辑
    strategy_id    TEXT,                         -- 波段轨关联策略
    submitted_at   TIMESTAMPTZ NOT NULL,
    filled_at      TIMESTAMPTZ,
    filled_price   DOUBLE,
    config_hash    TEXT NOT NULL,
    code_version   TEXT NOT NULL
);

-- 市场状态快照（日频预计算，涨跌停/停牌/ST 判断依据）
CREATE TABLE market_status (
    code            TEXT NOT NULL,
    trade_date      DATE NOT NULL,
    board           TEXT NOT NULL,               -- MAIN / CHINEXT / STAR / BSE / HK
    is_suspended    BOOLEAN NOT NULL DEFAULT FALSE,
    is_st           BOOLEAN NOT NULL DEFAULT FALSE,
    up_limit_price  DOUBLE,
    down_limit_price DOUBLE,
    is_hk_connect   BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (code, trade_date)
);

-- 规则校验流水（每次 Intent 校验的完整记录）
CREATE TABLE rule_check_log (
    check_id     TEXT PRIMARY KEY,
    intent_id    TEXT NOT NULL REFERENCES order_intent(intent_id),
    phase        TEXT NOT NULL,                  -- MARKET / ACCOUNT / RISK
    rule_type    TEXT NOT NULL,                  -- T1 / PRICE_LIMIT / LOT_SIZE / ...
    passed       BOOLEAN NOT NULL,
    message      TEXT,
    checked_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    config_hash  TEXT NOT NULL,
    code_version TEXT NOT NULL
);

-- 板块维度（dim_stock 补充，从 code 前缀推断，显式存储）
ALTER TABLE dim_stock ADD COLUMN board TEXT;     -- MAIN / CHINEXT / STAR / BSE / HK

-- position 增加 T+1 冻结量
-- 注：DuckDB 不支持 ADD COLUMN 带约束（NOT NULL DEFAULT），
--     列允许 NULL，历史行由本迁移回填 0，引擎层读写一律 COALESCE 为 0
ALTER TABLE position ADD COLUMN frozen_qty INTEGER;
UPDATE position SET frozen_qty = 0 WHERE frozen_qty IS NULL;
-- 约束：frozen_qty ≤ qty（应用层保证）

-- trade_log 关联订单意图
ALTER TABLE trade_log ADD COLUMN intent_id TEXT;  -- 引用 order_intent(intent_id)，应用层保证
