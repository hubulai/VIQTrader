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
| P1.3 QMT 适配器 | ⏸ 待启动 | 需用户 QMT 终端处于运行中方可实测（A股+港股通行情、A股财务 5 表） |
| P1.4 AkShare 适配器 | ✅ 完成 | `data/adapters/akshare.py`：声明 4 项能力（FINANCIAL / DIVIDEND / FX_RATE / STOCK_LIST），每项均实测选源；DISCLOSURE（实测无港股公告源）、HK_CONNECT_HISTORY（无名单变动流水源）、DAILY_BAR（属 QMT 分工）不声明，调用即抛 `UnsupportedCapabilityError`。附离线映射单测 17 例 + 联网集成测试 9 例 |
| P1.5 自算估值引擎 | ⏳ 待办 | PE/PB/PS/股息率（港股通按 H股 20% / 红筹 28% 税后）/市值 |
| P1.6 增量同步 + 质检 | ⏳ 待办 | 缺口、复权、停牌、披露日合理性校验 |
| P1.7 黄金数据集冻结 | ⏳ 待办 | 5~8 只代表股 3 年数据快照，后续回归基准 |
| P1.8 研报入库管线 | ⏳ 待办 | drop-in PDF → 文本提取 → 内容 hash 去重 → `document` 表 |
| P2~P7 | ⏳ 待办 | 账本+撮合+风控 → 回测+Walk-Forward+生命周期 → Agent 层 → 编排+学习地基 → Web 控制台 |

**最近一次验证**：`scripts/check.ps1` 全绿——ruff 通过、mypy 35 文件无错、import-linter 2 kept / 0 broken、pytest **49 passed, 9 deselected**；另有联网集成测试 **8 passed, 1 skipped**（跳过项为港股通名单接口，其依赖的 eastmoney push2 子域在本机不可达，如实标注跳过而非假装通过）。

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
│   │       └── 0002_domain_schema.sql   # 25 张领域表
│   ├── data/
│   │   ├── base.py                      # DataAdapter Protocol + Capability + 逐表列契约（SCHEMAS）
│   │   ├── contract.py                  # 契约检查套件（结构/取值/行为）+ ContractViolation
│   │   ├── adapters/                    # akshare.py（已交付）；qmt.py 待实现
│   │   ├── normalizer/                  # 标准化、复权、质检
│   │   ├── docs/                         # 研报 PDF 入库管线（P1.8）
│   │   └── schema/                       # 数据层内部 schema 辅助
│   ├── engine/                          # 确定性引擎（零 LLM）：backtest/portfolio/risk/ledger/execution
│   ├── agents/                          # LLM Agent：value（初筛/估值/反方）、swing、shared、llm、contracts、prompts
│   ├── orchestrator/                    # 任务编排与投委会会话
│   ├── learning/ · lifecycle/ · attribution/ · alerting/   # 自我学习 / 策略生命周期 / Brinson 归因 / 告警分级
│   └── web/                             # Web 控制台（只读 outputs/ 状态文件，与引擎解耦）
└── tests/
    ├── unit/                            # 配置加载与快照、迁移工具、全量建表核对
    ├── contract/                        # DataAdapter 契约套件 + FakeAdapter（假实现通过即合规）
    └── integration/                     # 真实外部依赖（网络/QMT），标记 integration 默认跳过
```

---

## 五、下一步

**当前推进：P1 数据层**

1. **P1.3 QMT 适配器**（当前，需用户配合）：确认 QMT 终端处于运行中后，实现 A股+港股通日线行情与 A股财务 5 表拉取（`download_financial_data2` 异步 + `report_type='report_time'`，拉 Balance/Income/CashFlow/PershareIndex/Capital），并过契约测试套件。
2. **P1.5** 自算估值引擎（含港股通税后股息率）→ **P1.6** 增量同步与质量校验 → **P1.7** 黄金数据集冻结 → **P1.8** 研报入库管线。
3. 随后按实现计划进入 **P2 账本+模拟撮合+风控引擎**。

**待用户提供的输入**：

- QMT 终端可用时段（P1.3 实测前置）；
- config 中全部阈值真实值（P2 联调前给出即可；目前 `default.yaml` 中标 `[占位]` 的项均为占位值）；
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

---

## 维护约定

- 每完成一个任务 → 更新「二、当前进度」表格与「五、下一步」，并跑一次 `scripts/check.ps1` 确认全绿。
- 增删或升级依赖 → 重新生成 `requirements.txt`。
- 阈值默认值变动 → 走 config 快照机制，不改业务代码。
