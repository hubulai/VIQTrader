-- P4.5.1 波段轨多策略架构：swing_signal 记录产生信号的策略（只增不改）
-- 注：DuckDB 不支持 ADD COLUMN 带约束（NOT NULL DEFAULT），
--     列允许 NULL，历史行（P4.5 三形态并集产物）由本迁移回填 trend_momentum，
--     写入层（pipeline）自此必填，读侧遇 NULL 按 trend_momentum 解释。

ALTER TABLE swing_signal ADD COLUMN strategy_id TEXT;  -- 注册表 strategy_id，应用层保证非空
UPDATE swing_signal SET strategy_id = 'trend_momentum' WHERE strategy_id IS NULL;
