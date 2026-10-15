# A股交易规则引擎与双轨账本 · 详细设计 v1.1

> 状态：**v1.1 已确认两项关键决策（2026-10-15），其余待确认项按默认方案执行**
> 依据：`股票分析交易平台设计方案.md` v1（已冻结）+ web3-quant-sandbox 架构借鉴评估
> 对应实现：P2 引擎层（`engine/execution` + `engine/risk` + `engine/ledger` + `engine/portfolio`）
> 本文档经确认后冻结，作为 P2 实现依据。

---

## 一、设计目标与约束

| 目标 | 说明 |
|------|------|
| 确定性 | 纯代码实现，零 LLM 依赖，回测/模拟/实盘三环境同构 |
| 规则前置 | 任何订单意图必须先过交易规则 → 再过账户规则 → 再过风控规则，任一环节拒绝即终止 |
| 双轨隔离 | 价值轨/波段轨独立账本、独立配额、独立持仓，同标的两轨分两行记录 |
| 全量审计 | 每次规则校验、每笔成交、每次资金变动均留痕，携带 `config_hash` + `code_version` |
| 参数外置 | 所有阈值（涨跌幅、手数、配额、费用率）写在 YAML，改参不动代码 |

### 已确认决策（2026-10-15）

1. **双轨配额为软限制**：超限不阻断交易，只产生 WARN 告警，最终拍板权在用户。
2. **Intent 不冻结资金**：FILLED 才扣款；撮合扣款前重检资金充足性兜底（见 5.2）。

---

## 二、核心抽象：OrderIntent（订单意图）

策略层/Agent/人工只产出**不可变**的 OrderIntent，不碰执行细节。引擎消费 Intent 完成校验、撮合、记账。

```python
@dataclass(frozen=True, slots=True)
class OrderIntent:
    intent_id: str  # 唯一 ID
    account_id: str  # 归属子账本（value / swing）
    mode: str  # value / swing（冗余字段，便于过滤）
    code: str  # 证券代码
    market: str  # SH / SZ / BJ / HK
    side: OrderSide  # BUY / SELL
    order_type: OrderType  # MARKET / LIMIT / STOP / STOP_LIMIT
    qty: int  # 数量（股）
    price: Decimal | None  # 限价（MARKET 单为 None）
    stop_price: Decimal | None  # 止损触发价
    time_in_force: TimeInForce  # GTC / IOC / FOK
    thesis_id: str | None  # 关联买入逻辑（价值轨必填）
    strategy_id: str | None  # 关联波段策略（波段轨必填）
    submitted_at: datetime
```

**关键决策**：Python 层用 `Decimal` 保证资金计算精度，持久化到 DuckDB 时转 `DOUBLE`（与现有 schema 一致）。转换点统一在 store 层，引擎内部全程 Decimal。

### 状态机

```
PENDING → REJECTED（规则校验失败，附原因；或 FILLED 时资金不足兜底拒绝）
        → FILLED（撮合成交，记账完成）
        → CANCELLED（人工撤销）
```

---

## 三、交易规则引擎：三阶段校验管线

### 3.1 执行顺序

```
OrderIntent
    ↓
Phase 1 MARKET（市场机制：能不能交易）
    ├── T1Rule            A股 T+1：当日买入不可卖
    ├── TradingDayRule    非交易日拒绝
    ├── SuspensionRule    停牌不可交易
    ├── PriceLimitRule    涨跌停校验
    ├── LotSizeRule       手数校验
    └── HKConnectRule     港股通专项（名单/交易日/T+0）
    ↓ 全部通过
Phase 2 ACCOUNT（账户可行性：有没有资源）
    ├── CashSufficiencyRule   可用资金充足（含费用预估）
    ├── PositionLimitRule     卖出量 ≤ 可用持仓（qty - frozen_qty）
    ├── ModeIsolationRule     mode 与 account 匹配
    └── QuotaRule             双轨配额检查（软限制，只告警不阻断）
    ↓ 全部通过
Phase 3 RISK（风控规则：该不该交易）
    └── 已有设计的风控规则集（安全边际/仓位上限/行业集中度等）
    ↓ 全部通过
ExecutionGateway.submit(intent) → 撮合 → LedgerEngine.execute_fill()
```

任一规则返回 `passed=False`（QuotaRule 除外，见 3.3），整个校验终止，Intent 状态置 REJECTED，所有检查结果写入 `rule_check_log`。

### 3.2 Phase 1：市场规则详细设计

#### T1Rule（T+1）

- A股（market ∈ SH/SZ/BJ）：BUY 成交后 `position.frozen_qty += qty`，当日 SELL 校验 `intent.qty ≤ qty - frozen_qty`
- 港股通（market = HK）：T+0 回转交易，跳过此规则
- 解冻时机：日终结算（`settle_end_of_day`）清零 frozen_qty
- 回测同构：每个新 bar 开始时先解冻昨日冻结

#### PriceLimitRule（涨跌停校验）

每日预计算 `market_status` 表的涨跌停价，按板块区分：

| 板块 | 判断依据 | 正常涨跌幅 | ST 涨跌幅 |
|------|----------|-----------|----------|
| 主板（SH/SZ） | code 前缀 60/00 | ±10% | ±5% |
| 创业板 | code 前缀 30 | ±20% | ±20% |
| 科创板 | code 前缀 68 | ±20% | ±20% |
| 北交所 | code 前缀 8/4 | ±30% | ±30% |

校验逻辑：
- LIMIT 单：`price` 必须在 `[down_limit, up_limit]` 区间内，否则拒绝
- MARKET 单：BUY 时若当前价 ≥ 涨停价，不可成交；SELL 时若当前价 ≤ 跌停价，不可成交
- 涨跌停价计算基准：前一交易日收盘价（未复权），按交易所规则 round 到小数点后 2 位

#### LotSizeRule（手数校验）

| 板块 | 规则 |
|------|------|
| 主板 | qty % 100 == 0 |
| 科创板 | qty ≥ 200 且之后按 1 股递增 |
| 北交所 | qty % 100 == 0 |
| 港股通 | qty % lot_size == 0（标的级数据，v1 简化为 1，标记 TODO） |

#### SuspensionRule（停牌）

- `market_status.is_suspended = true` 的标的，当日所有 Intent 拒绝
- 数据来源：从 `fact_daily_bar` 推导（无数据或 volume=0 视为停牌）

#### HKConnectRule（港股通专项）

| 子规则 | 逻辑 |
|--------|------|
| 名单拦截 | `is_hk_connect = false` 且 `side = BUY` → 拒绝（调出后只能卖不能买） |
| 交易日交集 | 仅 A股与港股同时开市日可交易（v1 用 A股交易日近似，标记 TODO） |
| T+0 | 跳过 T1Rule |
| 不可参与 | IPO/配售/供股 → 由数据层保证不出现此类 Intent |

### 3.3 Phase 2：账户规则详细设计

#### CashSufficiencyRule

```
所需资金 = qty × price + 预估费用（佣金 + 印花税 + 过户费）
可用资金 = cash_ledger 该 account 最新 balance_cny
校验：所需资金 ≤ 可用资金
```

注：因 Intent 不冻结资金，此校验为"校验时点"快照，最终一致性由 FILLED 时重检兜底（见 5.2）。

#### PositionLimitRule

```
SELL 校验：intent.qty ≤ position.qty - position.frozen_qty
BUY 无此限制
```

#### QuotaRule（双轨配额，**软限制——已确认**）

```
单轨总价值 = track_cash + track_market_value
总资产 = Σ(两轨总价值)
检查：买入后 单轨总价值 / 总资产 > target_ratio + drift_tolerance 时触发
```

- **已确认：软限制**。超限**不阻断交易**，生成 WARN 级风控事件（写入 `risk_event` 表 + 走 `alerting` 模块实时推送），最终拍板权在用户
- `drift_tolerance` 默认 0.05（进 config），作为告警阈值
- 新买入与存量漂移一视同仁：都只告警、不拦截
- 该规则在 `rule_check_log` 中记录 `passed=True`（不阻断），同时产生 WARN 级 `risk_event`

#### ModeIsolationRule

- `intent.mode` 必须与 `intent.account_id` 对应的 `account.mode` 一致
- 防止价值单误入波段账

### 3.4 Phase 3：风控规则衔接

不重复设计。已有设计方案第七节的两轨风控规则集作为 Phase 3 规则接入，接口统一为 `TradingRule` Protocol。风控规则触发时同时写入 `risk_event` 表（已有表结构）。

---

## 四、数据模型（DuckDB DDL）

### 4.1 新增表（迁移脚本 `0003_trading_rules_and_ledger.sql`）

```sql
-- 订单意图（核心表，记录所有下单意图的全生命周期）
CREATE TABLE order_intent (
    intent_id      TEXT PRIMARY KEY,
    account_id     TEXT NOT NULL REFERENCES account(account_id),
    mode           TEXT NOT NULL,                -- value / swing
    code           TEXT NOT NULL,
    market         TEXT NOT NULL,                -- SH / SZ / BJ / HK
    side           TEXT NOT NULL,                -- BUY / SELL
    order_type     TEXT NOT NULL,                -- MARKET / LIMIT / STOP / STOP_LIMIT
    qty            INTEGER NOT NULL,
    price          DOUBLE,                       -- 限价（MARKET 单为 NULL）
    stop_price     DOUBLE,                       -- 止损触发价
    time_in_force  TEXT NOT NULL DEFAULT 'GTC',
    status         TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING/REJECTED/FILLED/CANCELLED
    rejection_reason TEXT,
    thesis_id      TEXT,                         -- 价值轨关联买入逻辑
    strategy_id    TEXT,                         -- 波段轨关联策略
    submitted_at   TIMESTAMPTZ NOT NULL,
    filled_at      TIMESTAMPTZ,
    filled_price   DOUBLE,
    config_hash    TEXT NOT NULL,
    code_version   TEXT NOT NULL
);

-- 市场状态快照（日频预计算，涨跌停/停牌/ST 判断依据）
CREATE TABLE market_status (
    code            TEXT NOT NULL,
    trade_date      DATE NOT NULL,
    board           TEXT NOT NULL,               -- MAIN / CHINEXT / STAR / BSE / HK
    is_suspended    BOOLEAN NOT NULL DEFAULT FALSE,
    is_st           BOOLEAN NOT NULL DEFAULT FALSE,
    up_limit_price  DOUBLE,
    down_limit_price DOUBLE,
    is_hk_connect   BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (code, trade_date)
);

-- 规则校验流水（每次 Intent 校验的完整记录）
CREATE TABLE rule_check_log (
    check_id     TEXT PRIMARY KEY,
    intent_id    TEXT NOT NULL REFERENCES order_intent(intent_id),
    phase        TEXT NOT NULL,                  -- MARKET / ACCOUNT / RISK
    rule_type    TEXT NOT NULL,                  -- T1 / PRICE_LIMIT / LOT_SIZE / ...
    passed       BOOLEAN NOT NULL,
    message      TEXT,
    checked_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    config_hash  TEXT NOT NULL,
    code_version TEXT NOT NULL
);

-- 板块维度（dim_stock 补充，从 code 前缀推断，显式存储）
ALTER TABLE dim_stock ADD COLUMN board TEXT;     -- MAIN / CHINEXT / STAR / BSE / HK

-- position 增加 T+1 冻结量
ALTER TABLE position ADD COLUMN frozen_qty INTEGER NOT NULL DEFAULT 0;
-- 约束：frozen_qty ≤ qty（应用层保证）

-- trade_log 关联订单意图
ALTER TABLE trade_log ADD COLUMN intent_id TEXT REFERENCES order_intent(intent_id);
```

### 4.2 表关系

```
account ──┬── order_intent ──→ rule_check_log
          │         │
          │         └──→ trade_log（FILLED 后写入，通过 intent_id 关联）
          ├── position（含 frozen_qty）
          └── cash_ledger

market_status（独立预计算，供规则引擎查询）
```

---

## 五、T+1 与资金结算机制

### 5.1 T+1 冻结与解冻

```
BUY FILLED:
    position.qty += qty
    position.frozen_qty += qty    -- 当日不可卖

SELL 校验:
    intent.qty ≤ (position.qty - position.frozen_qty)

日终结算（settle_end_of_day）:
    UPDATE position SET frozen_qty = 0 WHERE frozen_qty > 0

回测同构:
    每个新 bar（新交易日）开始时，先执行解冻，再运行策略
```

### 5.2 资金结算（Intent 不冻结——已确认）

- **已确认**：Intent 创建/PENDING 时不冻结资金，FILLED 时才扣款
- **已知风险**：多个 PENDING Intent 可能同时通过 CashSufficiencyRule（校验时都未扣款）
- **兜底**：`LedgerEngine.execute_fill` 在实际扣款前**重检一次资金充足性**；不足则成交失败，Intent 置 `REJECTED`（reason: `INSUFFICIENT_CASH_AT_FILL`）
- 保证：账本永不出现负现金；日频 + 人工确认场景下极少触发

### 5.3 边界情况

| 场景 | 处理 |
|------|------|
| 同日多次买入 | frozen_qty 累加 |
| 同日买入+卖出 | SELL 只能卖可用部分（qty - frozen_qty） |
| 港股通 | 不设 frozen_qty，T+0 当日可卖 |
| 停牌后复牌 | frozen_qty 不清零，按正常日终结算处理 |

---

## 六、涨跌停价计算

### 6.1 预计算任务（每日盘后运行）

```python
def compute_market_status(code: str, trade_date: date) -> MarketStatus:
    prev_close = get_prev_close(code, trade_date)  # 未复权前收盘
    board = infer_board(code)  # 从 code 前缀推断
    is_st = check_st(code, trade_date)  # 从 dim_stock 名称含 "ST" 推断
    is_suspended = check_suspension(code, trade_date)

    limit_pct = PRICE_LIMIT_MAP[board]["st" if is_st else "normal"]

    return MarketStatus(
        code=code,
        trade_date=trade_date,
        board=board,
        is_suspended=is_suspended,
        is_st=is_st,
        up_limit_price=round(prev_close * (1 + limit_pct), 2),
        down_limit_price=round(prev_close * (1 - limit_pct), 2),
    )
```

### 6.2 板块推断规则

```python
def infer_board(code: str) -> str:
    if code.startswith("68"):
        return "STAR"  # 科创板
    if code.startswith("30"):
        return "CHINEXT"  # 创业板
    if code.startswith(("8", "4")):
        return "BSE"  # 北交所
    if len(code) == 5:
        return "HK"  # 港股
    return "MAIN"  # 主板
```

---

## 七、双轨账本模型

### 7.1 账户初始化

`account` 表预置两行：

| account_id | mode | target_ratio | capital_quota |
|-----------|------|-------------|---------------|
| acc_value | value | 0.80 | 用户确认后填入 |
| acc_swing | swing | 0.20 | 用户确认后填入 |

### 7.2 资金视图（派生，不冗余存储）

```
单轨可用现金 = cash_ledger 最新 balance_cny（按 account_id 过滤）
单轨持仓市值 = Σ(position.qty × current_price)（按 account_id 过滤）
单轨总价值 = 可用现金 + 持仓市值
总资产 = 价值轨总价值 + 波段轨总价值
单轨占比 = 单轨总价值 / 总资产
```

### 7.3 成本双口径

| 市场 | avg_cost（原币） | avg_cost_cny（人民币） |
|------|-----------------|----------------------|
| A股 | CNY 价格 | = avg_cost |
| 港股通 | HKD 价格 | avg_cost × 买入时 fx_rate |

已实现盈亏计算：
- A股：`realized_pnl = (sell_price - avg_cost) × qty - fees`
- 港股通：`realized_pnl_cny = (sell_price × sell_fx - avg_cost × buy_fx) × qty - fees_cny`
- 汇兑损益单列：`fx_pnl = qty × avg_cost × (sell_fx - buy_fx)`

### 7.4 分红入账

| 类型 | 处理 | cash_ledger | position |
|------|------|------------|----------|
| 现金分红（A股） | amount = qty × dps | change_type='DIVIDEND' | 不变 |
| 现金分红（港股通） | amount = qty × dps × (1 - tax_rate) × fx_rate | change_type='DIVIDEND' | 不变 |
| 送股 | new_qty = qty + 送股数（v1 暂不支持，标记 TODO） | 不涉及 | qty 增加，avg_cost 摊薄 |

送股后成本摊薄公式（v1 后续实现）：`avg_cost_new = total_cost / new_qty`

### 7.5 现金流水规则

- BUY FILLED：change_type='买入'，amount_cny = -(qty × price + fees)
- SELL FILLED：change_type='卖出'，amount_cny = +(qty × price - fees)
- 分红：change_type='分红'，amount_cny = +税后分红额
- 汇兑：change_type='汇兑'，amount_cny = 汇兑损益
- balance_cny 为 running balance，每条流水记录当时结余

### 7.6 持仓隔离红线

- `position` 表已有 `UNIQUE(account_id, code)` 约束
- 同一 code 在 value 和 swing 下是两条独立记录，**禁止合并**
- 任何查询、统计、风控必须带 `account_id` 或 `mode` 过滤
- 两轨之间不允许内部转账（v1 简化，如需调仓由人工在真实账户操作后手动调整）

---

## 八、与回测引擎的同构设计

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   回测环境    │     │   模拟盘     │     │  实盘(预留)  │
├─────────────┤     ├─────────────┤     ├─────────────┤
│ 历史数据适配器 │     │ 实时数据适配器│     │ 实时数据适配器│
│ 模拟撮合网关  │     │ dry-run 网关│     │ QMT 下单网关 │
├─────────────┤     ├─────────────┤     ├─────────────┤
│         同一套 OrderIntent + 规则引擎 + 账本引擎          │
└─────────────┘     └─────────────┘     └─────────────┘
```

- 策略接口、规则引擎、账本引擎三个环境完全同构
- 差异仅封装在**数据适配器**（历史 vs 实时）和**执行网关**（模拟撮合 vs dry-run vs QMT）
- 回测中的模拟撮合同样走 PriceLimitRule（涨停不可买、跌停不可卖）和 T1Rule

---

## 九、Python 接口契约

```python
# ---------- 规则引擎 ----------
@dataclass(frozen=True, slots=True)
class RuleCheckResult:
    passed: bool
    rule_id: str
    message: str = ""
    severity: str = "INFO"  # INFO / WARN：WARN 只告警不阻断（QuotaRule 用）


class TradingRule(Protocol):
    rule_id: str
    phase: Literal["MARKET", "ACCOUNT", "RISK"]

    def check(self, intent: OrderIntent, *, ctx: RuleContext) -> RuleCheckResult: ...


@dataclass(slots=True)
class RuleContext:
    trade_date: date
    market_status: MarketStatus | None
    position: Position | None
    track_cash: Decimal
    track_market_value: Decimal
    total_portfolio_value: Decimal
    config: dict


class TradingRuleEngine:
    def __init__(self, rules: list[TradingRule]): ...
    def validate(self, intent: OrderIntent, ctx: RuleContext) -> ValidationResult: ...


# ---------- 账本引擎 ----------
class LedgerEngine:
    def execute_fill(
        self,
        intent: OrderIntent,
        fill_price: Decimal,
        fees: FeeBreakdown,
        trade_date: date,
    ) -> TradeLog:
        """扣款前重检资金充足性，不足则抛 InsufficientCashAtFill"""

    def settle_end_of_day(self, trade_date: date) -> None: ...

    def process_dividend(
        self,
        code: str,
        ex_date: date,
        dps: Decimal,
        dividend_type: str,
    ) -> None: ...

    def get_available_cash(self, account_id: str) -> Decimal: ...
    def get_track_value(self, account_id: str, as_of: date) -> Decimal: ...


# ---------- 执行网关 ----------
class ExecutionGateway(Protocol):
    def submit(self, intent: OrderIntent) -> FillResult: ...


class SimulatedGateway:  # 回测用，按历史 bar 撮合
    ...


class DryRunGateway:  # 模拟盘用，只记录不真实下单
    ...


class QMTGateway:  # 实盘预留
    ...
```

---

## 十、配置扩展（default.yaml 新增节）

```yaml
# ---------- 交易规则参数 ----------
trading:
  a_share_t_plus_1: true              # 已有
  hk_connect_t_plus_0: true           # 已有
  limit_up_down_block: true           # 已有
  price_limit:                        # 新增：涨跌幅按板块
    main_normal: 0.10
    main_st: 0.05
    chinext: 0.20
    star: 0.20
    bse: 0.30
  lot_size:                           # 新增：手数按板块
    main: 100
    star_min: 200
    bse: 100
    hk_default: 1                     # [占位] v1 简化
  drift_tolerance: 0.05               # 新增：双轨配额告警阈值（软限制，只告警）
```

---

## 十一、实现顺序建议

| 步骤 | 内容 | 依赖 |
|------|------|------|
| 1 | 迁移脚本 `0003`（新表 + ALTER） | 无 |
| 2 | `market_status` 预计算任务 | dim_stock.board 填充 |
| 3 | OrderIntent + 状态机 | 迁移脚本 |
| 4 | TradingRule Protocol + Phase 1 市场规则 | market_status |
| 5 | Phase 2 账户规则（含软限制 QuotaRule） | position.frozen_qty + cash_ledger |
| 6 | LedgerEngine（execute_fill 含资金重检 / settle / dividend） | 迁移脚本 |
| 7 | Phase 3 风控规则接入 | 已有风控设计 |
| 8 | SimulatedGateway（回测撮合） | 规则引擎 + 账本 |
| 9 | DryRunGateway（模拟盘） | 规则引擎 + 账本 |
| 10 | 端到端测试 | 全部 |

---

## 十二、风险与待确认

| # | 事项 | 状态 | 说明 |
|---|------|------|------|
| 1 | 双轨配额软限制 | ✅ **已确认 2026-10-15** | 超限只告警不阻断，`drift_tolerance: 0.05` 为告警阈值 |
| 2 | Intent 不冻结资金 | ✅ **已确认 2026-10-15** | FILLED 才扣款 + 扣款前重检兜底 |
| 3 | 港股通手数数据（每标的不同） | 默认方案 | v1 简化为 1，后续从数据源补齐 |
| 4 | 交易日历（A股+港股交集） | 默认方案 | v1 用 A股交易日近似 |
| 5 | ST 状态历史回溯 | 默认方案 | 从 dim_stock.name 含 "ST" 推断，接受精度受限 |
| 6 | 涨跌停价反算精度 | 默认方案 | 按交易所规则 round 到小数点后 2 位 |
| 7 | 送股/拆股处理 | 默认方案 | v1 仅支持现金分红，送股后续实现 |

---

## 十三、变更记录

| 日期 | 变更点 | 影响范围 |
|------|--------|----------|
| 2026-10-15 | 初版 | P2 引擎层全部 |
| 2026-10-15 | v1.1：确认双轨配额为软限制（QuotaRule 只告警不阻断）；确认 Intent 不冻结资金，新增 FILLED 扣款前重检兜底（`INSUFFICIENT_CASH_AT_FILL`）；RuleCheckResult 增加 severity 字段支持 WARN 级告警 | QuotaRule、LedgerEngine.execute_fill、RuleCheckResult |
