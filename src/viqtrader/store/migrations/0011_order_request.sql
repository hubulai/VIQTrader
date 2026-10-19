-- P6 Web 控制台：人工确认闸待执行意图表（只增不改）
-- 定位：全平台唯一待执行意图入口。web 层只写本表（确认信号/手工录单），
--       执行侧 scripts/execute_orders.py 扫描 pending → 构造 OrderIntent →
--       ExecutionService.submit_order 全链留痕 → 回写 status/intent_id。
--       未确认意图永不进本表，更不可能成交（6.3 红线：v1 无全自动路径）。
-- 与 order_intent 的分工：本表是「人工已确认、待引擎消费」的前置队列；
--       order_intent 由 ExecutionService 自己落库，承载规则校验与撮合结果。

CREATE TABLE order_request (
    request_id   TEXT PRIMARY KEY,
    source_type  TEXT NOT NULL,              -- swing_signal（波段信号确认）/ manual（控制台手工录单）
    source_id    TEXT,                       -- 来源主键（如 swing_signal.signal_id）
    account_id   TEXT NOT NULL,
    mode         TEXT NOT NULL,              -- value / swing
    code         TEXT NOT NULL,
    market       TEXT NOT NULL,              -- SH / SZ / BJ / HK
    side         TEXT NOT NULL,              -- BUY / SELL
    order_type   TEXT NOT NULL,              -- MARKET / LIMIT
    qty          INTEGER NOT NULL,
    price        DOUBLE,                     -- 限价（MARKET 单为 NULL，原币）
    thesis_id    TEXT,                       -- 价值轨关联买入逻辑（可选）
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending/executed/rejected/cancelled
    intent_id    TEXT,                       -- 执行后回写关联 order_intent.intent_id
    note         TEXT,                       -- 拒绝原因 / 人工备注
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    executed_at  TIMESTAMPTZ,
    config_hash  TEXT NOT NULL,
    code_version TEXT NOT NULL
);
