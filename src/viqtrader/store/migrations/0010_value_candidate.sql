-- P4.4 价值初筛候选池（排除法漏斗产物，零 LLM，只增不改）
-- 定位：初筛只负责把全市场缩到「质量达标 + 估值不贵」的小池子；深度研究由人工
-- 挑候选 → 投委会（run_committee.py）→ 狩猎清单（watchlist.py add），三段链路各自
-- 已有 CLI 承接，本表不做状态机（候选池是每周快照，历史保留可对比）。
-- 质量口径：roe 代理 ROIC（QMT 财务 5 表无 roic 字段，与止损扫描 engine/risk 同口径；
-- 后续补 ROIC 数据源后切换）。年度报告按 ann_date ≤ 筛选日防前视。
-- 每周快照：UNIQUE (screen_date, code) 幂等，同日重跑先删后插。

CREATE TABLE value_candidate (
    screen_date       DATE NOT NULL,                 -- 筛选业务日（最新交易日）
    code              TEXT NOT NULL,
    market            TEXT NOT NULL,
    name              TEXT NOT NULL,
    industry          TEXT,                          -- dim_stock.industry（能力圈过滤依据）
    -- 质量漏斗快照（年度报告口径）
    roe_years         INTEGER,                       -- roe 连续达标年数（实际值，可能大于配置要求）
    roe_mean          DOUBLE,                        -- 观测窗口内 roe 均值（主排序键）
    profit_years      INTEGER,                       -- 连续盈利年数
    fcf_mean          DOUBLE,                        -- 观测窗口内 fcf 均值（并列排序键）
    fcf_all_positive  BOOLEAN,                       -- 窗口内 fcf 是否全为正
    debt_ratio_latest DOUBLE,                        -- 最新年度资产负债率
    revenue_cagr      DOUBLE,                        -- 窗口首末年度营收复合增速（参考列，不过滤）
    -- 估值截面（最新交易日 fact_valuation）
    pe_ttm            DOUBLE,
    pb                DOUBLE,
    dv_ratio          DOUBLE,
    pe_percentile     DOUBLE,                        -- 250 日分位；缺失 = 历史不足
    gate_passed       BOOLEAN NOT NULL,              -- 估值闸门：pe_percentile ≤ 阈值
    quality_score     DOUBLE NOT NULL,               -- 排序键 = roe_mean（并列按 fcf_mean）
    config_hash       TEXT NOT NULL,                 -- 筛选参数快照哈希
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (screen_date, code)
);
