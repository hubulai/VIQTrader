-- 0015: 财务多版本表（point-in-time 对齐修复）。
--
-- 背景：QMT 财务 report_time 口径下同一报告期存在多行（原始披露 + 后续调整/重述），
-- 各表公告日（m_anntime）可能不一致。旧 fact_financial 主键 (code, report_period)
-- 只能保留「公告日最晚」的那一版，实测 2010-2019 年 ann_date 中位滞后约 390 天
-- （正常 30-120 天），导致按 ann_date 的 PIT 对齐在这些年份完全失效。
--
-- 本迁移新增版本表：
--   * 主键 (code, report_period, ann_date)，保留全部披露版本（原始 + 重述）；
--   * 列契约与 fact_financial 完全一致（单季流量口径，as-of 差分见 qmt._map_financial）；
--   * ann_date 为版本可见日，NOT NULL（无公告日的行不进入版本表）。
--
-- fact_financial 保留（不加列、不改主键），同步时由版本表重建为「当前快照」：
--   值 = 该报告期最新版本（最新重述口径，当前时点最准确）；
--   ann_date = 该报告期首次披露日（供狩猎清单「新财报发布」与滞后分布体检使用）。
-- 消费口径：
--   * PIT 消费方（估值 TTM / 回测 context / 复盘快照 / 财务还原）读 fact_financial_version；
--   * 当前快照消费方（价值初筛 / 止损扫描 / 狩猎清单）继续读 fact_financial。

CREATE TABLE fact_financial_version (
    code          TEXT NOT NULL,
    report_period DATE NOT NULL,                -- 报告期
    ann_date      DATE NOT NULL,                -- 版本可见日（该版本公告日）
    revenue       DOUBLE,
    net_profit    DOUBLE,
    roe           DOUBLE,
    roa           DOUBLE,
    gross_margin  DOUBLE,
    net_margin    DOUBLE,
    debt_ratio    DOUBLE,
    ocf           DOUBLE,                       -- 经营性现金流（单季）
    fcf           DOUBLE,                       -- 自由现金流（单季）
    eps           DOUBLE,
    bps           DOUBLE,
    total_assets  DOUBLE,
    total_equity  DOUBLE,
    PRIMARY KEY (code, report_period, ann_date)
);
