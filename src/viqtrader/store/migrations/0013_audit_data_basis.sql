-- 0013: 审计表口径来源标记（单季口径事故留痕）。
-- 背景：fact_financial 流量列（revenue/net_profit/ocf/fcf/eps）落库为**单季口径**
-- （设计意图供估值层 rolling(4) 得 TTM），但 valuation/dissent Agent 与价值初筛、止损
-- 扫描、复盘 pipeline、回测财报视图曾误当年度/累计值消费。修复后（见 data/financials.py）
-- 统一还原为 as-reported 累计口径（同年 Q1..本报告期求和；Q4 即全年）。
-- 历史已落库的审计行属不可变事实，不删除；本迁移新增 data_basis 列标记口径来源，
-- 供学习层/人工回溯时识别并排除：
--   NULL                    = 未标注（默认；本列仅用于标记已知口径缺陷的历史行）
--   'single_quarter_legacy' = 生成时输入含单季口径财务（修复前）
-- 影响面（2026-10-16 那次 600519 投委会）：agent_run 2 条 valuation + 1 条 dissent，
-- 及其派生的 1 条 committee_session（另 1 条估值超时无产物，不标记）。
-- ADD COLUMN IF NOT EXISTS 使重复应用幂等；回填仅命中 data_basis IS NULL 的行。

ALTER TABLE agent_run ADD COLUMN IF NOT EXISTS data_basis TEXT;
ALTER TABLE committee_session ADD COLUMN IF NOT EXISTS data_basis TEXT;

-- 修复前产出的 valuation / dissent 运行 → 标记单季口径（其余 Agent 不消费财务流量）。
UPDATE agent_run
SET data_basis = 'single_quarter_legacy'
WHERE data_basis IS NULL
  AND agent_name IN ('valuation', 'dissent')
  AND CAST(started_at AS TIMESTAMP) < TIMESTAMPTZ '2026-10-20 00:00:00';

-- 派生于上述受影响输出的投委会会话 → 一并标记（按 output_id 精确关联）。
UPDATE committee_session
SET data_basis = 'single_quarter_legacy'
WHERE data_basis IS NULL
  AND (
      valuation_output_id IN (
          SELECT o.output_id FROM agent_output o
          JOIN agent_run r ON r.run_id = o.run_id
          WHERE r.data_basis = 'single_quarter_legacy'
      )
      OR dissent_output_id IN (
          SELECT o.output_id FROM agent_output o
          JOIN agent_run r ON r.run_id = o.run_id
          WHERE r.data_basis = 'single_quarter_legacy'
      )
  );
