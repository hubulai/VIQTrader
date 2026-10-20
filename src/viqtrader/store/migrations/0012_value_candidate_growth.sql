-- P4.4.1 增补：价值候选参考列 profit_cagr（净利复合增速，轻方案只展示不过滤）
-- 口径：net_profit 窗口首末年复合增速（与 revenue_cagr 同窗口同算法）。
-- QMT 财务 5 表无扣非净利字段，暂用含非经常性损益的 net_profit 代替
-- （与 roe 代理 ROIC 同一降级模式），扣非数据源补齐后切换口径。
-- 历史批次该列为 NULL，重算后填充；消费端只展示不参与过滤/排序。

ALTER TABLE value_candidate ADD COLUMN IF NOT EXISTS profit_cagr DOUBLE;
