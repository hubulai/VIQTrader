-- 0009: P5.3 学习地基 —— 重建 outcome_label 为拍板的标签口径。
-- 背景：0002 中的 outcome_label 是占位设计（label_id/horizon_days 整数四时点），
-- 自建仓以来无任何生产者/消费者、表为空。P5.3 设计评审拍板新口径：
-- T+N 固定时点（波段 T+5/10/20，价值 T+20/60/250）+ 触位事件（信号自带
-- stop/target 参考位，先触者定胜负，同日触两侧保守 stop 先）。
-- 经用户确认以本脚本 DROP 后重建（不修改 0002 文件本身；空表无数据损失，
-- 全新装库按序重放 0002→0009 结果一致）。

DROP TABLE outcome_label;

CREATE TABLE outcome_label (
    decision_type TEXT NOT NULL,        -- 决策来源（=agent_name）：swing_pick / valuation
    decision_ref  TEXT NOT NULL,        -- swing_signal.signal_id / committee_session.session_id
    code          TEXT NOT NULL,
    decision_date DATE NOT NULL,        -- 决策发生日（标签到期计算基准）
    label_kind    TEXT NOT NULL,        -- t_n / touch / value_eval
    horizon       TEXT NOT NULL,        -- T+5 / T+10 / T+20 / T+60 / T+250 / first_touch
    label_date    DATE,                 -- 实际判定日（到期交易日 / 触位日）；NULL=观察中
    return_pct    DOUBLE,               -- 判定区间收益（基准价→判定价，复权口径）
    hit           BOOLEAN,              -- t_n: return_pct>0；touch: 目标先触=TRUE、止损先触=FALSE
    detail_json   TEXT,                 -- 基准价/判定价/触位侧/估值误差等扩展事实（JSON）
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (decision_type, decision_ref, label_kind, horizon)  -- 幂等锚点
);
