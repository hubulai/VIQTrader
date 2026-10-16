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

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from viqtrader.config.loader import Config  # noqa: E402
from viqtrader.data.adapters.qmt import QMTAdapter  # noqa: E402
from viqtrader.data.base import Capability  # noqa: E402
from viqtrader.data.sync import IncrementalSyncer, SyncConfig  # noqa: E402
from viqtrader.store.db import connect  # noqa: E402
from viqtrader.store.migrate import migrate  # noqa: E402

logger = logging.getLogger("sync_market")

CHUNK_SIZE = 200  # 每块代码数：兼顾进度可见性与单块失败止损


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


def main() -> int:
    parser = argparse.ArgumentParser(description="全市场名单+日K同步（QMT）")
    parser.add_argument("--markets", default="SH,SZ,BJ", help="逗号分隔市场（默认 SH,SZ,BJ）")
    parser.add_argument("--list-only", action="store_true", help="只同步名单，不拉日 K")
    parser.add_argument("--limit", type=int, default=None, help="只同步前 N 只（调试用）")
    args = parser.parse_args()
    markets = [m.strip().upper() for m in args.markets.split(",") if m.strip()]
    mk_in = "','".join(markets)  # 已白名单化，仅 SH/SZ/BJ 字面量拼接

    config = Config.load(REPO_ROOT / "config")
    log_file = _setup_logging(config)
    logger.info(
        "===== sync_market 启动 markets=%s list_only=%s limit=%s 日志=%s",
        markets,
        args.list_only,
        args.limit,
        log_file,
    )

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
    codes = [
        r[0]
        for r in conn.execute(
            f"SELECT code FROM dim_stock WHERE market IN ('{mk_in}') ORDER BY code"
        ).fetchall()
    ]
    if args.limit:
        codes = codes[: args.limit]
    logger.info("[日K] 待同步 %d 只，分块 %d，增量起点=库内最后日期+1", len(codes), CHUNK_SIZE)

    t_start = time.time()
    done = rows_total = warn_total = 0
    failed: list[str] = []
    for chunk_no, i in enumerate(range(0, len(codes), CHUNK_SIZE), start=1):
        chunk = codes[i : i + CHUNK_SIZE]
        t_chunk = time.time()
        chunk_reports = syncer.sync(
            SyncConfig(codes=chunk, adapter=adapter, capabilities=[Capability.DAILY_BAR])
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
