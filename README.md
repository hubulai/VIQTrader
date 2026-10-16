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
| P1.3 QMT 适配器 | ✅ 完成 | `data/adapters/qmt.py`：xtquant 实测联调通过——`get_market_data_ex` 日线（关键字传 `count=-1`，行索引兼容毫秒 epoch/YYYYMMDD）、`download_financial_data2` 财务 5 表逐表回调（`m_timetag`/`m_anntime` 列式结构，英文字段映射含 ROE/EPS/毛利率，无 ROA 留空）；板块 `'沪深京A股'`（含 BJ），无港股通板块→名单归 AkShare |
| P1.4 AkShare 适配器 | ✅ 完成 | `data/adapters/akshare.py`：声明 4 项能力（FINANCIAL / DIVIDEND / FX_RATE / STOCK_LIST），每项均实测选源；DISCLOSURE（实测无港股公告源）、HK_CONNECT_HISTORY（无名单变动流水源）、DAILY_BAR（属 QMT 分工）不声明，调用即抛 `UnsupportedCapabilityError`。附离线映射单测 17 例 + 联网集成测试 9 例 |
| P1.5 自算估值引擎 | ✅ 完成 | `valuation.py` 纯函数（零状态零依赖）：PE/PB/PS/股息率/市值/PE 分位；backward 对齐公告日防前视、分母 0 返 NaN、PE 分位 250 日窗口样本不足 60 留空、港股通股息率按 0.2 保守税率 |
| P1.6 增量同步 + 质检 | ✅ 完成 | `data/sync.py` 增量同步编排器（起点自适应 + DELETE+INSERT 幂等 + 缺口检测 + 前视偏差硬过滤）；`data/quality.py` 质量校验 |
| P1.7 黄金数据集冻结 | ✅ 完成 | 8 只代表股 3 年日线基准 parquet + MANIFEST + 回归测试；东财 push2 域不可达改用新浪源 |
| P1.8 研报入库管线 | ✅ 完成 | `data/docs/ingestor.py`：pdfplumber 提取（Protocol 缝隙可注入假实现）、全文 SHA256 内容去重（改名仍判重）、文件名元数据正则（机构/代码/周期/日期，先剥日期再匹配代码防误识别） |
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
| P3.7/P4~P7 | ⏳ 待办 | 前视 AST linter → Agent 层（可并行）→ 编排+学习地基 → Web 控制台 |

**最近一次验证**：`scripts/check.ps1` 全绿——ruff 通过、mypy strict 61 文件无错、import-linter 2 kept / 0 broken、pytest **310 passed, 16 deselected**（P3.4 新增 10 用例：单笔胜负平保/加仓部分平仓 FIFO 拆分（pnl+1000 与 -500、持有 20/15 天、超期 1 笔）/无样本全 None/年换手率三态/卖超持仓 ValueError/多标的独立批次/乱序确定性/Decimal TradeLog 兼容）。

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
│   └── check.ps1                       # 一键检查：ruff → mypy → import-linter → pytest
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
│   │   └── backtest/                    # P3.1：engine.py / context.py（前视防线）/ strategy.py / result.py（净值+通用指标）/ P3.3 value.py（XIRR/超额/FIFO持有期/年换手率/PeriodicSchedule）/ P3.4 swing.py（FIFO round-trip 胜率/盈亏比/超期/换手）
│   ├── agents/                          # LLM Agent：value（初筛/估值/反方）、swing、shared、llm、contracts、prompts
│   ├── orchestrator/                    # 任务编排与投委会会话
│   ├── learning/ · attribution/ · alerting/   # 自我学习 / Brinson 归因 / 告警分级
│   │   └── lifecycle/                   # P3.5 walk_forward.py（IS/OOS 滚动验证+门禁）/ P3.6 registry.py（状态机）+ kpi.py + equity.py（净值重放）
│   └── web/                             # Web 控制台（只读 outputs/ 状态文件，与引擎解耦）
└── tests/
    ├── unit/                            # 配置/迁移/建表核对/同步质检/估值/market_status/规则管线/账本/网关+执行服务/回测引擎
    ├── contract/                        # DataAdapter 契约套件 + FakeAdapter（假实现通过即合规）
    └── integration/                     # 真实外部依赖（网络/QMT），标记 integration 默认跳过
```

---

## 五、下一步

**当前推进：P3 回测剩余任务（可与 P4 Agent 层并行）**

1. **P3.7 前视偏差 AST linter**：静态检测 `shift(-N)`、未来索引等模式，P4 LLM 生成策略时强制启用（可后置到 P4 前）。
2. **P4 Agent 层**（可与 P3 并行）：估值 Agent / 反方 Agent 契约 + 投委会交叉验证，复用 P1.8 研报库与 P1.5 估值引擎。

**待用户提供的输入**：

- QMT 终端可用时段（P3 回测若需补真实日线数据时）；
- config 中全部阈值真实值（目前 `default.yaml` 中标 `[占位]` 的项均为占位值，P3 联调前给出即可）；
- LLM 选型与 API key（P4 前给出即可）。

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
    - **AkShare 港股财报无公告日源**（东财三表与指标接口均无 `ann_date`），当前如实留空——港股财务的前视偏差防控因此弱于 A股，相关回测结论需标注精度受限。
14. **DuckDB 迁移限制**（0003 已踩）：`ALTER TABLE ADD COLUMN` 不支持 `NOT NULL DEFAULT`/外键约束——新列允许 NULL 由迁移回填 + 引擎层 `COALESCE`，FK 引用关系由应用层写入前校验。
15. **沙箱文件视图**（P2 已踩）：沙箱内偶发读到旧文件内容导致「编辑看似成功但磁盘未变」，验证门禁一律以 `dangerouslyDisableSandbox` 非沙箱模式重跑为准。

---

## 维护约定

- 每完成一个任务 → 更新「二、当前进度」表格与「五、下一步」，并跑一次 `scripts/check.ps1` 确认全绿。
- 增删或升级依赖 → 重新生成 `requirements.txt`。
- 阈值默认值变动 → 走 config 快照机制，不改业务代码。
