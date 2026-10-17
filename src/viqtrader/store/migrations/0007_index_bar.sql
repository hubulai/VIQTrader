-- P4.6 复盘归因：基准指数日线行情表（只增不改）
-- 与 fact_daily_bar 分表存放，避免指数行污染全市场统计（等权/市值加权基准、初筛 universe）。
-- 数据来源：AkShare（东财 index_zh_a_hist），QMT 不声明该能力。

CREATE TABLE IF NOT EXISTS fact_index_bar (
    code       TEXT NOT NULL,               -- 指数代码（如 000300 沪深300）
    trade_date DATE NOT NULL,
    open       DOUBLE NOT NULL,
    high       DOUBLE NOT NULL,
    low        DOUBLE NOT NULL,
    close      DOUBLE NOT NULL,
    volume     BIGINT,
    amount     DOUBLE,
    PRIMARY KEY (code, trade_date)
);
