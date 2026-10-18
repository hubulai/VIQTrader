# VIQTrader

个人自用的 A股 + 港股通双轨股票分析交易平台。核心是**原教旨主义价值投资**，而非高频交易系统。

> 设计依据：[股票分析交易平台设计方案](doc/股票分析交易平台设计方案.md) v1（14 章）、[实现计划与任务拆分](doc/实现计划与任务拆分.md)（P0~P7）。
> 本文档随开发过程持续更新——**每完成一个任务，同步更新「当前进度」一节**。

---

## 一、项目目标

| 维度 | 目标 |
|---|---|
| 使用者 | 个人自用，单用户，不对外开放 |
| 模式结构 | 双轨完全独立：价值轨（80% 资金配额）/ 波段轨（20% 资金配额） |
| 资金分账 | 单券商账户 → 两个逻辑子账本，各轨独立核算成本、仓位、盈亏；**同标的双轨持仓必须分两条记录，禁止合并** |
| 市场范围 | 主投 A股；港股仅限港股通标的（调出后只能卖不能买） |
| 交易闭环 | v1 **仅模拟盘**，不接实盘；架构预留实盘网关 |
| 决策机制 | 投委会式交叉验证（估值 Agent 输出 → 反方 Agent 校验漏洞 → **用户最终拍板**） |
| 技术栈 | Python 全栈 + DuckDB + 本地 Windows + Web 控制台 |

**价值哲学基线（价值轨）**：

1. 股权思维——买股票即买企业所有权，收益来自企业盈利与分红。
2. 估值是带假设的区间——输出 bull/base/bear 三档与全部假设，必须给出「自身假设 vs 市场一致预期的差异」。
3. 安全边际是唯一买入闸门——**质量优先排序**（ROIC 持续性、现金流转化率在前），便宜只是闸门不是排序键。
4. 能力圈是负面清单——行业白/黑名单在初筛第 0 步一票否决。
5. 卖出只有三个理由：逻辑证伪、价格远超价值、发现明显更好标的。**下跌从来不是卖出理由**。
6. 换手率是纪律指标——年换手率异常升高触发风格漂移告警。

---

## 二、当前进度

| 阶段 | 状态 | 说明 |
|---|---|---|
| P0.1 项目骨架 | ✅ 完成 | src 布局包 `viqtrader`，全部模块目录 + pre-commit（ruff） + .gitattributes(LF) |
| P0.2 配置体系 | ✅ 完成 | `default.yaml < user.yaml < VIQ_ 环境变量` 三层深合并、mtime 热加载、sha256 快照入库 |
| P0.3 依赖方向强制 | ✅ 完成 | import-linter 分层契约 + Web 与引擎解耦的 forbidden 契约，反向 import 探针已被拦截 |
| P0.4 DuckDB 迁移工具 | ✅ 完成 | 版本化、只增不改 checksum 强校验、幂等 |
| P1.1 全量建表 | ✅ 完成 | `0002_domain_schema.sql` 25 张领域表（维度/行情/财务/账本/审计/学习/狩猎清单/研报）+ `0001` 的 `config_snapshot` |
| P1.2 DataAdapter 契约 | ✅ 完成 | `data/base.py`（Protocol + 7 项能力 + 逐表列契约）、`data/contract.py`（结构/取值/行为校验）、`FakeAdapter` + 14 条契约用例 |
| P1.3 QMT 适配器 | ✅ 完成 | `data/adapters/qmt.py`：xtquant 实测联调通过——`get_market_data_ex` 日线（关键字传 `count=-1`，行索引兼容毫秒 epoch/YYYYMMDD）、`download_financial_data2` 财务 5 表逐表回调（`m_timetag`/`m_anntime` 列式结构，英文字段映射含 ROE/EPS/毛利率，无 ROA 留空）；板块 `'沪深京A股'`（含 BJ），无港股通板块→名单归 AkShare。**2026-10-16 实测修复 2 处**：① 5 表 merge 同报告期多版本（原披露+调整后）致主键冲突 → 先按报告期去重保留公告日最新，跨表公告日取最晚（保守无前视）；② QMT 流量为年初至今**累计**口径、估值引擎按**单季** rolling(4) → 适配器统一折算单季落库（Q1 原样、年内相邻差分、按（年，季度）判定相邻、缺上期保留累计宁缺勿错），茅台 PE_TTM 修复后 20.3（错值 7.5） |
| P1.4 AkShare 适配器 | ✅ 完成 | `data/adapters/akshare.py`：声明 4 项能力（FINANCIAL / DIVIDEND / FX_RATE / STOCK_LIST），每项均实测选源；DISCLOSURE（实测无港股公告源）、HK_CONNECT_HISTORY（无名单变动流水源）、DAILY_BAR（属 QMT 分工）不声明，调用即抛 `UnsupportedCapabilityError`。**2026-10-16 补 A股分红**：`fetch_dividend` 按代码位数路由市场——A股 6 位走巨潮 `stock_dividend_cninfo`（含派息日，填 P4.4 首跑暴露的 `dv_ratio=NaN` 缺口），港股 5 位走东财 `stock_hk_dividend_payout_em`；A股派息取税前值（估值层仅对 HK 扣 20%）。附离线映射单测 20 例 + 联网集成测试 10 例 |
| P1.5 自算估值引擎 | ✅ 完成 | `valuation.py` 纯函数（零状态零依赖）：PE/PB/PS/股息率/市值/PE 分位；backward 对齐公告日防前视、分母 0 返 NaN、PE 分位 250 日窗口样本不足 60 留空、港股通股息率按 0.2 保守税率 |
| P1.6 增量同步 + 质检 | ✅ 完成 | `data/sync.py` 增量同步编排器（起点自适应 + DELETE+INSERT 幂等 + 缺口检测 + 前视偏差硬过滤）；`data/quality.py` 质量校验 |
| P1.7 黄金数据集冻结 | ✅ 完成 | 8 只代表股 3 年日线基准 parquet + MANIFEST + 回归测试；东财 push2 域不可达改用新浪源 |
| P1.8 研报入库管线 | ✅ 完成 | `data/docs/ingestor.py`：pdfplumber 提取（Protocol 缝隙可注入假实现）、全文 SHA256 内容去重（改名仍判重）、文件名元数据正则（机构/代码/周期/日期，先剥日期再匹配代码防误识别）。**2026-10-16 元数据解析修复（P4.8 真实研报暴露）**：① 机构前缀格式 `华安证券-标题` 支持（券商后缀白名单防误抓），括号内纯数字不再误判为机构；② 括号显式代码 `(300866)` 最优先提取；③ 末尾 6 位日期缩写 `260809` 仅在「末尾连字符分隔 + 日历合法 + 首位非 A股代码前缀 0/3/4/6/8」时识别为日期，规避 `600519`=2060-05-19 合法日期撞车，日历非法（如 300866→dd=66）保留为代码 |
| P2.1 迁移 0003 | ✅ 完成 | `0003_trading_rules_and_ledger.sql`：order_intent / market_status / rule_check_log 3 新表 + dim_stock.board / position.frozen_qty / trade_log.intent_id 3 加列（DuckDB ADD COLUMN 不支持约束，NULL 容忍 + 回填，FK 由应用层保证） |
| P2.2 核心类型 + 市场状态预计算 | ✅ 完成 | `engine/types.py`（OrderIntent frozen 不可变 / RuleContext / TradeLog，金额全程 Decimal，to_decimal overload）；`engine/market_status.py`（板块推断、ST 判定、涨跌停价差异化计算四舍五入到分、停牌判定、幂等回填） |
| P2.3 三阶段规则管线 | ✅ 完成 | `engine/rules/`：TradingRule Protocol + TradingRuleEngine（MARKET→ACCOUNT→RISK，硬拒绝即终止，WARN 不阻断）；Phase1 市场 6 条（T1/交易日/停牌/涨跌停/手数/港股通名单）+ Phase2 账户 4 条（资金含费预估/持仓可用/双轨隔离红线/配额软限制 WARN+risk_event） |
| P2.4 LedgerEngine 双轨账本 | ✅ 完成 | `engine/ledger/engine.py`：FILLED 才扣款 + 扣款前重检资金（失败抛 InsufficientCashAtFill 全事务回滚）；A 股 T+1 买入冻结/日终解冻；加权成本；SELL 返回 realized_pnl；港股通分红 0.20 税率折 CNY；同标的双轨各入各账 |
| P2.5 网关 + 执行服务 | ✅ 完成 | `engine/execution/`：SimulatedGateway（回测 bar 撮合：MARKET 开盘价 ±10bp 滑点、涨跌停封死不成交、LIMIT 触及成交）、DryRunGateway（模拟盘参考价 dry-run）、build_rule_context、ExecutionService（intent 落库→三阶段校验→rule_check_log→risk_event→撮合→记账→状态回写，全链留痕） |
| P3.1 事件驱动回测引擎 | ✅ 完成 | `engine/backtest/`：日频回放主循环（T+1 开盘前解冻 → 除息日分红按开盘前持仓入账 → market_status 预计算 → 策略产 Intent → 三阶段校验+bar 撮合+记账 → 逐日净值）；决策时点方案 A（T-1 收盘决策 → T 日成交）；策略/规则/撮合/账本全部复用 P2 组件零重写 |
| P3.2 前视偏差硬约束 | ✅ 完成 | StrategyContext SQL 层强制 `trade_date < T` / `ann_date < T`（机制防线非自觉）；报告强制标注精度受限；防线测试：T 日查当日 bar/财报/估值全部不可见 |
| P3.5 Walk-Forward 滚动验证 | ✅ 完成 | `lifecycle/walk_forward.py`：IS 250/OOS 60 滚动切分（尾段不足 OOS 丢弃）+ IS Sharpe 寻优 + oos_is_ratio 均值 ≥0.7 且窗口 ≥2 硬门禁；保守规则（IS≤0 记 0、全无效参数跳窗）；acc_wf 试跑账户 Intent 重定向 + 每轮清场，主账户零污染 |
| P3.6 生命周期状态机 | ✅ 完成 | `lifecycle/registry.py` + `kpi.py` + `equity.py` + 迁移 `0004`：5 阶段 KPI 驱动自动迁移（判定优先级：单日亏损 -5% 立即退役 > 低 Sharpe 持续 14 自然日退役 > 主力跌破保持线降级 > 晋级，单步不跳档）；资金配额联动（0/0/5%/30%/0）记录不做下单硬阻断；退役清仓产 MARKET SELL Intent 走正常规则管线；KPI 本金口径=策略峰值累计净投入（防摊薄）；价值轨拒绝注册；entered_at 业务日期化支持回测回放 |
| P3.3 价值轨回测指标 | ✅ 完成 | `engine/backtest/value.py` 纯函数零 LLM：XIRR（trade_log 实际现金流 BUY负/SELL正 + 期末市值终值，二分法解 NPV=0）；算术超额（基准序列参数注入，默认沪深300 `value_track.benchmark_code`）；FIFO round-trip 持有期分布（count/mean/median/p25/p75/max/min + 未平仓浮动天数单列）；年换手率（卖出额/均资产，252 年化，风格漂移告警用不阻断）；`PeriodicSchedule` 再平衡日工具（每月末/每季度末/每N交易日，引擎零改动） |
| P3.4 波段轨回测指标 | ✅ 完成 | `engine/backtest/swing.py` 纯函数零 DB/LLM：FIFO round-trip 统计单元（SELL 与历史 BUY 批次配对，含费净额 CNY 可对账闭合；SELL 超持仓 fail-fast）；胜率（平保计入分母作非胜，breakeven_count 单列）/avg_win/avg_loss/payoff_ratio/profit_factor（无亏损单→None 不用 inf）/expectancy/平均成本收益率；持有期 mean/median/max/min + 超期笔数（`max_holding_days` 参数注入，严格大于计超期）；年换手率（策略重放净值注入，同 P3.3 公式）；Protocol 结构类型接收 lifecycle.StrategyTrade 同构成交，engine 不越层依赖；胜率/盈亏比仅展示不作生命周期门槛 |
| P3.7 前视偏差 AST linter | ✅ 完成 | `engine/backtest/lookahead.py` 纯 ast 零执行：LA001 负 shift(ERROR) / LA002 变量 shift(WARN) / LA003 bfill·backfill·fillna(method)·interpolate(limit_direction)(ERROR) / LA004 rolling(center=True)(ERROR) / LA005 变量+正偏移下标含切片上界(WARN) / LA006 import duckdb·sqlite3·requests·urllib·http·socket·aiohttp·httpx 与 open() 绕过 ctx(ERROR)；独立 API+入口强制（引擎不自动 lint），CLI `scripts/lint_strategy.py [--strict]`；strict 双档（P4 LLM 强制 strict，WARN 亦阻断）；行尾豁免 `# viq-lookahead: ignore LAxxx 理由（强制非空）`，未知规则/无理由反报 ERROR、死豁免 WARN；报告 JSON 可序列化（to_dict）；utf-8-sig 容忍 BOM |
| P4.1 LLM 通道（Agent 地基） | ✅ 完成 | `agents/llm/`：`base.py`（`Message` / `LLMResponse` / `LLMProvider` Protocol 统一补全接口）；`pricing.py`（读 config `llm.pricing` 单价算 `cost_cny`，**Decimal 保留 6 位小数**——按分舍入会把单次调用成本归零、失去核算意义，未配置模型留 `None` 不猜）；`openai_compat.py`（标准库 urllib 打 `{base_url}/chat/completions`，OpenAI 兼容协议，密钥经 `api_key_from_env` 从 `.env` 读且**绝不进盒带**；HTTP 传输可注入，测试离线验请求构造/响应解析）；`fake.py`（`FakeProvider` 测试零网络）；`cassette.py`（录制→重放，fingerprint=sha256(model+messages+参数) 不含凭据，重放未命中 **fail-fast** 杜绝偷发真实请求）；`runs.py`（`agent_run` 审计落库，`config_hash`+`code_version` 必填） |
| P4.2 Agent 输出契约 | ✅ 完成 | `agents/contracts/`：`base.py`（`BaseContract` PEP695 泛型骨架 + `ValidationResult`；`run_contract` 不合格带错误提示**重试 1 次**→仍不合格标 `DEGRADED`；`parse_json_response` 容忍 markdown 代码块与前后废话）；`valuation.py`/`dissent.py`（深度估值 / 反方两份 **dataclass 样板契约**，逐字段校验并回填强类型实例）；`persist.py`（**仅 SUCCESS 写 `agent_output`**，DEGRADED/FAILED 只留 `agent_run` 痕） |
| P4.3 Prompt 版本化 | ✅ 完成 | `agents/prompts/`：`registry.py`（`PromptRegistry`）+ `manifest.yaml`（`name → version/path`）+ 版本化文本 `texts/valuation.v1.md`·`dissent.v1.md`；`string.Template` 用 `$var` 占位、**避开 JSON 花括号冲突**；`version()` 产 `<name>.vN` 注入 `agent_run.prompt_version` 供回溯，已发布版本不删 |
| P4.4 投委会 Agent + 编排 | ✅ 完成（首跑通过） | `agents/value/`：`valuation_agent.py`（DuckDB 装配输入快照——财务按报告期倒序随 ann_date、最新估值截面/分位、分红、公告、同业对比、研报共识；`document_analysis` 解析结果入 prompt 与自身假设严格区分）→ 渲染版本化 prompt → `run_contract` → `persist_contract_run`；`dissent_agent.py`（估值方完整 JSON + 研报输入，攻击假设与共同乐观偏差）；`committee.py`（编排：估值方 DEGRADED/FAILED → 会话中止留痕不跑反方；反方失败 → 估值结论保留半程存档；双方 output_id 并列写 `committee_session`，`final_decision`/`decided_by` 留空待人工拍板）；`scripts/run_committee.py` CLI（无财务数据拒绝出结论 exit 2；真实响应按日归档盒带 `committee/<code>_<date>.json` 可离线复现）。**2026-10-16 真实首跑通过**（茅台，实录见下方引注） |
| P4.7 学习上下文注入 | ✅ 完成 | `agents/context.py` 统一装配器（只读不写，数据生产归 P5.3）：案例检索为**确定性纯函数打分**（code +4 / industry +2 / thesis_type +1，code+market 反查 `dim_stock.industry`；得分>0 才注入，按分值+时间降序取 `learning.case_retrieval_top_k`，全 0 分不硬塞无关案例）；评分卡取 `(agent_name, decision_type)` 最新 window_end 一行，**多维正交指标分开呈现**不合成单一分数，None 字段跳过；空库降级固定文案（明示有意为空非数据遗漏）；注入节统一标注「非输入数据，不得作为 data_refs 引用」防递归污染；prompt 升 **v2**（`$learning_context` 占位，v1 保留可回溯），注入文本随 `input_snapshot` 落库可还原；`created_at` SQL 层 CAST 去时区免 pytz 依赖；17 用例（纯函数 12 + 内存库装配 3 + FakeProvider agent 集成 2） |
| P4.8 研报解析 Agent | ✅ 完成（真实首跑通过） | `agents/shared/doc_parser.py`：document(pending) → 复用 P1.8 同一 `PdfExtractor` 缝隙按 file_path 重提全文（全文不落库只留 hash）→ **头 12000+尾 4000 截断**（封面盈利预测/评级 + 末尾风险点，`documents.parse_head_chars/parse_tail_chars` 可配）→ `doc_parse.v1` prompt（元数据供对照 + 外部观点红线标注 + 学习上下文统一注入）→ `DocAnalysisContract`（摘要/核心假设/forecasts 逐财年/评级/目标价/风险点，数值可空 fiscal_year 必填，禁止估算）→ SUCCESS 写 `document_analysis`（doc_id UNIQUE **一份只解析一次**，入口幂等 skipped）+ 拆 `analyst_forecast` 行（无 code 不拆/全空行不写）+ parse_status='parsed'；失败仅留 agent_run 痕且 parse_status='failed'；轻任务走 `llm.model_cheap`；批量 `parse_pending` 单份成败隔离；`scripts/parse_docs.py` CLI（--doc-id/--all/--limit，盒带归档 doc_parse/）；16 用例全离线。**2026-10-16 真实首跑**：3 份真实研报（澜起科技/安克创新/AI 行业）全部 parsed，4+5 行个股预测正确拆行、行业研报 0 行符合预期，盒带 `doc_parse/2026-10-16.json` |
| P4.5 波段选股 + 信号解读双 Agent | ✅ 完成（真实首跑通过） | **建议层**：不产 OrderIntent、不注册生命周期、不碰资金配额，产物落 `swing_signal`（迁移 `0005`，`(code,signal_date)` UNIQUE）status=pending 待 P6 人工确认。`agents/swing/screen.py`：**全市场代码初筛（零 LLM）**——dim_stock 全量（SH/SZ，BJ/HK v1 排除），三形态并集（breakout 收盘=20日新高且量比≥1.5 / pullback 距 MA20±3% 且 MA20 上行 / reversal 近5日 MA5 上穿 MA20 且处 60 日区间下 40%）+ 多头排列趋势加分；ST（name 含 ST）/停牌（最新交易日无 bar）/上市不足 60 日/近 20 日日均成交额<1 亿硬过滤；分值+流动性排序取 Top-N；特征计算为零 DB 纯函数可单测，单条窗口 SQL 取数。`picker_agent.py`（swing_pick.v1）→ `interpreter_agent.py`（signal_interpret.v1）均走 `llm.model_cheap` + P4.7 学习上下文（swing_pick / signal_interpret）；`contracts/swing.py`：signal_type 五枚举、参考位构造期绑定给定 K 线 [min low, max high] **越界即 ContractError**（机制防虚构价位）、多头 stop<entry<target。`pipeline.py`：选股 DEGRADED/FAILED 不跑解读不落库、signal_type=none 不落库、解读失败半程保留（signal_partial）、单候选异常隔离、同日重复跑幂等；`scripts/run_swing_pipeline.py`（--limit，盒带 swing/）；24 用例全离线。**2026-10-16 真实首跑**（生产库仅黄金数据集 2 票且 dim_stock 空 → 副本库补名并 as-of 2026-10-15：茅台 breakout、五粮液 reversal，参考位全部可回溯到真实 K 线价位；两票均主动声明无资金流/题材数据、win_rate_note 均为「暂无历史样本，胜率待积累」零编造）；首跑暴露 Flash 把契约拆成多个 JSON、数组字段给成字符串 → 提交前加固解读 prompt（单 JSON + 数组强约束 + 示例）后双 SUCCESS，盒带 `swing/swing_2026-10-15_review.json`。**待办**：正式全市场首跑需 QMT 同步全 A 日 K + dim_stock 名单；资金流/题材数据无表无适配器（prompt 已禁止编造） |
| P4.5.1 波段多策略架构 + 信号回测 | ✅ 完成 | `agents/swing/strategies/`：`ScreenStrategy` Protocol + `ParamSpec`（类型/默认值/范围/中文说明，即 P6 界面渲染契约，控制台经 `list_strategies()` 直接生成策略选择与参数表单）+ registry（新策略注册一行全链路可用）；`trend_momentum` 承载原三形态逻辑（逐行等价，原硬编码 0.4 下区间比例外置为 `lower_band_pct`，共 7 参数）；参数三层合并：schema 默认 ← YAML `swing_track.strategies.<id>` ← 调用覆盖（未知键/越界 fail-fast）。`screen.py` 策略化：`strategy_id` 缺省读 `swing_track.active_strategy`（界面可切换的后端地基），`as_of_date` SQL 截断支持历史重放；`swing_signal` 加 `strategy_id` 列（迁移 `0006`，NULL 容忍 + 回填）；pipeline/CLI `--strategy`/`--param k=v` 透传落库。`agents/swing/backtest.py` + `scripts/backtest_swing.py`：**零 LLM 不落库**，逐日 as-of 重放全市场初筛，双口径表现——固定持有 5/10/20 日收益（数据不足记 None 统计剔除）+ ATR 触位出场（stop=entry-2xATR14 / target=entry+3xATR14，同日触两侧保守止损先走，`max_holding_days` 未触及按 timeout 收盘）；`--grid k=v1,v2` 笛卡尔积参数网格逐组合独立重放评估；产出明细+汇总两份 CSV（utf-8-sig）与控制台汇总表；29 新用例 |
| P4.6 复盘归因 + 风控解读 | ✅ 完成（构造数据验证，真实归因待成交） | **数据地基**：迁移 `0007` fact_index_bar（指数日线独立分表，AkShare `index_zh_a_hist`）；AkShare 适配器声明 INDUSTRY_MAP / INDEX_BAR 两能力（东财行业板块~86 个逐块取成份回填 dim_stock.industry，节流防限流）；sync 脚本 `--industry`/`--index`/`--index-start` CLI（真实回填待东财网络恢复）。**定量层** `attribution/brinson.py` 纯函数：BHB 单期三效应（allocation/selection/interaction，恒等式 Σ=rp-rb 强校验权重和±1e-6 fail-fast），现金段（rp=0/wb=0/rb=基准总收益，现金拖累落 interaction）、区间新建仓剔除单列、指数收益仅作参考口径；`attribution/loader.py` 装配：期初持仓重放 trade_date<start + 期初现金（quota+cash_ledger）、复权收盘 backward 对齐、行业 NULL→未分类、基准 universe=全市场 A 股 as-of 期初市值加权自算（港股不入）、未持有段 rp=rb/基准外段 rb=rp 约定、账本空/基准空返 None + notes 透明。**定性层三 Agent**（契约+prompt v1+学习注入+落库）：归因解读（cheap，配置/选股效应分别解读禁编数字）、单持仓 thesis 复核（deep，intact/partially_broken/falsified × hold/reduce/exit，**falsified 必须 exit**，changes 只收事实陈述价格下跌不算证伪，区间事实=行情+ann_date 期间财报+公告 point-in-time 装配）、风控解读（cheap，normal/warning/critical 逐条解读，空事件 fail-fast 不调用）。**pipeline** 三段失败隔离：归因 unavailable/风控 empty/无 active thesis 跳过均为正常，单仓异常不连坐；`replay_holdings` 公共助手统一期初口径。CLI `scripts/review_portfolio.py`（--account/--start/--end/--benchmark，盒带 review_portfolio/ 按账户区间归档，任一段 failed 退 1）。58 新用例（brinson 18+loader 11+契约 16+Agent 8+pipeline 9 含 cassette 重放） |
| P4.6.1 总股本数据源 + 市值/PS 启用 | ✅ 完成 | fact_valuation.market_cap 全 NaN 修复（P4.6 基准 universe 数据前置收尾）。迁移 `0008` 新表 `fact_share_structure`（(code, change_date) 主键存股本变动历史，as-of 取数依赖保留历史）；AkShare 适配器声明 `SHARE_STRUCTURE` 能力：东财 datacenter-web `RPT_F10_EH_EQUITY`（F10 股本变动历史）标准库 urllib 直连全量分页（pageSize=500 约 650 页、必须带浏览器 UA、页间 0.2s 限流、单页重试穷尽 fail-fast 整体不落库），`DatacenterEquityClient` HTTP 可注入离线单测；`_map_share_structure` 纯函数：END_DATE 为股本时点（客观事实快照与东财 F10 展示口径一致无前视）、TOTAL_SHARES 空/缺日期/非 6 位代码丢弃、同 (code, change_date) 保留页序最后一条、`format="mixed"` 防同列双日期格式被 coerce 成 NaT；估值引擎 `compute(..., shares_df=...)` merge_asof backward 对齐 → **market_cap = close × 总股本**（总股本口径，用户确认）、**ps_ttm = market_cap / TTM 营收** 一并启用，股本缺省时两列 NaN 向后兼容（键 dtype 统一 ns）；sync 链路 `_recompute_valuation` 自动注入 fact_share_structure；`sync_share_structure()` 整表 DELETE+INSERT（仅保留 dim_stock 已有代码，拉取/入库失败均不动库内数据）+ `recompute_valuation_all()` 分块全量重算历史回填入口；CLI `sync_market.py --shares`（同步 → 自动分块重算 → market_cap 非空统计）；QMT 适配器补 Protocol 成员（按分工不支持即抛错）；契约新增 check_share_structure + FakeAdapter 样本；15 新用例（映射/分页/客户端 5 + 估值 as-of 5 + 同步与重算 4 + 契约 1） |
| P5.3 学习地基：结果打标 + 评分卡 + 案例尸检 | ✅ 完成 | 迁移 `0009` 重建 `outcome_label`（0002 占位表为空、无任何消费者，经用户确认以新脚本 DROP+CREATE 为拍板口径，只增不改纪律不破文件本身）：**标签口径**=T+N 固定时点（波段 T+5/10/20、价值 T+20/60/250）+ 触位事件（信号自带 entry/stop/target 参考位，先触者定胜负、同日触两侧保守 stop 先、收益按触位参考价保守估算）；`learning/labels.py` 打标纯函数（后复权口径、未到期/未触位返 None 不留痕、value_eval 附估值误差=（估值中枢-判定价）/判定价 正值=高估与 P4.7 评分卡口径一致）；`learning/labeler.py` 装配（(decision_type, decision_ref, label_kind, horizon) 主键锚点幂等，**成败信号都收**——评分卡度量 Agent 产出质量本身，与人工确认与否无关；decision_type 取值=agent_name（swing_pick/valuation），与 P4.7 注入侧查询对齐）；`learning/scorecard.py` 滚动 90 天窗口聚合（swing_pick：命中率 + T+5 误报率；valuation：error_mean/std 样本 <2 时 std None；窗口无样本不写保留历史行，按 window_end 幂等重写）；**案例尸检**：`contracts/postmortem.py`（LLM 只产观点字段 title/failure_mode/postmortem/tags，code/industry/thesis_type 事实字段装配侧带入防编造，failure_mode=NULL 表成功案例）+ `postmortem.v1` prompt + `shared/postmortem_agent.py`（cheap 模型 + P4.7 学习注入），**人工确认闸**：--draft 只产草稿文件，--confirm 才入 `case_library`（case_id=`decision_type:decision_ref` 幂等锚点，扫描自动排除已入库/已有草稿）；CLI `scripts/label_outcomes.py`（打标+评分卡一体，每日重跑安全）与 `scripts/postmortem.py`（--scan/--draft/--confirm，盒带 postmortem/）；31 新用例 |
| P5.1 Orchestrator 定时任务 | ✅ 完成 | **形态拍板（4 项用户确认）**：schtasks 每交易日触发单次 `scripts/run_daily.py` 跑完即退（无常驻进程）；方案 A 骨架+就绪任务挂接，盯价/止损扫描/价值初筛占位；告警 v1 文件+控制台；财报月每周六复盘。`alerting/router.py`：AlertRouter 分级路由（WARN/CRITICAL/FATAL 实时写 `<data_dir>/logs/alerts/alerts-YYYYMMDD.log`+控制台，INFO 缓冲 run 末尾聚合——config `info_aggregate_minutes` 为常驻形态口径，单次形态退化为末尾聚合；channels 邮件/webhook 键位预留不消费）。`orchestrator/dag.py`：PEP695 泛型 `Task[CtxT]`/`DagRunner[CtxT]`（Callable 逆变类型安全），注册序即拓扑序（依赖须先注册，重名/未知依赖 fail-fast），依赖非 ok 级联 skipped、异常收敛 failed 不中断独立分支、占位任务固定 skipped 不执行 run；`RunReport.exit_code` 任一 failed=1。`orchestrator/state.py`：P5.4 状态文件契约运行侧——`outputs/runs/<run_id>.json` 全量任务状态 + `live_state.json` 最近 run 指针，tmp+replace 原子写。`orchestrator/jobs.py`：子进程任务（sync_market/run_swing_pipeline/review_portfolio，超时外置+stdout 尾部 3 行摘要进状态文件）+ 进程内任务（quality 轻 SQL 口径=最新交易日覆盖率/重复键/陈旧度阈值 WARN，逐 code 全量质检归 P7 audit_db；label_outcomes 复用 P5.3 打标+评分卡）+ 3 占位（watchlist_watch→P5.2 / risk_scan→engine/risk 待立项 / value_screen→P4.4 欠账）；财报月（`orchestrator.earnings_months`）内周六追加 review_value/review_swing（区间=当季季首→当日，复用 P4.6 退出码语义 failed=1/不可用=0）+ value_screen 占位。CLI：全链 / `--task` 单任务补跑（忽略依赖）/ `--date` / `--list`；任务结果映射告警 failed→CRITICAL、ok→INFO 聚合；config 新增 `orchestrator` 节 8 键。22 新用例（dag 7 + state 2 + router 5 + jobs/装配 8），真实冒烟 `--list` 平日 6 任务/财报月周六 9 任务/非法任务名 exit 2 |
| P5.2 狩猎清单状态机 | ✅ 完成 | **三态 v1**：watching / triggered / expired（ready 字段保留不使用）。`engine/watchlist/state.py` 纯函数零依赖：`is_expired`（last_review 缺失视为过期、边界严格大于 ttl_days）、`adjust_target_price`（**按复权因子动态调整目标价到最新交易日基准**——除息日未复权 close 跳变不误触发，无复权因子返 None）、`is_triggered`（close ≤ adjusted_target）、`has_new_report`（ann_date > last_review_date）。`engine/watchlist/scan.py` 批量扫描：LATERAL join 取 `created_at` 当日或之前最近交易日的 adj_factor 作 target_day 基准（不加列、用 created_at 反查、无新迁移）；**判定优先级 expired > triggered**（研究作废时即使价格到位也不触发，强制重新投委会）；新财报→自动 expired + WARN 告警（用户拍板「自动复核」= 强制作废）；显式 commit/rollback 不用 `with conn` 避免关闭共享连接。`orchestrator/jobs.py`：`watchlist_watch` 占位替换为进程内真实任务（依赖 sync_daily，triggered/expired 均 WARN）。`scripts/watchlist.py` 人工管理 CLI（P6 前 CRUD 入口）：add/list/expire/reverify，list 展示距过期天数与最新价 gap%。16 新用例（纯函数 6 + 扫描四态 6：触发/保持/TTL过期/新财报过期/无行情跳过/幂等 + CLI 4） |
| 止损扫描 engine/risk | ✅ 完成 | **价值轨 watch 档 3 事件**（配置 `watch_events` 驱动，仅告警+thesis 复核建议，绝不因价格下跌触发）：`roic_below_min_2y`（**roe 代理 ROIC**——QMT 财务 5 表无 roic 字段、有息负债拆分缺失致自算退化，roe 为落库最接近代理，后续 AkShare 乐咕源补 ROIC 后切换口径，配置键名保留事件语义）、`fcf_negative_2y`（连续 2 个年度报告 FCF<0）、`dividend_cut`（当年每股分红<上年，任一年无记录不触发避免中途误报）；年度报告按 `ann_date ≤ today` 防前视、任一期缺失宁缺勿错不触发。**波段轨 3 事件**：`price_stop_loss`（对成本线 -8%）、`max_drawdown`（对持仓期间峰值 -15%，peak=开仓以来最高收盘，与固定止损互补）、`max_holding_days`（严格大于口径与 P3.4 一致）。落库：risk_event severity=WARN/decision=ALERT，config_hash+code_version 必填与 ExecutionService 同口径；**确定性 event_id 幂等**（rule_key|account|code|position_id，同持仓同规则只告警一次，重复扫描 suppressed 计数呈现不重复告警）；显式 commit 不用 `with conn`。只告警不自动下单（用户拍板，v1 模拟盘人工确认卖出）；exit_events（暴雷/证伪）属公告信号+LLM/人工定性判断，不适用规则扫描。`jobs.py` risk_scan 占位激活（依赖 sync_daily）。config 补 `value_track.stop_loss.roe_min: 0.12`。16 新用例（纯函数 6 + 装配 9：roe/fcf/dividend 触发与不触发、波段三事件齐触发、健康不触发、幂等、空持仓、无行情跳过、DAG 装配断言） |
| P4.4.1 价值初筛（P4.4 三件套补全） | ✅ 完成（真实首跑通过） | `agents/value/screen.py` **零 LLM 排除法漏斗**（与波段初筛同构）：第 0 步能力圈一票否决（行业白/黑名单+个股黑名单，白名单空=不启用）→ 硬剔除（ST/非 SH·SZ/最新交易日无 bar）→ 质量漏斗（fact_financial **年度报告**按 `ann_date ≤ 筛选日` 防前视、宁缺勿错：roe≥`roe_min` 连续 `roe_sustain_years` 年、净利连续盈利 `min_consecutive_profit_years` 年、`require_positive_fcf` 窗口 fcf 全正、最新年度 `max_debt_ratio`）→ 估值闸门（`pe_percentile ≤ pe_percentile_max: 0.5`，分位缺失=历史不足不过闸）→ 质量排序（roe 均值主键、fcf 均值并列键，**全量入表不截断**）；roe 代理 ROIC 与止损扫描同口径（config roic_min/roic_sustain_years 保留不消费，ROIC 数据源补齐后切换）。迁移 `0010` 新表 `value_candidate`（(screen_date, code) UNIQUE，质量+估值快照+gate_passed+quality_score+config_hash，每周快照追加不做状态机）；截面取「≤ 筛选日的最新交易日」适配周末初筛；SQL 一次拉全市场年度报告内存分组（避免 5577 次逐只查询）；同日先删后插幂等。`orchestrator/jobs.py`：`value_screen` 占位替换为真实任务（**每周六固定** `orchestrator.value_screen_weekday`，不再挂财报月周六分支；依赖 quality；INFO 告警含漏斗各级存活数），占位机制清零。`scripts/screen_value.py` CLI（--date/--top/--all，退出码 0/1/2 与全平台一致）；人工链路=挑候选→`run_committee.py --code`→`watchlist.py add` 三段各有入口。config 补 `value_track.screening.pe_percentile_max` + `orchestrator.value_screen_weekday: 6`。17 新用例（漏斗纯函数 5 + 端到端/幂等/空库/缺省日期/黑名单 5 + DAG 装配 2 + CLI 复用真实冒烟）。**2026-10-18 真实首跑**：5216 universe → 硬剔除 5022（ST 194）→ 质量达标 **215**（估值闸门通过 **171**），榜首苏泊尔（ROE 均 35.7%/9 年、PE 分位 37%）、特宝生物、阳光电源等合理；幂等复跑 215 行不变 |
| P5.4 / P6 / P7 | ⏳ 待办 | 5.4 状态文件契约控制台侧 → P6 Web 控制台 → P7 audit_db |

> **P4.4 首跑实录（2026-10-16，session aeb658a1）**：茅台投委会端到端跑通（QMT 实测数据 + 真实 LLM）。估值方三法交叉（DCF 1322 / PE 法 1171 / PB-ROE 1206），区间 1171–1450，现价 1317.5 → 安全边际 -12.5%，不满足买入闸门，置信度 0.55；反方 18 条攻击、3 条致命（FCF/NI=1.43 异常、justified PB 3.57→6.0 主观上调、单季净利 -6.9% 与正增长假设矛盾）。双方结论全带 data_refs，盒带归档可复现。LLM 自主标注数据缺口：**无分红记录、无公告摘要、无研报输入**——其中**无分红记录已于 2026-10-16 修复**（AkShare 补 A股分红，`dv_ratio` 由 NaN 转有效）；fact_disclosure（A股/港股公告源）与研报库（待 P1.8 真实文档）仍待补。首次尝试因 V4-Pro 深度思考 >120s 超时 FAILED，`timeout_seconds` 调至 300 后成功。

**最近一次验证**：`scripts/check.ps1` 全绿——ruff 通过、mypy strict **111** 文件无错、import-linter 2 kept / 0 broken、pytest **658 passed, 17 deselected**。**P4.4.1 价值初筛完成（2026-10-18，真实首跑通过）**：零 LLM 排除法漏斗（能力圈→硬剔除→质量→估值闸门→质量排序，ann_date 防前视、宁缺勿错、全量入表不截断）；迁移 0010 value_candidate 周快照；每周六 `orchestrator.value_screen_weekday` 固定执行；`value_screen` 占位激活，**P5.1 全部占位清零**；CLI screen_value.py；真实首跑 5216→5022→215（闸门通过 171），幂等复跑行数不变。**止损扫描 engine/risk 完成（2026-10-18）**：价值轨 watch 档 3 事件（roe 代理 ROIC / fcf_negative_2y / dividend_cut，年度报告 ann_date 防前视、宁缺勿错）+ 波段轨 3 事件（对成本 -8% / 对持仓峰值 -15% / 持有期上限）；risk_event WARN/ALERT 落库、确定性 event_id 幂等（重复扫描 suppressed 计数不重复告警）；只告警不自动下单；`risk_scan` 占位已激活；16 新用例全过。**P5.2 狩猎清单状态机完成（2026-10-18）**：三态 watching/triggered/expired（ready 保留不用）；盯价用复权因子动态调整 target_buy_price 避免除息误触发；新财报自动 expired 强制复核；判定优先级 expired>triggered；`watchlist_watch` 占位已激活；`scripts/watchlist.py` CLI（add/list/expire/reverify）；16 新用例全过。**P5.1 Orchestrator 完成（2026-10-18）**：22 新用例（dag 拓扑/级联 skipped/占位短路/重名与未注册 fail-fast/退出码 7 例 + state 原子写 2 例 + router 缓冲聚合与实时分级 5 例 + jobs 装配与进程内任务 8 例）；真实冒烟 `run_daily.py --list` 周日 6 任务、财报月周六 9 任务（+review_value/review_swing/value_screen）、非法任务名 exit 2。**全市场财务+分红回填完成（2026-10-18，后台串行约 2.5 小时 exit 0）**：fact_dividend **5,126 只 / 63,162 行**（1992-03-20 ~ 2026-10-26，AkShare 巨潮 0 警告）；fact_financial **5,577 只 / 183,986 行**（2017-03-31 ~ 2026-09-30，QMT 近 10 年口径 154.6 分钟，仅 910000~910005 非股票代码段 6 只失败属预期）；fact_valuation 133.4 万行非空率——pe_ttm **99.7%**（1,330,116 行，财务回填后从千余行修复至全量）、ps_ttm **99.9%**、dv_ratio **92.0%**（空值为无分红记录标的属预期）、market_cap **99.92%**。**P5.3 学习地基完成（2026-10-17）**：打标纯函数 12 用例（T+N 到期/未到期/负收益/参数校验、触位先 stop 先 target/同日两侧保守 stop/未触位、value_eval 估值误差符号）+ 装配套 8 用例（内存库端到端：四标签落库与重跑幂等、触位判定日、无行情跳过告警、valuation T+20 到期 T+60/250 观察中、评分卡窗口内外样本/幂等重写）+ 尸检 11 用例（契约校验、装配、候选扫描排除已入库/已有草稿、case_id 幂等锚点）。**P4.6 复盘归因完成（2026-10-17，构造数据端到端验证）**：BHB 定量层 18 用例三效应手算对拍 + 装配层 11 用例（茅台 15 万持仓 wp=0.1875/食品基准 wb=0.2 等手算核对）+ 三 Agent 契约 16 + 入口 8 + pipeline 9（含 cassette 录制→重放零网络）；`review_portfolio.py` 内存库全链路冒烟三段 SUCCESS、报告完整。**真实库归因数据（2026-10-17 回填完成）**：① dim_stock.industry **5577/5612** 已回填（东财 EM2016 三级行业，如「食品饮料-饮料-白酒」；push2 板块接口仍被本机阻断，改走 datacenter-web F10 接口分页拉全市场回填；未命中 35 只均为北交所债券/指数非股票，无行业属正常，且无行情不会进基准 universe）；② fact_index_bar **000300 已回填 4074 行（2010-01-04 ~ 2026-10-16）**（push2his 直连绕过 push2 代码映射阻断，复用适配器映射与增量入库全链路；首尾对拍 3535.23/4170.61 合理）；③ fact_valuation.market_cap **已回填（P4.6.1，2026-10-17 真实库落库）**：fact_share_structure **161,535 行覆盖 5,577 只**（变动时点 1979-02-23 ~ 2026-10-28，东财源含已退市/历史代码的 147,000 行因不在 dim_stock 跳过属预期）；估值全量重算 5,585 只，**market_cap 非空 1,332,761/1,333,876 行（99.92%）**——空值仅 9 只标的 2025~2026 年共 1,115 行，系上市首日早于首条股本变动记录、merge_asof backward 无前值可取，属预期；ps_ttm 非空 1,454 行与 pe_ttm 完全一致（受限于 fact_financial 仅 44 行/2 只样本，全市场财务同步为既有待办，非本次问题）；抽查茅台 2026-10-16：收盘 1317.5 × 总股本 ≈ **市值 1.647 万亿**、PE_TTM 20.25、PS_TTM 9.51，数值合理；④ 模拟账本产生真实成交后做真实归因人工评审（P4.5.2 成交后补）。**P4.5.1 真实库回测冒烟通过（2026-10-17）**：`backtest_swing.py --start 2026-07-01 --end 2026-09-15` 逐日 as-of 重放全市场，基线 275 信号双口径统计（固定 5/10/20 日 + ATR 触位：stop 190 / target 46 / timeout 39，形态分组齐全）落 `data/backtest/` 明细+汇总 CSV；`--grid volume_ratio_min=1.5,2.0` 两组合独立重放对比正常（该区间三形态口径整体亏损，如实呈现供参数调优参考）。**P4.5 正式全市场首跑通过（2026-10-16）**：QMT 全市场同步（5612 只名单含名称 / 133 万行日 K，~2.7 只/s 38.7 分钟，ST 类 201 只剔除）→ 零 LLM 初筛出 5 候选（华电新能三形态并集 + 4 只放量突破）→ 双 Agent 5/5 全 SUCCESS 落 `swing_signal`（pending），参考位全部落在真实 K 线区间、失效条件客观可验证、win_rate_note 均「暂无历史样本待积累」零编造，盒带 swing/swing_2026-10-16.json；全市场同步脚本 scripts/sync_market.py（含运维日志：独立文件+控制台双输出/分块进度/分级告警），并修复 QMT fetch_stock_list 名称 NULL 撞 NOT NULL 契约的潜伏 bug。**P4.8 真实首跑通过**：3 份真实研报（2 公司 + 1 行业）全部 parsed，0 失败，盒带归档 doc_parse/2026-10-16.json。**P4.4 真实首跑成功**（茅台投委会，QMT 实测数据 + 真实 LLM，双方 SUCCESS，session aeb658a1）。

---

## 三、运行环境

- 操作系统：Windows（PowerShell 5）
- Python：3.12.10；虚拟环境：`D:\Work\VIQTrader\.venv`
- 数据库：DuckDB，默认文件 `D:\Work\VIQTrader\data\viqtrader.duckdb`

**关键依赖版本**（完整锁定清单见 [requirements.txt](requirements.txt)）：

| 包 | 版本 |
|---|---|
| pandas | 3.0.6（大版本 3，行为有变更，契约测试已在 pandas 3 下通过） |
| akshare | 1.19.1 |
| duckdb | 1.5.6 |
| numpy | 2.5.4 |
| pytest / mypy / ruff / import-linter | 9.1.1 / 2.4.0 / 0.17.0 / 2.15 |

**依赖双文件约定**：

- [pyproject.toml](pyproject.toml) —— 抽象依赖声明（兼容范围，如 `pandas>=2.2`），供安装解析；
- [requirements.txt](requirements.txt) —— 锁定精确版本的快照，供复现环境。
- 双文件必须同步：**增删/升级依赖后重新生成 requirements.txt**：

```powershell
.venv\Scripts\python.exe -m pip freeze > requirements.txt
# 然后删除末尾指向本项目自身的 editable 行：-e d:\work\viqtrader
```

**一键检查**（等价于提交前 CI）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check.ps1
```

> 该脚本逐步校验退出码，任一步失败即以非零码退出——PowerShell 5 不会因原生命令失败抛异常，早期版本曾因此「假绿」，已修正。

**测试分层**：默认只跑单元/契约测试（`-m 'not integration'`）；依赖真实网络或 QMT 终端的用例需在模块顶部声明 `pytestmark = pytest.mark.integration`，手动执行：

```powershell
pytest -m integration
```

---

## 四、关键文件

```
D:\Work\VIQTrader\
├── pyproject.toml                      # 工程与工具配置：ruff/mypy/pytest/import-linter 契约
├── requirements.txt                    # 依赖锁定快照（pip freeze 生成）
├── .pre-commit-config.yaml             # 提交前 ruff + 文件卫生钩子
├── .env.example                        # 机密模板（TUSHARE_TOKEN / QMT_INSTALL_DIR 等，真实 .env 不入库）
├── doc/                                # 权威设计文档（立项期产物，随开发同步修订）
│   ├── 股票分析交易平台设计方案.md      # v1 设计方案：架构/数据模型/Agent 契约/双轨流程/风控/配置/学习/归因
│   └── 实现计划与任务拆分.md            # P0~P7 任务拆分与各任务验证方式
├── config/
│   └── default.yaml                    # 全部阈值基线：双轨配额/能力圈/筛选闸门/风控/生命周期/港股通税率/费用/告警/数据源
├── scripts/
│   ├── check.ps1                       # 一键检查：ruff → mypy → import-linter → pytest
│   └── backtest_swing.py               # P4.5.1 波段信号回测 CLI（--start/--end/--strategy/--param/--grid，CSV 落 data/backtest/）
├── src/viqtrader/                      # 分层架构（下层禁止 import 上层，import-linter 强制）
│   ├── config/loader.py                # 三层配置加载 + 热加载 + 快照 hash
│   ├── store/
│   │   ├── db.py                        # DuckDB 连接
│   │   ├── migrate.py                   # 迁移执行器：只增不改 + checksum 强校验 + 幂等
│   │   ├── config_snapshot.py           # 参数快照去重入库
│   │   └── migrations/
│   │       ├── 0001_meta.sql            # config_snapshot（schema_migrations 由代码自举）
│   │       ├── 0002_domain_schema.sql   # 25 张领域表
│   │       └── 0003_trading_rules_and_ledger.sql  # order_intent/market_status/rule_check_log + board/frozen_qty/intent_id
│   │       ├── 0004_strategy_lifecycle.sql  # P3.6 生命周期注册表/KPI/退役留痕
│   │       ├── 0005_swing_signal.sql     # P4.5 波段双 Agent 建议表（status=pending 人工确认闸，code+signal_date UNIQUE）
│   │       └── 0006_swing_strategy.sql   # P4.5.1 swing_signal.strategy_id 加列（NULL 容忍 + 回填 trend_momentum）
│   ├── data/
│   │   ├── base.py                      # DataAdapter Protocol + Capability + 逐表列契约（SCHEMAS）
│   │   ├── contract.py                  # 契约检查套件（结构/取值/行为）+ ContractViolation
│   │   ├── adapters/                    # akshare.py / qmt.py（均已交付）
│   │   ├── sync.py / quality.py         # 增量同步编排器 / 数据质量校验（P1.6）
│   │   ├── valuation.py                 # 自算估值引擎纯函数（P1.5）
│   │   ├── normalizer/                  # 标准化、复权、质检
│   │   ├── docs/                         # 研报 PDF 入库管线（P1.8）
│   │   └── schema/                       # 数据层内部 schema 辅助
│   ├── engine/                          # 确定性引擎（零 LLM，金额全程 Decimal）
│   │   ├── types.py                     # OrderIntent/RuleContext/TradeLog/FeeBreakdown/InsufficientCashAtFill
│   │   ├── market_status.py             # 涨跌停/停牌/ST/港股通名单快照预计算（板块差异化涨跌幅）
│   │   ├── fees.py                      # A股+港股通费用模型，统一 CNY 口径
│   │   ├── rules/                       # 三阶段规则管线：market.py（6 条市场规则）/ account.py（4 条账户规则）/ base.py（Protocol+引擎）
│   │   ├── ledger/engine.py             # LedgerEngine：FILLED 才扣款+扣款前重检 / T+1 冻结解冻 / 分红入账 / 资金视图
│   │   ├── execution/                   # gateway.py（Simulated/DryRun）/ context.py / service.py（编排+留痕）
│   │   └── backtest/                    # P3.1：engine.py / context.py（前视防线）/ strategy.py / result.py（净值+通用指标）/ P3.3 value.py（XIRR/超额/FIFO持有期/年换手率/PeriodicSchedule）/ P3.4 swing.py（FIFO round-trip 胜率/盈亏比/超期/换手）/ P3.7 lookahead.py（AST 前视 linter LA001-LA006/strict/豁免留痕）
│   ├── agents/                          # LLM Agent 层（P4）
│   │   ├── llm/                         # P4.1：base（Message/LLMResponse/Provider Protocol）/ pricing（Decimal 计价）/ openai_compat（urllib）/ fake / cassette（盒带录制重放）/ runs（agent_run 审计）
│   │   ├── contracts/                   # P4.2：base（ValidationResult/run_contract 重试降级）/ valuation.py · dissent.py（dataclass 样板契约）/ swing.py（P4.5 双契约：参考位 K 线区间绑定越界即拒）/ persist.py（DEGRADED 不写 agent_output）
│   │   ├── prompts/                     # P4.3：registry.py + manifest.yaml + texts/valuation·dissent（版本化 prompt）/ P4.5 swing_pick.v1.md · signal_interpret.v1.md / P4.8 doc_parse.v1.md
│   │   ├── value/                       # P4.4：valuation_agent（输入装配+估值 Agent）/ dissent_agent（反方）/ committee（编排+session 落库）
│   │   ├── swing/                       # P4.5：screen.py（全市场零 LLM 初筛，策略化入口+窗口 SQL）/ picker_agent · interpreter_agent / pipeline（双 Agent 编排落 swing_signal，建议层不产 Intent）；P4.5.1：strategies/（base 协议+ParamSpec / registry 注册表 / trend_momentum 三形态并集）+ backtest.py（as-of 重放 + 双口径评估 + 参数网格 + CSV）
│   │   └── shared/                      # P4.8：doc_parser（研报解析 Agent）
│   ├── orchestrator/                    # 任务编排与投委会会话
│   ├── learning/ · attribution/ · alerting/   # 自我学习 / Brinson 归因 / 告警分级
│   │   └── lifecycle/                   # P3.5 walk_forward.py（IS/OOS 滚动验证+门禁）/ P3.6 registry.py（状态机）+ kpi.py + equity.py（净值重放）
│   └── web/                             # Web 控制台（只读 outputs/ 状态文件，与引擎解耦）
└── tests/
    ├── unit/                            # 配置/迁移/建表核对/同步质检/估值/market_status/规则管线/账本/网关+执行服务/回测引擎/LLM 通道+盒带（test_llm_provider·test_llm_cassette）/契约（test_agent_contracts）/prompt（test_prompts）
    ├── contract/                        # DataAdapter 契约套件 + FakeAdapter（假实现通过即合规）
    └── integration/                     # 真实外部依赖（网络/QMT），标记 integration 默认跳过
```

---

## 五、下一步

**当前推进：P5.1 Orchestrator 已完成，待本机提交**

1. **P5 编排剩余**：~~5.1 Orchestrator 定时任务~~ ✅ 已完成（schtasks 单次形态 DAG：同步→质检→波段信号→打标 + 3 占位 + 财报月周六复盘；告警 v1 + 状态文件运行侧）；5.2 狩猎清单状态机（激活 watchlist_watch 占位）；5.4 状态文件契约控制台侧（运行侧已由 5.1 落地）。~~5.3 学习地基~~ ✅ 已完成（T+N/触位打标 + 90 天滚动评分卡 + 尸检人工确认闸）。**衍生欠账两笔（5.1 盘点暴露，待立项）**：止损扫描 `engine/risk` 持仓巡检（激活 risk_scan 占位）；价值初筛（激活 value_screen 占位，P4.4 三件套实际只交付估值+反方）。
2. **全市场财务+分红同步（✅ 已完成并验证 2026-10-18）**：`sync_market.py --dividend --financial` 全量回填（分红 AkShare 巨潮 + 财务 QMT 近 10 年，`financial_lookback_years` 3→10 激活沉睡配置，新增 `--financial`/`--dividend` 模式与公共分块骨架）；覆盖率见「最近一次验证」，价值轨 ROIC 持续年限筛选与全市场 PE/PS 数据依赖已闭合，另起提交。
3. **P4.6 真实评审（数据依赖）**：✅ 行业（EM2016 三级，5577/5612）与沪深300日线（4074 行，2010 起）已于 2026-10-17 经 datacenter/push2his 直连回填（push2 阻断绕过方案）；✅ 总股本已接入（P4.6.1，fact_share_structure + market_cap/ps_ttm 全量重算）；剩余——待模拟盘成交后做首次真实组合复盘人工评审。
4. **P6 Web 控制台**：FastAPI 单框架；人工确认闸（任何下单意图必须控制台确认）、波段策略选择/参数表单（消费 `list_strategies()`）、P4.6 复盘报告页（三段结果只读展示）。
5. **P7 库况体检 audit_db（设计已确认 2026-10-18，待实现）**：v1 = financial/valuation/勾稽三核心表 + 抽样打印 + `--json` 输出；阈值外置 config `audit:` 节，OK/WARN/FAIL 三态判定 + `--strict` 退出码；落点 `store/audit.py` + `scripts/audit_db.py`。

**待用户提供的输入**：

- QMT 终端可用时段：✅ 2026-10-16 已实测同步（茅台/五粮液 3 年日线+财务入库，估值引擎自算正常）；后续补数仍需终端在线；
- config 中全部阈值真实值（目前 `default.yaml` 中标 `[占位]` 的项均为占位值，联调前给出即可）；
- ~~LLM 选型与 API key~~ ✅ 已提供（ModelScope + DeepSeek 系列）；仅 `llm.pricing` 单价待补（厂商价目表 CNY/1K token，补前 `cost_cny` 留空）。

---

## 六、安全边界（硬约束）

**架构与审计**

1. **引擎层零 LLM**——回测、风控规则执行、订单撮合、账本记账、Brinson 归因、Walk-Forward 必须是纯代码，确定性、可回放。
2. **风控独立且先于交易**——任何交易意图必须先过风控规则引擎。
3. **迁移只增不改**——已发布的 migration 脚本禁止修改；新增表走新编号（如 `0003_*.sql`），旧脚本改动会被 checksum 校验拒绝。
4. **审计留痕**——`risk_event` 与 `agent_run` 必须携带 `config_hash` + `code_version`，保证历史决策可按当时参数与代码版本复盘。
5. **双轨红线**——`position` 表对 `(account_id, code)` 建 UNIQUE 约束，同一标的在价值轨与波段轨必须是两条独立记录。
6. **参数版本化**——所有阈值外置 YAML 且支持快照；改参不影响已存档的历史快照。
7. **研报治理**——外部研报内容一律标注为「外部观点」，与原始财务数据严格区分；Agent 引用时须注明来源机构与日期。
8. **状态文件契约**——引擎只写 `outputs/` 状态文件，Web 控制台只读，避免进程间耦合（import-linter 强制）。

**操作纪律**

9. **git 提交需明确指示**——仅在用户明确要求时创建 commit；不擅自提交。
10. **v1 不碰实盘**——`platform.env` 保持 `sim`，不得改为 `live`；实盘网关仅预留接口。
11. **机密不入库**——API token 只放 `.env`（已 gitignore），仓库内只保留 `.env.example`。

**环境注意**

12. **沙箱范围**——`D:\Work\VIQTrader` 位于命令沙箱可写范围之外，用 Shell 操作该目录时需显式关闭沙箱（`dangerouslyDisableSandbox`）；Write/Edit 工具不受此限。
13. **数据源易错点**（已实测，勿重复踩坑）：
    - QMT 财务必须用 `download_financial_data2` **异步版** + `report_type='report_time'`，且 5 张表全拉（缺 `PershareIndex` 则无 ROE/EPS/毛利率）；
    - QMT **不覆盖港股基本面**，港股财务必须走第三方源；
    - 免费数据源财报易引入前视偏差，须优先使用带公告日（`ann_date`）的时点数据；
    - **AkShare 港股财报无公告日源**（东财三表与指标接口均无 `ann_date`），当前如实留空——港股财务的前视偏差防控因此弱于 A股，相关回测结论需标注精度受限；
    - **Tushare token 实测边界（2026-10-16，仅验证未接入）**：token 有效，可用接口仅 `trade_cal` / `stock_basic` / `dividend`（A股分红）/ `hk_daily`（港股日线）；平台真正缺口 `hk_basic`·`hk_tradecal`·`hk_income`·`hk_balancesheet`·`hk_cashflow`·`hk_fina_indicator`·`fx_daily`·`anns_d` 全部 **`code=40203` 无权限**（积分档位偏低）。且 `dividend` 有**每小时 1 次**频率限制，逐票同步不可行。故 **Tushare 暂不接入**（`tushare.enabled: false`），A股分红缺口改由 AkShare `stock_dividend_cninfo`（无频率限制、含派息日）填补；Tushare 作为升级积分后拿港股财务/公告/汇率 时的候选源预留。
14. **DuckDB 迁移限制**（0003 已踩）：`ALTER TABLE ADD COLUMN` 不支持 `NOT NULL DEFAULT`/外键约束——新列允许 NULL 由迁移回填 + 引擎层 `COALESCE`，FK 引用关系由应用层写入前校验。
15. **沙箱文件视图**（P2 已踩）：沙箱内偶发读到旧文件内容导致「编辑看似成功但磁盘未变」，验证门禁一律以 `dangerouslyDisableSandbox` 非沙箱模式重跑为准。

---

## 维护约定

- 每完成一个任务 → 更新「二、当前进度」表格与「五、下一步」，并跑一次 `scripts/check.ps1` 确认全绿。
- 增删或升级依赖 → 重新生成 `requirements.txt`。
- 阈值默认值变动 → 走 config 快照机制，不改业务代码。
