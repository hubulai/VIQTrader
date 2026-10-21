-- 0014: P7 元复盘 Agent —— 学习提案表（横切学习资产的唯一出口）。
-- 元复盘 Agent 只提议不执行：提案先落 draft，经反方审查（dissent_reviewed）、
-- 人工拍板（approved / rejected），人工合并 user.yaml 后 --applied 收口（applied）。
-- 状态机：draft → dissent_reviewed → approved / rejected → applied
-- 红线：平台禁止任何自动改写配置的路径，applied_config_hash 仅记录人工合并后的
-- 参数快照哈希，供审计回溯「哪次提案生效于哪份配置」。

CREATE TABLE learning_proposal (
    proposal_id         TEXT PRIMARY KEY,        -- meta_review:<window_end>:<seq>（确定性幂等锚点）
    window_start        DATE NOT NULL,           -- 复盘窗口起
    window_end          DATE NOT NULL,           -- 复盘窗口止
    kind                TEXT NOT NULL,           -- param_change / thesis_retire / screen_rule / prompt_change
    title               TEXT NOT NULL,           -- 一句话提案标题
    payload_json        TEXT NOT NULL,           -- 提案正文（JSON，必填键随 kind，见契约）
    rationale           TEXT NOT NULL,           -- 提案理由（定性）
    evidence_json       TEXT NOT NULL,           -- 证据引用（JSON 数组，非空——无据不提红线）
    confidence          TEXT NOT NULL,           -- high / medium / low（样本不足必须降级）
    status              TEXT NOT NULL DEFAULT 'draft',
    dissent_output_id   TEXT,                    -- 反方审查 agent_output.output_id
    applied_config_hash TEXT,                    -- 人工合并配置后的 config_snapshot.config_hash
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
