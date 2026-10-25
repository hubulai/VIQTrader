"""全市场数据同步：dim_stock 名单（QMT，含名称）+ 全 A 日 K 增量（QMT）。

P4.5 波段轨正式首跑前置：初筛需要 dim_stock 全量名单与名称（ST 过滤依赖
name NOT LIKE '%ST%'）以及全市场日线（60 根以上有效 K 线才能参与形态判定）。
名单与名称均来自 QMT（get_stock_list_in_sector + get_instrument_detail），
需 miniQMT 终端已启动登录。财务/分红不在本脚本范围（价值轨按观察池代码单独同步）。

用法（仓库根目录）：
    .venv\\Scripts\\python.exe scripts\\sync_market.py                 # 全市场
    .venv\\Scripts\\python.exe scripts\\sync_market.py --markets SH,SZ  # 限定市场
    .venv\\Scripts\\python.exe scripts\\sync_market.py --list-only      # 只同步名单
    .venv\\Scripts\\python.exe scripts\\sync_market.py --limit 20       # 冒烟调试
    .venv\\Scripts\\python.exe scripts\\sync_market.py --industry       # 行业回填（AkShare）
    .venv\\Scripts\\python.exe scripts\\sync_market.py --index          # 基准指数日线（AkShare）
    .venv\\Scripts\\python.exe scripts\\sync_market.py --index --index-start 2015-01-01
    .venv\\Scripts\\python.exe scripts\\sync_market.py --shares         # 股本结构 + 估值全量重算
    .venv\\Scripts\\python.exe scripts\\sync_market.py --financial      # 财务 5 表（QMT，近 10 年）
    .venv\\Scripts\\python.exe scripts\\sync_market.py --dividend       # A股分红（AkShare 巨潮）

--industry / --index / --shares / --dividend 走 AkShare/东财（不依赖 miniQMT 终端），
可单独或同时指定；指定后跳过 QMT 名单/日K流程。行业映射 UPDATE 回填 dim_stock.industry
（P4.6 Brinson 行业段依赖）；指数日线写入 fact_index_bar（默认代码取
config value_track.benchmark_code，--index-start 可强制全量回拉起点）；
--shares 拉东财 F10 股本变动历史入 fact_share_structure（约 32 万行 / 650 页），
完成后自动分块全量重算 fact_valuation（market_cap/ps_ttm 历史回填）；
--dividend 逐只拉巨潮分红配送入 fact_dividend（适配器内置限流，dv_ratio 依赖）。

--financial 走 QMT（需 miniQMT 终端）：库内 dim_stock 全市场分块拉财务 5 表，
回拉年数取 config data.sync.financial_lookback_years（默认 10 年，价值轨 ROIC
持续年限筛选依赖）；与 --dividend 同指定时先分红后财务串行（DuckDB 单写者约束）。
财务/分红假定名单已就位（codes 取库内 dim_stock），每块同步完自动重算该块估值。

特性：
- 日 K 增量（库内最后日期 +1 起，新标的默认回拉 daily_lookback_days）；
- 分块同步 + 逐块进度打印；整块失败（终端掉线特征）即中止，避免逐码重试空耗；
- 重跑安全：名单全量覆盖、日 K 按主键幂等。

运维日志（问题定位）：
- 每次运行生成独立日志文件 <data_dir>/logs/sync/sync_market-YYYYmmdd_HHMMSS.log，
  控制台同步输出；FileHandler 逐条落盘，后台/重定向运行不丢进度；
- 进度日志每块一行：块号、首末代码、新增行数、警告数、速率、预计剩余；
- 有警告逐条 WARNING（代码+原因）、失败逐个 ERROR、整块失败 CRITICAL，
  未捕获异常 logger.exception 带完整堆栈；
- root logger 配置，viqtrader.data.sync 内部的 logger.exception 也进同一文件。
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from viqtrader.config.loader import Config  # noqa: E402
from viqtrader.data.adapters.akshare import AkShareAdapter  # noqa: E402
from viqtrader.data.adapters.qmt import QMTAdapter  # noqa: E402
from viqtrader.data.base import Capability, DataAdapter  # noqa: E402
from viqtrader.data.sync import IncrementalSyncer, SyncConfig  # noqa: E402
from viqtrader.store.db import connect  # noqa: E402
from viqtrader.store.migrate import migrate  # noqa: E402

logger = logging.getLogger("sync_market")

CHUNK_SIZE = 200  # 每块代码数：兼顾进度可见性与单块失败止损
FIN_CHUNK_SIZE = 100  # 财务逐只异步下载较慢，块小一点进度更可见


def _codes_from_db(conn: duckdb.DuckDBPyConnection, markets: list[str]) -> list[str]:
    """库内 dim_stock 按市场过滤取代码（财务/分红假定名单已就位）。"""
    mk_in = "','".join(m.strip().upper() for m in markets)  # 仅 SH/SZ/BJ 字面量
    return [
        r[0]
        for r in conn.execute(
            f"SELECT code FROM dim_stock WHERE market IN ('{mk_in}') ORDER BY code"
        ).fetchall()
    ]


def _sync_in_chunks(
    syncer: IncrementalSyncer,
    adapter: DataAdapter,
    codes: list[str],
    capability: Capability,
    *,
    chunk_size: int,
    tag: str,
) -> tuple[int, int, list[str]]:
    """分块同步通用骨架：进度日志 + 整块失败（终端/数据源掉线）即中止。

    返回 (插入总行数, 告警总条数, 失败代码清单)。每块 sync 完自动重算该块估值。
    """
    rows_total = warn_total = 0
    failed: list[str] = []
    for chunk_no, i in enumerate(range(0, len(codes), chunk_size), start=1):
        chunk = codes[i : i + chunk_size]
        t_chunk = time.time()
        chunk_reports = syncer.sync(
            SyncConfig(codes=chunk, adapter=adapter, capabilities=[capability])
        )
        by_code = {r.code: r for r in chunk_reports}
        chunk_failed = [c for c in chunk if any("数据源错误" in w for w in by_code[c].warnings)]
        for r in chunk_reports:  # 逐条告警落日志：事后可精确定位到代码与原因
            for w in r.warnings:
                logger.warning("[%s] %s %s", tag, r.code, w)
        chunk_rows = sum(r.rows_inserted for r in chunk_reports)
        chunk_warns = sum(len(r.warnings) for r in chunk_reports)
        done = i + len(chunk)
        rows_total += chunk_rows
        warn_total += chunk_warns
        failed.extend(chunk_failed)
        speed = len(chunk) / max(time.time() - t_chunk, 1e-6)
        eta = (len(codes) - done) / max(speed, 1e-6) / 60
        logger.info(
            "[%s] 块%d %s..%s 进度 %d/%d  本块 +%d 行 警告 %d  %.1f 只/s 预计剩 %.0f 分钟",
            tag,
            chunk_no,
            chunk[0],
            chunk[-1],
            done,
            len(codes),
            chunk_rows,
            chunk_warns,
            speed,
            eta,
        )
        if len(chunk_failed) >= len(chunk):  # 整块失败=连接异常，中止避免空耗
            logger.critical(
                "[%s] 块%d（%s..%s）整块失败：数据源连接异常，中止。请检查后重跑（幂等续传）",
                tag,
                chunk_no,
                chunk[0],
                chunk[-1],
            )
            break
    return rows_total, warn_total, failed


def _setup_logging(config: Config) -> Path:
    """配置 root logger：文件（每次运行独立）+ 控制台双输出，返回日志文件路径。"""
    data_dir = Path(str(config.get("platform.data_dir", REPO_ROOT / "data")))
    log_dir = data_dir / "logs" / "sync"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"sync_market-{datetime.now():%Y%m%d_%H%M%S}.log"

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s %(message)s")
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(fmt)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(fmt)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)
    return log_file


def _run_akshare_side(args: argparse.Namespace, config: Config, markets: list[str]) -> int:
    """AkShare 侧任务：行业映射回填 / 基准指数日线 / 股本 / 分红（不依赖 miniQMT）。"""
    conn = connect(config)
    migrate(conn)
    adapter = AkShareAdapter()
    syncer = IncrementalSyncer(conn, config)

    if args.dividend:
        codes = _codes_from_db(conn, markets)
        if args.limit:
            codes = codes[: args.limit]
        if not codes:
            logger.error("[分红] dim_stock 为空：请先跑名单同步")
            return 2
        logger.info("[分红] 待同步 %d 只，分块 %d（巨潮限流内置）", len(codes), CHUNK_SIZE)
        t0 = time.time()
        rows_total, warn_total, failed = _sync_in_chunks(
            syncer, adapter, codes, Capability.DIVIDEND, chunk_size=CHUNK_SIZE, tag="分红"
        )
        stats = conn.execute("SELECT count(DISTINCT code), count(*) FROM fact_dividend").fetchone()
        logger.info(
            "[分红] 本次 +%d 行，警告 %d 条，失败 %d 只，库内 %d 只 / %d 行（%.1f 分钟）",
            rows_total,
            warn_total,
            len(failed),
            stats[0],
            stats[1],
            (time.time() - t0) / 60,
        )
        if failed:
            logger.info("[分红] 失败代码清单（%d）：%s", len(failed), ",".join(failed))

    if args.industry:
        t0 = time.time()
        report = syncer.sync_industry_map(adapter)
        for w in report.warnings:
            logger.warning("[行业] %s", w)
        filled = conn.execute(
            "SELECT count(*) FROM dim_stock WHERE industry IS NOT NULL AND industry <> ''"
        ).fetchone()[0]
        total = conn.execute("SELECT count(*) FROM dim_stock").fetchone()[0]
        logger.info(
            "[行业] 回填 %d 只，dim_stock 已有行业 %d/%d（%.1fs）",
            report.rows_updated,
            filled,
            total,
            time.time() - t0,
        )
        if report.rows_updated == 0 and any("数据源错误" in w for w in report.warnings):
            return 2

    if args.index:
        index_code = args.index_code or str(config.get("value_track.benchmark_code", "000300"))
        start_override = (
            datetime.strptime(args.index_start, "%Y-%m-%d").date() if args.index_start else None
        )
        t0 = time.time()
        reports = syncer.sync(
            SyncConfig(
                codes=[index_code],
                adapter=adapter,
                capabilities=[Capability.INDEX_BAR],
                start_date=start_override,
            )
        )
        for r in reports:
            for w in r.warnings:
                logger.warning("[指数] %s %s", r.code, w)
        rows = sum(r.rows_inserted for r in reports)
        stats = conn.execute(
            "SELECT count(*), min(trade_date), max(trade_date) FROM fact_index_bar WHERE code = ?",
            [index_code],
        ).fetchone()
        logger.info(
            "[指数] %s 本次 +%d 行，库内 %d 行（%s ~ %s，%.1fs）",
            index_code,
            rows,
            stats[0],
            stats[1],
            stats[2],
            time.time() - t0,
        )
        if stats[0] == 0:
            logger.error("[指数] fact_index_bar 仍为空：请检查数据源后重试")
            return 2

    if args.shares:
        t0 = time.time()
        report = syncer.sync_share_structure(adapter)
        for w in report.warnings:
            logger.warning("[股本] %s", w)
        stats = conn.execute(
            "SELECT count(*), count(DISTINCT code), max(change_date) FROM fact_share_structure"
        ).fetchone()
        logger.info(
            "[股本] 本次写入 %d 行，覆盖 %d 只，最新时点 %s（%.1fs）",
            report.rows_inserted,
            stats[1],
            stats[2],
            time.time() - t0,
        )
        if report.rows_inserted == 0 and any("数据源错误" in w for w in report.warnings):
            logger.error("[股本] 同步失败：请检查东财 datacenter 连通性后重试")
            return 2
        # 全量重算估值（market_cap/ps_ttm 历史回填，分块限内存）
        t1 = time.time()
        codes_done = syncer.recompute_valuation_all()
        filled = conn.execute(
            "SELECT count(*) FROM fact_valuation"
            " WHERE market_cap IS NOT NULL AND NOT isnan(market_cap)"
        ).fetchone()[0]
        total = conn.execute("SELECT count(*) FROM fact_valuation").fetchone()[0]
        logger.info(
            "[估值] 全量重算 %d 只完成，market_cap 非空 %d/%d 行（%.1fs）",
            codes_done,
            filled,
            total,
            time.time() - t1,
        )
    return 0


def _run_financial(args: argparse.Namespace, config: Config, markets: list[str]) -> int:
    """QMT 侧财务同步：库内 dim_stock 全市场分块拉 5 表（需 miniQMT 终端）。"""
    conn = connect(config)
    migrate(conn)
    adapter = QMTAdapter()
    syncer = IncrementalSyncer(conn, config)

    codes = _codes_from_db(conn, markets)
    if args.limit:
        codes = codes[: args.limit]
    if not codes:
        logger.error("[财务] dim_stock 为空：请先跑名单同步")
        return 2
    lookback = config.get("data.sync.financial_lookback_years", 10)
    logger.info(
        "[财务] 待同步 %d 只，分块 %d，回拉近 %s 年（季报口径）",
        len(codes),
        FIN_CHUNK_SIZE,
        lookback,
    )
    t_start = time.time()
    rows_total, warn_total, failed = _sync_in_chunks(
        syncer, adapter, codes, Capability.FINANCIAL, chunk_size=FIN_CHUNK_SIZE, tag="财务"
    )
    stats = conn.execute(
        "SELECT count(DISTINCT code), count(*), min(report_period), max(report_period) "
        "FROM fact_financial"
    ).fetchone()
    logger.info(
        "[财务] 本次 +%d 行，警告 %d 条，失败 %d 只，库内 %d 只 / %d 行（%s ~ %s，%.1f 分钟）",
        rows_total,
        warn_total,
        len(failed),
        stats[0],
        stats[1],
        stats[2],
        stats[3],
        (time.time() - t_start) / 60,
    )
    if failed:
        logger.info("[财务] 失败代码清单（%d）：%s", len(failed), ",".join(failed))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="全市场名单+日K同步（QMT）")
    parser.add_argument("--markets", default="SH,SZ,BJ", help="逗号分隔市场（默认 SH,SZ,BJ）")
    parser.add_argument("--list-only", action="store_true", help="只同步名单，不拉日 K")
    parser.add_argument("--limit", type=int, default=None, help="只同步前 N 只（调试用）")
    parser.add_argument(
        "--industry", action="store_true", help="行业映射回填 dim_stock.industry（AkShare）"
    )
    parser.add_argument(
        "--index", action="store_true", help="基准指数日线同步 fact_index_bar（AkShare）"
    )
    parser.add_argument("--index-code", default=None, help="指数代码（默认取 config 基准）")
    parser.add_argument("--index-start", default=None, help="指数回拉起点 YYYY-MM-DD（默认增量）")
    parser.add_argument(
        "--daily-start",
        default=None,
        help="个股日K强制回拉起点 YYYY-MM-DD（默认增量=库内最后日期+1，历史回补用）",
    )
    parser.add_argument(
        "--shares",
        action="store_true",
        help="股本结构同步 fact_share_structure + 估值全量重算（AkShare/东财）",
    )
    parser.add_argument(
        "--financial",
        action="store_true",
        help="财务 5 表同步 fact_financial（QMT，回拉年数取 config，需 miniQMT）",
    )
    parser.add_argument(
        "--dividend",
        action="store_true",
        help="A股分红同步 fact_dividend（AkShare 巨潮，限流内置）",
    )
    args = parser.parse_args()
    markets = [m.strip().upper() for m in args.markets.split(",") if m.strip()]
    mk_in = "','".join(markets)  # 已白名单化，仅 SH/SZ/BJ 字面量拼接

    config = Config.load(REPO_ROOT / "config")
    log_file = _setup_logging(config)
    logger.info(
        "===== sync_market 启动 markets=%s list_only=%s limit=%s industry=%s"
        " index=%s shares=%s financial=%s dividend=%s 日志=%s",
        markets,
        args.list_only,
        args.limit,
        args.industry,
        args.index,
        args.shares,
        args.financial,
        args.dividend,
        log_file,
    )

    if args.industry or args.index or args.shares or args.dividend:
        rc = _run_akshare_side(args, config, markets)
        if not args.financial:
            logger.info("===== AkShare 侧任务完成，退出码 %d", rc)
            return rc
        if rc != 0:
            logger.error("===== AkShare 侧任务失败（退出码 %d），中止后续财务同步", rc)
            return rc
        logger.info("===== AkShare 侧任务完成，继续 QMT 财务同步")

    if args.financial:
        rc = _run_financial(args, config, markets)
        logger.info("===== QMT 财务同步完成，退出码 %d", rc)
        return rc

    conn = connect(config)
    migrate(conn)
    adapter = QMTAdapter()
    syncer = IncrementalSyncer(conn, config)

    # ---- 1. 标的主数据：QMT 沪深京A股全量覆盖（名称经 get_instrument_detail 补全） ----
    t0 = time.time()
    reports = syncer.sync(
        SyncConfig(codes=[], adapter=adapter, capabilities=[Capability.STOCK_LIST])
    )
    for r in reports:
        for w in r.warnings:
            logger.warning("[名单] %s", w)
    total = conn.execute(f"SELECT count(*) FROM dim_stock WHERE market IN ('{mk_in}')").fetchone()[
        0
    ]
    logger.info(
        "[名单] dim_stock 目标市场 %s 共 %d 只（QMT，%.1fs）", markets, total, time.time() - t0
    )
    if total == 0:
        logger.error("名单为空：请确认 miniQMT 终端已启动登录后重试")
        return 2
    unnamed = conn.execute(
        f"SELECT count(*) FROM dim_stock WHERE market IN ('{mk_in}') AND name = ''"
    ).fetchone()[0]
    if unnamed:
        logger.warning("[名单] %d 只未取到名称（ST 过滤对空名放行，建议重跑补全）", unnamed)
    st_count = conn.execute(
        f"SELECT count(*) FROM dim_stock WHERE market IN ('{mk_in}') AND UPPER(name) LIKE '%ST%'"
    ).fetchone()[0]
    logger.info("[名单] 其中 ST 类 %d 只（初筛将排除）", st_count)

    if args.list_only:
        logger.info("===== list-only 完成，耗时 %.1fs", time.time() - t0)
        return 0

    # ---- 2. 全市场日 K 增量（分块，整块失败即中止） ----
    daily_start = (
        datetime.strptime(args.daily_start, "%Y-%m-%d").date() if args.daily_start else None
    )
    codes = [
        r[0]
        for r in conn.execute(
            f"SELECT code FROM dim_stock WHERE market IN ('{mk_in}') ORDER BY code"
        ).fetchall()
    ]
    if args.limit:
        codes = codes[: args.limit]
    logger.info(
        "[日K] 待同步 %d 只，分块 %d，%s",
        len(codes),
        CHUNK_SIZE,
        f"强制回拉起点={daily_start}" if daily_start else "增量起点=库内最后日期+1",
    )

    t_start = time.time()
    done = rows_total = warn_total = 0
    failed: list[str] = []
    for chunk_no, i in enumerate(range(0, len(codes), CHUNK_SIZE), start=1):
        chunk = codes[i : i + CHUNK_SIZE]
        t_chunk = time.time()
        chunk_reports = syncer.sync(
            SyncConfig(
                codes=chunk,
                start_date=daily_start,
                adapter=adapter,
                capabilities=[Capability.DAILY_BAR],
            )
        )
        by_code = {r.code: r for r in chunk_reports}
        chunk_failed = [c for c in chunk if any("数据源错误" in w for w in by_code[c].warnings)]
        for r in chunk_reports:  # 逐条告警落日志：事后可精确定位到代码与原因
            for w in r.warnings:
                logger.warning("[日K] %s %s", r.code, w)
        chunk_rows = sum(r.rows_inserted for r in chunk_reports)
        chunk_warns = sum(len(r.warnings) for r in chunk_reports)
        done += len(chunk)
        rows_total += chunk_rows
        warn_total += chunk_warns
        failed.extend(chunk_failed)
        speed = len(chunk) / max(time.time() - t_chunk, 1e-6)
        eta = (len(codes) - done) / max(speed, 1e-6) / 60
        logger.info(
            "[日K] 块%d %s..%s 进度 %d/%d  本块 +%d 行 警告 %d  %.1f 只/s 预计剩 %.0f 分钟",
            chunk_no,
            chunk[0],
            chunk[-1],
            done,
            len(codes),
            chunk_rows,
            chunk_warns,
            speed,
            eta,
        )
        if len(chunk_failed) >= len(chunk):  # 整块失败=终端掉线，中止避免空耗
            logger.critical(
                "[日K] 块%d（%s..%s）整块失败：QMT 连接异常，中止。请检查终端后重跑（增量续传）",
                chunk_no,
                chunk[0],
                chunk[-1],
            )
            break

    # ---- 3. 汇总 ----
    stats = conn.execute(
        "SELECT count(DISTINCT code), count(*), max(trade_date) FROM fact_daily_bar"
    ).fetchone()
    logger.info(
        "[完成] 本次插入 %d 行，警告 %d 条，失败 %d 只，总耗时 %.1f 分钟",
        rows_total,
        warn_total,
        len(failed),
        (time.time() - t_start) / 60,
    )
    if failed:
        logger.info("[完成] 失败代码清单（%d）：%s", len(failed), ",".join(failed))
    logger.info("[库况] 有日K标的 %d 只 / %d 行，最新交易日 %s", stats[0], stats[1], stats[2])
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        logger.exception("sync_market 未捕获异常")
        raise SystemExit(1) from None
