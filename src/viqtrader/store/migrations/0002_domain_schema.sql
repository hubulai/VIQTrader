-- 领域全量建表（对应设计文档第三节数据模型）
-- JSON 载荷统一用 TEXT 存储（避免依赖 json 扩展），由应用层序列化/校验
-- 审计红线：risk_event / agent_run 必须携带 config_hash + code_version

-- ========== 3.1 维度表 ==========

CREATE TABLE dim_stock (
    code                TEXT NOT NULL,
    market              TEXT NOT NULL,          -- SH / SZ / BJ / HK
    name                TEXT NOT NULL,
    industry            TEXT,
    list_date           DATE,
    is_hk_connect       BOOLEAN NOT NULL DEFAULT FALSE,
    hk_connect_in_date  DATE,
    hk_connect_out_date DATE,                   -- NULL = 仍在名单内
    PRIMARY KEY (code, market)
);

CREATE TABLE dim_hk_connect_history (
    code          TEXT NOT NULL,
    change_date   DATE NOT NULL,                -- 变动生效日
    action        TEXT NOT NULL,                -- IN / OUT
    announce_date DATE,                         -- 公告日
    PRIMARY KEY (code, change_date, action)
);

-- ========== 3.2 事实表 ==========

CREATE TABLE fact_daily_bar (
    code          TEXT NOT NULL,
    trade_date    DATE NOT NULL,
    open          DOUBLE,
    high          DOUBLE,
    low           DOUBLE,
    close         DOUBLE,                       -- 未复权价
    volume        BIGINT,
    amount        DOUBLE,                       -- 成交额
    adj_factor    DOUBLE,                       -- 复权因子
    turnover_rate DOUBLE,                       -- 换手率
    PRIMARY KEY (code, trade_date)
);

CREATE TABLE fact_financial (
    code          TEXT NOT NULL,
    report_period DATE NOT NULL,                -- 报告期
    ann_date      DATE,                         -- 披露日（回测按此日才可见，防前视）
    revenue       DOUBLE,
    net_profit    DOUBLE,
    roe           DOUBLE,
    roa           DOUBLE,
    gross_margin  DOUBLE,
    net_margin    DOUBLE,
    debt_ratio    DOUBLE,
    ocf           DOUBLE,                       -- 经营性现金流
    fcf           DOUBLE,                       -- 自由现金流
    eps           DOUBLE,
    bps           DOUBLE,
    total_assets  DOUBLE,
    total_equity  DOUBLE,
    PRIMARY KEY (code, report_period)
);

CREATE TABLE fact_valuation (
    code           TEXT NOT NULL,
    trade_date     DATE NOT NULL,
    pe_ttm         DOUBLE,
    pb             DOUBLE,
    ps_ttm         DOUBLE,
    dv_ratio       DOUBLE,                      -- 股息率
    market_cap     DOUBLE,
    pe_percentile  DOUBLE,                      -- 历史分位（自算）
    PRIMARY KEY (code, trade_date)
);

CREATE TABLE fact_dividend (
    code               TEXT NOT NULL,
    ex_date            DATE NOT NULL,
    pay_date           DATE,
    dividend_per_share DOUBLE,
    dividend_type      TEXT NOT NULL,           -- 现金 / 送股
    PRIMARY KEY (code, ex_date, dividend_type)
);

CREATE TABLE fact_disclosure (
    code     TEXT NOT NULL,
    ann_date DATE NOT NULL,
    title    TEXT NOT NULL,
    category TEXT,                              -- 财报 / 重大事项 / 风险提示
    summary  TEXT,                              -- 摘要文本（供 LLM）
    PRIMARY KEY (code, ann_date, title)
);

CREATE TABLE fact_fx_rate (
    pair       TEXT NOT NULL,                   -- HKD/CNY
    trade_date DATE NOT NULL,
    rate       DOUBLE NOT NULL,
    PRIMARY KEY (pair, trade_date)
);

-- ========== 3.3 账本表（子账本，核心） ==========

CREATE TABLE account (
    account_id    TEXT PRIMARY KEY,
    mode          TEXT NOT NULL,                -- value / swing
    base_currency TEXT NOT NULL DEFAULT 'CNY',
    capital_quota DOUBLE NOT NULL,              -- 初始资金配额
    target_ratio  DOUBLE NOT NULL               -- 价值 0.8 / 波段 0.2
);

CREATE TABLE position (
    position_id  TEXT PRIMARY KEY,
    account_id   TEXT NOT NULL REFERENCES account(account_id),
    mode         TEXT NOT NULL,                 -- value / swing
    code         TEXT NOT NULL,
    qty          INTEGER NOT NULL,
    avg_cost     DOUBLE NOT NULL,               -- 原币成本
    avg_cost_cny DOUBLE NOT NULL,               -- 人民币口径成本
    open_date    DATE NOT NULL,
    thesis_id    TEXT,                          -- 关联买入逻辑（复盘用）
    -- 红线：同标的在 value 与 swing 下是两条独立记录，禁止合并
    UNIQUE (account_id, code)
);

CREATE TABLE trade_log (
    trade_id     TEXT PRIMARY KEY,
    account_id   TEXT NOT NULL,
    mode         TEXT NOT NULL,
    code         TEXT NOT NULL,
    side         TEXT NOT NULL,                 -- BUY / SELL
    qty          INTEGER NOT NULL,
    price        DOUBLE NOT NULL,               -- 原币价
    commission   DOUBLE NOT NULL DEFAULT 0,
    stamp_tax    DOUBLE NOT NULL DEFAULT 0,
    transfer_fee DOUBLE NOT NULL DEFAULT 0,
    fx_rate      DOUBLE,                        -- 港股通汇兑
    amount_cny   DOUBLE NOT NULL,               -- 人民币结算额
    trade_date   DATE NOT NULL
);

CREATE TABLE cash_ledger (
    entry_id    TEXT PRIMARY KEY,
    account_id  TEXT NOT NULL,
    trade_date  DATE NOT NULL,
    change_type TEXT NOT NULL,                  -- 买入/卖出/分红/费用/汇兑
    amount_cny  DOUBLE NOT NULL,
    balance_cny DOUBLE NOT NULL                 -- 结余
);

-- ========== 3.4 规则与审计表 ==========

CREATE TABLE risk_rule (
    rule_id        TEXT PRIMARY KEY,
    mode           TEXT NOT NULL,               -- value / swing / shared
    rule_key       TEXT NOT NULL,
    rule_value     TEXT NOT NULL,               -- JSON
    config_hash    TEXT NOT NULL,               -- 与 config 文件同步的当前生效值
    effective_from TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE risk_event (
    event_id        TEXT PRIMARY KEY,
    event_ts        TIMESTAMPTZ NOT NULL DEFAULT now(),
    account_id      TEXT,
    mode            TEXT,
    rule_key        TEXT NOT NULL,
    code            TEXT,
    severity        TEXT NOT NULL,              -- INFO / WARN / CRITICAL / FATAL
    decision        TEXT NOT NULL,              -- ALLOW / DENY / ALERT
    intent_snapshot TEXT,                       -- JSON：被检查的下单意图
    message         TEXT,
    config_hash     TEXT NOT NULL,              -- 审计红线：当时生效参数
    code_version    TEXT NOT NULL               -- 审计红线：当时代码版本
);

CREATE TABLE agent_run (
    run_id         TEXT PRIMARY KEY,
    agent_name     TEXT NOT NULL,
    started_at     TIMESTAMPTZ NOT NULL,
    finished_at    TIMESTAMPTZ,
    model          TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    input_snapshot TEXT,                        -- JSON
    token_in       INTEGER,
    token_out      INTEGER,
    cost_cny       DOUBLE,
    status         TEXT NOT NULL,               -- SUCCESS / FAILED / DEGRADED
    config_hash    TEXT NOT NULL,
    code_version   TEXT NOT NULL
);

CREATE TABLE agent_output (
    output_id   TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL REFERENCES agent_run(run_id),
    output_json TEXT NOT NULL,                  -- 结构化 JSON 结论
    data_refs   TEXT,                           -- JSON：数据引用（不允许无据推断）
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE thesis (
    thesis_id   TEXT PRIMARY KEY,
    code        TEXT NOT NULL,
    mode        TEXT NOT NULL,                  -- value / swing
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    summary     TEXT NOT NULL,
    assumptions TEXT,                           -- JSON：关键假设（防逻辑漂移比对基准）
    status      TEXT NOT NULL DEFAULT 'active'  -- active / falsified / expired
);

CREATE TABLE committee_session (
    session_id          TEXT PRIMARY KEY,
    code                TEXT NOT NULL,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    valuation_output_id TEXT,                   -- 估值方结论
    dissent_output_id   TEXT,                   -- 反方结论（并列存档）
    final_decision      TEXT,                   -- 人最终拍板
    decided_by          TEXT,
    decided_at          TIMESTAMPTZ,
    notes               TEXT
);

-- ========== 自我学习三表（设计文档第十一节） ==========

CREATE TABLE outcome_label (
    label_id       TEXT PRIMARY KEY,
    decision_type  TEXT NOT NULL,               -- 关联的决策类型（买入/卖出/估值...）
    decision_id    TEXT NOT NULL,               -- 关联任何决策 ID
    horizon_days   INTEGER NOT NULL,            -- 30 / 180 / 365 / 1095
    labeled_at     TIMESTAMPTZ,
    excess_return  DOUBLE,                      -- 超额收益
    valuation_error DOUBLE,                     -- 估值误差
    thesis_delivered BOOLEAN,                   -- 逻辑兑现与否
    metrics_json   TEXT,
    UNIQUE (decision_type, decision_id, horizon_days)
);

CREATE TABLE agent_scorecard (
    agent_name         TEXT NOT NULL,
    decision_type      TEXT NOT NULL,
    window_end         DATE NOT NULL,
    sample_size        INTEGER NOT NULL,
    hit_rate           DOUBLE,
    error_mean         DOUBLE,
    error_std          DOUBLE,
    false_positive_rate DOUBLE,
    metrics_json       TEXT,
    PRIMARY KEY (agent_name, decision_type, window_end)
);

CREATE TABLE case_library (
    case_id      TEXT PRIMARY KEY,
    code         TEXT,
    industry     TEXT,
    thesis_type  TEXT,
    failure_mode TEXT,                          -- 失败模式标签
    title        TEXT NOT NULL,
    postmortem   TEXT,                          -- 尸检结论
    tags         TEXT,                          -- JSON：模式标签，供 few-shot 检索
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE watchlist (
    watch_id             TEXT PRIMARY KEY,
    code                 TEXT NOT NULL,
    committee_session_id TEXT,                  -- 关联投委会存档
    target_buy_price     DOUBLE NOT NULL,       -- 安全边际买入价
    required_discount    DOUBLE,                -- 要求折价率
    status               TEXT NOT NULL DEFAULT 'watching',  -- watching/ready/triggered/expired
    research_summary     TEXT,
    last_review_date     DATE,                  -- 最近复核日期（90 天保质期基准）
    thesis_id            TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ========== 3.5 文档与外部观点表 ==========

CREATE TABLE document (
    doc_id       TEXT PRIMARY KEY,
    file_path    TEXT NOT NULL,
    content_hash TEXT NOT NULL UNIQUE,          -- 内容 hash 去重
    doc_type     TEXT NOT NULL,                 -- 个股研报 / 行业研报 / 其他
    code         TEXT,
    industry     TEXT,
    org          TEXT,                          -- 机构
    publish_date DATE,
    parse_status TEXT NOT NULL DEFAULT 'pending',  -- pending / parsed / failed
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE document_analysis (
    analysis_id    TEXT PRIMARY KEY,
    doc_id         TEXT NOT NULL UNIQUE REFERENCES document(doc_id),  -- 一份只解析一次
    analyzed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    result_json    TEXT NOT NULL,               -- 摘要/核心假设/盈利预测/评级目标价/风险点
    model          TEXT NOT NULL,
    prompt_version TEXT NOT NULL
);

CREATE TABLE analyst_forecast (
    forecast_id         TEXT PRIMARY KEY,
    doc_id              TEXT NOT NULL REFERENCES document(doc_id),
    org                 TEXT,
    forecast_date       DATE,
    code                TEXT NOT NULL,
    fiscal_year         TEXT NOT NULL,          -- 财年（如 2026 / 2026E）
    forecast_net_profit DOUBLE,
    forecast_eps        DOUBLE,
    target_price        DOUBLE,
    UNIQUE (doc_id, code, fiscal_year)
);
