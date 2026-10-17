"""P1.6 增量同步编排器。

职责：从适配器拉数据 → 归一化入库 → 串联估值引擎 → 质量校验 → 输出报告。

分层契约：data 层（第 6 层）只 import store / config / alerting，不得 import
engine / agents / orchestrator。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import duckdb
import pandas as pd

from viqtrader.config.loader import Config

# data 层只 import 下层
from .base import (
    SCHEMAS,
    Capability,
    DataAdapter,
    DataSourceError,
    TableSchema,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 能力 → 表名 / 主键 / 日期列 映射
# ---------------------------------------------------------------------------

_CAP_TABLE: dict[Capability, str] = {
    Capability.STOCK_LIST: "dim_stock",
    Capability.DAILY_BAR: "fact_daily_bar",
    Capability.FINANCIAL: "fact_financial",
    Capability.DIVIDEND: "fact_dividend",
    Capability.FX_RATE: "fact_fx_rate",
    Capability.INDEX_BAR: "fact_index_bar",
}

# 用于增量起点查询的日期列（每种能力用于 "最后日期" 的那列）
_CAP_DATE_COL: dict[Capability, str] = {
    Capability.DAILY_BAR: "trade_date",
    Capability.FINANCIAL: "report_period",
    Capability.DIVIDEND: "ex_date",
    Capability.FX_RATE: "trade_date",
    Capability.INDEX_BAR: "trade_date",
    Capability.STOCK_LIST: "",  # 无日期
}

# 适配器是否接收 (code, start, end) 风格参数
_CODE_CAPS = frozenset({Capability.DAILY_BAR, Capability.FINANCIAL, Capability.DIVIDEND})
_DATE_CAPS = frozenset({Capability.DAILY_BAR, Capability.FX_RATE})


# ---------------------------------------------------------------------------
# 配置与报告
# ---------------------------------------------------------------------------


@dataclass
class SyncConfig:
    """单次同步的配置。

    Attributes:
        codes: 本次同步标的列表（FX_RATE 用 pair 代替 code，如 "HKD/CNY"）。
        start_date: 强制起点；None 则从库里最后日期 + 1 开始。
        end_date: 强制终点；None 默认今天。
        adapter: 数据适配器（AkShare / QMT）。
        capabilities: 要同步的能力列表。
        fx_pairs: FX_RATE 专用的 pair 列表（默认 ["HKD/CNY"]）。
    """

    codes: list[str]
    start_date: date | None = None
    end_date: date | None = None
    adapter: DataAdapter = None  # type: ignore[assignment]  # 构造时必填
    capabilities: list[Capability] = field(
        default_factory=lambda: [Capability.DAILY_BAR, Capability.FINANCIAL, Capability.DIVIDEND]
    )
    fx_pairs: list[str] = field(default_factory=lambda: ["HKD/CNY"])


@dataclass
class SyncReport:
    """单个 (code, capability) 的同步结果。"""

    code: str
    capability: str
    rows_inserted: int = 0
    rows_updated: int = 0
    gaps_found: list[tuple[date, date]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 内部工具：日期缺口检测
# ---------------------------------------------------------------------------


def _detect_gaps(
    dates: pd.Series, *, lookback_limit: int
) -> tuple[list[tuple[date, date]], list[str]]:
    """按日期排序后检测连续缺口。

    返回:
        gaps: 缺口区间列表 [(start, end), ...]
        warnings: 超出 lookback_limit 的缺口告警
    """
    gaps: list[tuple[date, date]] = []
    warnings: list[str] = []
    s = pd.to_datetime(dates.dropna()).sort_values().reset_index(drop=True)
    if len(s) < 2:
        return gaps, warnings
    diffs = s.diff().dt.days
    for i in range(1, len(s)):
        d = diffs.iloc[i]
        if d > 1:
            gap_start = (s.iloc[i - 1] + timedelta(days=1)).date()
            gap_end = (s.iloc[i] - timedelta(days=1)).date()
            if gap_start <= gap_end:
                gaps.append((gap_start, gap_end))
                if d - 1 > lookback_limit:
                    warnings.append(
                        f"缺口 {gap_start}~{gap_end} 共 {d - 1} 天，"
                        f"超出回补窗口 {lookback_limit} 天"
                    )
    return gaps, warnings


def _quarterly_periods(end: date, years: int) -> list[date]:
    """近 N 年季度报告期列表（自然季度末，含 end 之前已过去的季度末）。

    最早到 end.year - years + 1 的 Q1。财务同步按报告期列表拉取，
    报告期 P 的公告日必然晚于 P，适配器以 min(periods) 为下载起点无前视遗漏。
    """
    first_year = end.year - max(1, years) + 1
    periods: list[date] = []
    for y in range(first_year, end.year + 1):
        for m, d in ((3, 31), (6, 30), (9, 30), (12, 31)):
            p = date(y, m, d)
            if p <= end:
                periods.append(p)
    return periods


# ---------------------------------------------------------------------------
# 增量起点计算
# ---------------------------------------------------------------------------


def _latest_in_db(
    conn: duckdb.DuckDBPyConnection, table: str, date_col: str, code: str | None = None
) -> date | None:
    """查库中某标的的最大日期。"""
    sql = f"SELECT MAX({date_col}) FROM {table}"
    args: list[Any] = []
    if code is not None:
        schema = SCHEMAS[_cap_from_table(table)]
        code_col = schema.key[0]  # 主键第一列即 code/pair
        sql += f" WHERE {code_col} = ?"
        args.append(code)
    row = conn.execute(sql, args).fetchone()
    if row and row[0] is not None:
        val = row[0]
        if isinstance(val, datetime):
            return val.date()
        return date.fromisoformat(str(val))
    return None


_TABLE_TO_CAP: dict[str, Capability] = {
    "dim_stock": Capability.STOCK_LIST,
    "fact_daily_bar": Capability.DAILY_BAR,
    "fact_financial": Capability.FINANCIAL,
    "fact_dividend": Capability.DIVIDEND,
    "fact_fx_rate": Capability.FX_RATE,
    "fact_index_bar": Capability.INDEX_BAR,
}


def _cap_from_table(table: str) -> Capability:
    return _TABLE_TO_CAP[table]


# ---------------------------------------------------------------------------
# 入库：DELETE + INSERT 幂等
# ---------------------------------------------------------------------------


def _delete_range(
    conn: duckdb.DuckDBPyConnection,
    table: str,
    schema: TableSchema,
    df: pd.DataFrame,
) -> None:
    """根据 DataFrame 的主键范围 DELETE，再批量 INSERT。"""
    if df.empty:
        return
    key_cols = schema.key
    date_cols_in_key = [c for c in key_cols if c in schema.date_columns]

    if date_cols_in_key:
        # 有日期列：按主键第一列 + 日期范围 DELETE
        code_col = key_cols[0]
        date_col = date_cols_in_key[0]
        codes = df[code_col].dropna().unique().tolist()
        dates = pd.to_datetime(df[date_col]).dropna()
        if codes and not dates.empty:
            min_date = dates.min().date()
            max_date = dates.max().date()
            conn.execute(
                f"DELETE FROM {table} WHERE {code_col} IN ({','.join(['?'] * len(codes))}) "
                f"AND {date_col} BETWEEN ? AND ?",
                [*codes, min_date, max_date],
            )
    else:
        # STOCK_LIST：全量覆盖
        code_col = key_cols[0]
        conn.execute(f"DELETE FROM {table}")


def _insert_df(
    conn: duckdb.DuckDBPyConnection,
    table: str,
    schema: TableSchema,
    df: pd.DataFrame,
) -> int:
    """批量插入，返回插入行数。"""
    if df.empty:
        return 0
    # 裁剪到契约列
    cols = [c for c in schema.columns if c in df.columns]
    out = df[cols].copy()
    # DuckDB 对 datetime64 接受 DATE 兼容
    for col in schema.date_columns:
        if col in out.columns:
            out[col] = pd.to_datetime(out[col]).dt.date
    # bool 列：DuckDB executemany 不接受 numpy.bool，需转 Python bool
    for col in schema.bool_columns:
        if col in out.columns:
            out[col] = out[col].apply(lambda v: bool(v) if v is not None and pd.notna(v) else None)
    # date 列也确保是原生 date 而非 pandas Timestamp
    for col in schema.date_columns:
        if col in out.columns:
            out[col] = out[col].apply(lambda v: v.date() if isinstance(v, pd.Timestamp) else v)
    tuples = list(out.itertuples(index=False, name=None))
    if not tuples:
        return 0
    placeholders = ",".join(["?"] * len(cols))
    conn.executemany(
        f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})",
        tuples,
    )
    return len(tuples)


# ---------------------------------------------------------------------------
# 前视偏差过滤
# ---------------------------------------------------------------------------


def _filter_lookahead(df: pd.DataFrame, cap: Capability) -> list[str]:
    """过滤 ann_date < report_period 等硬性前视偏差违规，返回告警列表。"""
    warnings: list[str] = []
    if df.empty:
        return warnings

    if cap is Capability.FINANCIAL:
        mask = df["ann_date"].notna() & df["report_period"].notna()
        bad = mask & (pd.to_datetime(df["ann_date"]) < pd.to_datetime(df["report_period"]))
        if bad.any():
            count = int(bad.sum())
            df.drop(df.index[bad], inplace=True)
            df.reset_index(drop=True, inplace=True)
            warnings.append(f"ann_date < report_period 前视偏差：拒绝 {count} 行")

    if cap is Capability.DIVIDEND:
        # ex_date 不能是未来日期（宽松检查，不硬拒绝，只告警）
        today = pd.Timestamp(date.today())
        future = df["ex_date"].notna() & (pd.to_datetime(df["ex_date"]) > today)
        if future.any():
            count = int(future.sum())
            warnings.append(f"发现 {count} 条 ex_date 为未来日期的分红记录")
        # ex_date ≤ pay_date
        both = df["ex_date"].notna() & df["pay_date"].notna()
        wrong_order = both & (pd.to_datetime(df["ex_date"]) > pd.to_datetime(df["pay_date"]))
        if wrong_order.any():
            count = int(wrong_order.sum())
            df.drop(df.index[wrong_order], inplace=True)
            df.reset_index(drop=True, inplace=True)
            warnings.append(f"ex_date > pay_date 异常：拒绝 {count} 行")

    return warnings


# ---------------------------------------------------------------------------
# 核心类
# ---------------------------------------------------------------------------


class IncrementalSyncer:
    """增量同步编排器。

    使用方式::

        conn = db.connect_memory()
        migrate.migrate(conn)
        adapter = AkShareAdapter()
        syncer = IncrementalSyncer(conn, config)
        reports = syncer.sync(SyncConfig(codes=["00700"], adapter=adapter, ...))
    """

    def __init__(
        self,
        conn: duckdb.DuckDBPyConnection,
        config: Config | dict[str, Any] | None = None,
    ) -> None:
        self.conn = conn
        # 从 Config 或 dict 读配置
        if isinstance(config, Config):
            self.lookback_days_on_gap: int = int(config.get("data.sync.lookback_days_on_gap", 10))
            self.financial_lookback_years: int = int(
                config.get("data.sync.financial_lookback_years", 3)
            )
            self.daily_lookback_days: int = int(config.get("data.sync.daily_lookback_days", 365))
            self.config: Config | dict[str, Any] | None = config
        elif isinstance(config, dict):
            sync_cfg = config.get("data", {}).get("sync", {})
            self.lookback_days_on_gap = int(sync_cfg.get("lookback_days_on_gap", 10))
            self.financial_lookback_years = int(sync_cfg.get("financial_lookback_years", 3))
            self.daily_lookback_days = int(sync_cfg.get("daily_lookback_days", 365))
            self.config = config
        else:
            self.lookback_days_on_gap = 10
            self.financial_lookback_years = 3
            self.daily_lookback_days = 365
            self.config = None

    # ---------------- 公共入口 ----------------

    def sync(self, cfg: SyncConfig) -> list[SyncReport]:
        """执行同步，返回 SyncReport 列表。"""
        end_date = cfg.end_date or date.today()
        start_date_override = cfg.start_date

        reports: list[SyncReport] = []

        for cap in cfg.capabilities:
            if cap not in cfg.adapter.capabilities():
                for code in cfg.codes:
                    reports.append(
                        SyncReport(
                            code=code,
                            capability=cap.value,
                            warnings=[f"适配器 {cfg.adapter.name} 不支持能力 {cap.value}"],
                        )
                    )
                continue

            if cap is Capability.STOCK_LIST:
                reports.extend(self._sync_stock_list(cfg, cap))
            elif cap is Capability.FX_RATE:
                for pair in cfg.fx_pairs:
                    reports.extend(
                        self._sync_fx_rate(cfg, cap, pair, start_date_override, end_date)
                    )
            elif cap is Capability.DAILY_BAR:
                for code in cfg.codes:
                    reports.extend(
                        self._sync_daily_bar(cfg, cap, code, start_date_override, end_date)
                    )
            elif cap is Capability.INDEX_BAR:
                for code in cfg.codes:
                    reports.extend(
                        self._sync_index_bar(cfg, cap, code, start_date_override, end_date)
                    )
            elif cap is Capability.FINANCIAL:
                for code in cfg.codes:
                    reports.extend(self._sync_financial(cfg, cap, code, end_date))
            elif cap is Capability.DIVIDEND:
                for code in cfg.codes:
                    reports.extend(self._sync_dividend(cfg, cap, code))
            else:
                for code in cfg.codes:
                    reports.append(
                        SyncReport(
                            code=code,
                            capability=cap.value,
                            warnings=[f"暂未实现能力 {cap.value}"],
                        )
                    )

        # 同步完基础数据后，串联估值引擎（需要日线 + 财务 + 分红 + dim_stock）
        self._recompute_valuation(cfg.codes)

        return reports

    # ---------------- 各能力私有实现 ----------------

    def _sync_stock_list(self, cfg: SyncConfig, cap: Capability) -> list[SyncReport]:
        report = SyncReport(code="__all__", capability=cap.value)
        try:
            df = cfg.adapter.fetch_stock_list()
        except DataSourceError as exc:
            report.warnings.append(f"数据源错误: {exc}")
            return [report]

        if df.empty:
            return [report]

        # STOCK_LIST 无日期，全量覆盖
        schema = SCHEMAS[cap]
        table = _CAP_TABLE[cap]
        self.conn.execute("BEGIN TRANSACTION")
        try:
            _delete_range(self.conn, table, schema, df)
            report.rows_inserted = _insert_df(self.conn, table, schema, df)
            self.conn.execute("COMMIT")
        except Exception as exc:
            self.conn.execute("ROLLBACK")
            report.warnings.append(f"入库失败: {exc}")
            logger.exception("stock_list 入库异常")
        return [report]

    def _sync_daily_bar(
        self,
        cfg: SyncConfig,
        cap: Capability,
        code: str,
        start_override: date | None,
        end_date: date,
    ) -> list[SyncReport]:
        report = SyncReport(code=code, capability=cap.value)

        # 增量起点
        table = _CAP_TABLE[cap]
        date_col = _CAP_DATE_COL[cap]
        latest = _latest_in_db(self.conn, table, date_col, code)

        if start_override is not None:
            start = start_override
        elif latest is not None:
            start = latest + timedelta(days=1)
        else:
            start = end_date - timedelta(days=self.daily_lookback_days)

        if start > end_date:
            report.warnings.append(f"无新数据：起点 {start} > 终点 {end_date}")
            return [report]

        try:
            df = cfg.adapter.fetch_daily_bar(code, start, end_date)
        except DataSourceError as exc:
            report.warnings.append(f"数据源错误: {exc}")
            return [report]

        if df.empty:
            return [report]

        # 缺口检测
        gaps, gap_warnings = _detect_gaps(
            df["trade_date"], lookback_limit=self.lookback_days_on_gap
        )
        report.gaps_found = gaps
        report.warnings.extend(gap_warnings)

        schema = SCHEMAS[cap]
        self.conn.execute("BEGIN TRANSACTION")
        try:
            _delete_range(self.conn, table, schema, df)
            report.rows_inserted = _insert_df(self.conn, table, schema, df)
            self.conn.execute("COMMIT")
        except Exception as exc:
            self.conn.execute("ROLLBACK")
            report.warnings.append(f"入库失败: {exc}")
            logger.exception("daily_bar 入库异常")
        return [report]

    def _sync_index_bar(
        self,
        cfg: SyncConfig,
        cap: Capability,
        code: str,
        start_override: date | None,
        end_date: date,
    ) -> list[SyncReport]:
        """指数日线同步（fact_index_bar），增量逻辑与日 K 一致，仅取数入口不同。"""
        report = SyncReport(code=code, capability=cap.value)

        table = _CAP_TABLE[cap]
        date_col = _CAP_DATE_COL[cap]
        latest = _latest_in_db(self.conn, table, date_col, code)

        if start_override is not None:
            start = start_override
        elif latest is not None:
            start = latest + timedelta(days=1)
        else:
            start = end_date - timedelta(days=self.daily_lookback_days)

        if start > end_date:
            report.warnings.append(f"无新数据：起点 {start} > 终点 {end_date}")
            return [report]

        try:
            df = cfg.adapter.fetch_index_bar(code, start, end_date)
        except DataSourceError as exc:
            report.warnings.append(f"数据源错误: {exc}")
            return [report]

        if df.empty:
            return [report]

        gaps, gap_warnings = _detect_gaps(
            df["trade_date"], lookback_limit=self.lookback_days_on_gap
        )
        report.gaps_found = gaps
        report.warnings.extend(gap_warnings)

        schema = SCHEMAS[cap]
        self.conn.execute("BEGIN TRANSACTION")
        try:
            _delete_range(self.conn, table, schema, df)
            report.rows_inserted = _insert_df(self.conn, table, schema, df)
            self.conn.execute("COMMIT")
        except Exception as exc:
            self.conn.execute("ROLLBACK")
            report.warnings.append(f"入库失败: {exc}")
            logger.exception("index_bar 入库异常")
        return [report]

    def sync_industry_map(self, adapter: DataAdapter) -> SyncReport:
        """行业映射回填：UPDATE dim_stock.industry（非整表同步，只动 industry 一列）。

        - 仅回填 dim_stock 已存在的代码；映射中多出的代码跳过并计数；
        - dim_stock 中未命中映射的标的保留原 industry 值（不清空），计数告警；
        - 映射按 code 去重（同 code 后值覆盖前值，适配器侧已保证唯一）。
        """
        report = SyncReport(code="__all__", capability=Capability.INDUSTRY_MAP.value)
        try:
            df = adapter.fetch_industry_map()
        except DataSourceError as exc:
            report.warnings.append(f"数据源错误: {exc}")
            return report

        if df.empty:
            report.warnings.append("行业映射为空，未做任何回填")
            return report

        mapping: dict[str, str] = {}
        for row in df.itertuples(index=False):
            code = str(getattr(row, "code", "") or "").strip()
            industry = str(getattr(row, "industry", "") or "").strip()
            if code and industry:
                mapping[code] = industry
        if not mapping:
            report.warnings.append("行业映射有效行为 0，未做任何回填")
            return report

        existing = {r[0] for r in self.conn.execute("SELECT code FROM dim_stock").fetchall()}
        updates = [(industry, code) for code, industry in mapping.items() if code in existing]
        skipped = len(mapping) - len(updates)
        unmatched = len(existing - set(mapping))

        self.conn.execute("BEGIN TRANSACTION")
        try:
            self.conn.executemany("UPDATE dim_stock SET industry = ? WHERE code = ?", updates)
            self.conn.execute("COMMIT")
        except Exception as exc:
            self.conn.execute("ROLLBACK")
            report.warnings.append(f"回填失败: {exc}")
            logger.exception("industry_map 回填异常")
            return report

        report.rows_updated = len(updates)
        if skipped:
            report.warnings.append(f"映射中 {skipped} 个代码不在 dim_stock，已跳过")
        if unmatched:
            report.warnings.append(f"dim_stock 中 {unmatched} 只未命中行业映射，industry 保留原值")
        return report

    def sync_share_structure(self, adapter: DataAdapter) -> SyncReport:
        """股本结构全量同步：东财 F10 股本变动历史 -> fact_share_structure。

        整表 DELETE + INSERT（源为全市场快照流水，全量刷新语义最简）；
        仅保留 dim_stock 中已存在的代码（债券/指数等非股票噪声跳过并计数）。
        拉取失败时不动库内现有数据（先取全量、后事务写入）。
        """
        report = SyncReport(code="__all__", capability=Capability.SHARE_STRUCTURE.value)
        try:
            df = adapter.fetch_share_structure()
        except DataSourceError as exc:
            report.warnings.append(f"数据源错误: {exc}")
            return report

        if df.empty:
            report.warnings.append("股本结构数据为空，未做任何写入")
            return report

        existing = {r[0] for r in self.conn.execute("SELECT code FROM dim_stock").fetchall()}
        hit = df[df["code"].isin(existing)].reset_index(drop=True)
        skipped = len(df) - len(hit)
        if hit.empty:
            report.warnings.append("股本结构无命中 dim_stock 代码，未写入")
            if skipped:
                report.warnings.append(f"股本结构中 {skipped} 行不在 dim_stock，已跳过")
            return report

        schema = SCHEMAS[Capability.SHARE_STRUCTURE]
        self.conn.execute("BEGIN TRANSACTION")
        try:
            self.conn.execute("DELETE FROM fact_share_structure")
            report.rows_inserted = _insert_df(self.conn, "fact_share_structure", schema, hit)
            self.conn.execute("COMMIT")
        except Exception as exc:
            self.conn.execute("ROLLBACK")
            report.warnings.append(f"入库失败: {exc}")
            logger.exception("share_structure 入库异常")
            return report

        if skipped:
            report.warnings.append(f"股本结构中 {skipped} 行不在 dim_stock，已跳过")
        return report

    def _sync_financial(
        self,
        cfg: SyncConfig,
        cap: Capability,
        code: str,
        end_date: date,
    ) -> list[SyncReport]:
        report = SyncReport(code=code, capability=cap.value)

        # financial 不支持 start/end，只支持 periods 或全量；
        # 按 data.sync.financial_lookback_years 生成近 N 年季度报告期列表拉取
        # （价值轨 ROIC 持续年限筛选依赖历史长度）→ DELETE + INSERT 幂等
        periods = _quarterly_periods(end_date, self.financial_lookback_years)
        try:
            df = cfg.adapter.fetch_financial(code, periods=periods)
        except DataSourceError as exc:
            report.warnings.append(f"数据源错误: {exc}")
            return [report]

        if df.empty:
            return [report]

        # 前视偏差硬过滤
        lookahead_warnings = _filter_lookahead(df, cap)
        report.warnings.extend(lookahead_warnings)
        if df.empty:
            return [report]

        # 财务不做日级缺口检测：季报披露间隔 ~92 天是业务固有，
        # 套用日级窗口会对每期误报「缺口超窗」（全市场量级下淹没运维日志）

        schema = SCHEMAS[cap]
        table = _CAP_TABLE[cap]
        self.conn.execute("BEGIN TRANSACTION")
        try:
            _delete_range(self.conn, table, schema, df)
            report.rows_inserted = _insert_df(self.conn, table, schema, df)
            self.conn.execute("COMMIT")
        except Exception as exc:
            self.conn.execute("ROLLBACK")
            report.warnings.append(f"入库失败: {exc}")
            logger.exception("financial 入库异常")
        return [report]

    def _sync_dividend(self, cfg: SyncConfig, cap: Capability, code: str) -> list[SyncReport]:
        report = SyncReport(code=code, capability=cap.value)

        # dividend 不支持 start/end，adapter.fetch_dividend(code) 返回全部
        try:
            df = cfg.adapter.fetch_dividend(code)
        except DataSourceError as exc:
            report.warnings.append(f"数据源错误: {exc}")
            return [report]

        if df.empty:
            return [report]

        # ex_date ≤ pay_date 硬约束 / 未来 ex_date 告警
        div_warnings = _filter_lookahead(df, cap)
        report.warnings.extend(div_warnings)
        if df.empty:
            return [report]

        schema = SCHEMAS[cap]
        table = _CAP_TABLE[cap]
        self.conn.execute("BEGIN TRANSACTION")
        try:
            _delete_range(self.conn, table, schema, df)
            report.rows_inserted = _insert_df(self.conn, table, schema, df)
            self.conn.execute("COMMIT")
        except Exception as exc:
            self.conn.execute("ROLLBACK")
            report.warnings.append(f"入库失败: {exc}")
            logger.exception("dividend 入库异常")
        return [report]

    def _sync_fx_rate(
        self,
        cfg: SyncConfig,
        cap: Capability,
        pair: str,
        start_override: date | None,
        end_date: date,
    ) -> list[SyncReport]:
        report = SyncReport(code=pair, capability=cap.value)

        table = _CAP_TABLE[cap]
        date_col = _CAP_DATE_COL[cap]
        latest = _latest_in_db(self.conn, table, date_col, pair)

        if start_override is not None:
            start = start_override
        elif latest is not None:
            start = latest + timedelta(days=1)
        else:
            start = end_date - timedelta(days=self.daily_lookback_days)

        if start > end_date:
            report.warnings.append(f"无新数据：起点 {start} > 终点 {end_date}")
            return [report]

        try:
            df = cfg.adapter.fetch_fx_rate(pair, start, end_date)
        except DataSourceError as exc:
            report.warnings.append(f"数据源错误: {exc}")
            return [report]

        if df.empty:
            return [report]

        gaps, gap_warnings = _detect_gaps(
            df["trade_date"], lookback_limit=self.lookback_days_on_gap
        )
        report.gaps_found = gaps
        report.warnings.extend(gap_warnings)

        schema = SCHEMAS[cap]
        self.conn.execute("BEGIN TRANSACTION")
        try:
            _delete_range(self.conn, table, schema, df)
            report.rows_inserted = _insert_df(self.conn, table, schema, df)
            self.conn.execute("COMMIT")
        except Exception as exc:
            self.conn.execute("ROLLBACK")
            report.warnings.append(f"入库失败: {exc}")
            logger.exception("fx_rate 入库异常")
        return [report]

    # ---------------- 估值重算 ----------------

    def recompute_valuation_all(self, chunk_size: int = 500) -> int:
        """全量重算 fact_valuation（总股本接入后的历史回填入口）。

        按 fact_daily_bar 标的清单分块调用 _recompute_valuation，单块内存可控；
        返回处理标的数。逐块独立事务，单块失败不阻断后续块。
        """
        codes = [
            r[0]
            for r in self.conn.execute(
                "SELECT DISTINCT code FROM fact_daily_bar ORDER BY code"
            ).fetchall()
        ]
        total_chunks = (len(codes) + chunk_size - 1) // chunk_size
        for i in range(0, len(codes), chunk_size):
            chunk = codes[i : i + chunk_size]
            logger.info(
                "估值全量重算：块 %d/%d（%s..%s）",
                i // chunk_size + 1,
                total_chunks,
                chunk[0],
                chunk[-1],
            )
            self._recompute_valuation(chunk)
        return len(codes)

    def _recompute_valuation(self, codes: list[str]) -> None:
        """同步完基础数据后重算 fact_valuation。

        从 DuckDB 读最新 5 张表 → 调 ValuationEngine.compute() → 写回 fact_valuation。
        """
        try:
            from viqtrader.data import valuation as _valuation  # 延迟 import 避免循环
        except ImportError:
            logger.info("valuation.py 不可用，跳过估值重算")
            return

        if not codes:
            return

        # 从 DuckDB 拉取最新数据
        codes_placeholder = ",".join(["?"] * len(codes))
        bar_df = self.conn.execute(
            f"SELECT * FROM fact_daily_bar WHERE code IN ({codes_placeholder})",
            codes,
        ).df()
        fin_df = self.conn.execute(
            f"SELECT * FROM fact_financial WHERE code IN ({codes_placeholder})",
            codes,
        ).df()
        div_df = self.conn.execute(
            f"SELECT * FROM fact_dividend WHERE code IN ({codes_placeholder})",
            codes,
        ).df()
        fx_df = self.conn.execute("SELECT * FROM fact_fx_rate").df()
        dim_df = self.conn.execute("SELECT * FROM dim_stock").df()
        shares_df = self.conn.execute(
            "SELECT code, change_date, total_shares FROM fact_share_structure"
        ).df()

        if bar_df.empty:
            return

        # 估值引擎需要 ann_date 等列为 datetime；DuckDB DATE 读回来是 date 或 datetime
        for df_to_cast, date_cols in [
            (fin_df, ["report_period", "ann_date"]),
            (div_df, ["ex_date", "pay_date"]),
            (bar_df, ["trade_date"]),
            (fx_df, ["trade_date"]),
            (shares_df, ["change_date"]),
        ]:
            for col in date_cols:
                if col in df_to_cast.columns:
                    df_to_cast[col] = pd.to_datetime(df_to_cast[col])

        # 调估值引擎（股本未同步时 shares_df 为空帧 → 引擎内等价 None，留 NaN）
        result = _valuation.compute(
            bar_df,
            fin_df,
            div_df,
            fx_df,
            dim_df,
            config=self.config or {},
            shares_df=shares_df if not shares_df.empty else None,
        )
        if result.empty:
            return

        self.conn.execute("BEGIN TRANSACTION")
        try:
            # DELETE + INSERT
            _delete_range_valuation(self.conn, result, codes)
            _insert_df_valuation(self.conn, result)
            self.conn.execute("COMMIT")
            logger.info("估值重算完成：%d 行", len(result))
        except Exception:
            self.conn.execute("ROLLBACK")
            logger.exception("估值重算入库失败")


def _delete_range_valuation(
    conn: duckdb.DuckDBPyConnection, df: pd.DataFrame, codes: list[str]
) -> None:
    """fact_valuation 的 DELETE：按 code + trade_date 范围。"""
    if df.empty:
        return
    codes_ph = ",".join(["?"] * len(codes))
    dates = pd.to_datetime(df["trade_date"]).dropna()
    if not dates.empty:
        conn.execute(
            f"DELETE FROM fact_valuation WHERE code IN ({codes_ph}) AND trade_date BETWEEN ? AND ?",
            [*codes, dates.min().date(), dates.max().date()],
        )


def _insert_df_valuation(conn: duckdb.DuckDBPyConnection, df: pd.DataFrame) -> int:
    """写 fact_valuation。"""
    if df.empty:
        return 0
    cols = [
        "code",
        "trade_date",
        "pe_ttm",
        "pb",
        "ps_ttm",
        "dv_ratio",
        "market_cap",
        "pe_percentile",
    ]
    out = df[[c for c in cols if c in df.columns]].copy()
    out["trade_date"] = pd.to_datetime(out["trade_date"]).dt.date
    tuples = list(out.itertuples(index=False, name=None))
    if not tuples:
        return 0
    placeholders = ",".join(["?"] * len(out.columns))
    conn.executemany(
        f"INSERT INTO fact_valuation ({','.join(out.columns)}) VALUES ({placeholders})",
        tuples,
    )
    return len(tuples)
