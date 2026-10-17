-- 0008: 股本结构变动历史表（估值 market_cap / ps_ttm 数据前置）。
-- 数据源：东财 datacenter-web RPT_F10_EH_EQUITY（F10 股本变动历史，A 股全市场）。
-- 每行 = 某时点（源 END_DATE）的股本结构快照；估值引擎按 change_date 对
-- trade_date 做 backward 对齐，取「最近一次已生效的股本结构」计算市值。
-- 说明：股本快照为客观事实数据（时点已实际生效），非业绩预测，按 END_DATE
-- 对齐与东财 F10 展示口径一致；NOTICE_DATE 缺失率不可控，不作对齐键。

CREATE TABLE fact_share_structure (
    code            TEXT NOT NULL,       -- 6 位代码（不带市场后缀）
    change_date     DATE NOT NULL,       -- 股本结构时点（源 END_DATE）
    total_shares    DOUBLE,              -- 总股本（股，源 TOTAL_SHARES）
    total_a_shares  DOUBLE,              -- 总 A 股份（股，源 TOTAL_A_SHARES）
    listed_a_shares DOUBLE,              -- 已上市流通 A 股（股，源 LISTED_A_SHARES）
    change_reason   TEXT,                -- 变动原因（源 CHANGE_REASON）
    PRIMARY KEY (code, change_date)
);
