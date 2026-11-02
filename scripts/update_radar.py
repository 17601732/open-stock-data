"""顶底雷达每日流水线: 抓取 → 指标 → 综合分 → 事件研究 → 快照。

用法:
    uv run python -m scripts.update_radar                # 每日增量
    uv run python -m scripts.update_radar --full         # 全量重建(保留已有原始数据)
    uv run python -m scripts.update_radar --skip-stocks  # 只更新聚合类指标
"""

from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

import pandas as pd

from scripts.radar import backtest, build_snapshot, fetch_inputs, indicators, store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("radar")


def run(full: bool = False, skip_stocks: bool = False, with_holders: bool = False,
        android_assets: Path | None = None) -> dict:
    """执行完整流水线, 返回快照字典。"""
    t0 = time.time()
    logger.info("1/4 抓取输入数据")
    fetch_inputs.fetch_all(full=full, with_holders=with_holders, skip_stocks=skip_stocks)

    logger.info("2/4 计算指标与综合分")
    ind = indicators.build_indicators()
    if ind.empty:
        raise RuntimeError("指标为空: 原始数据不足, 请检查 data/radar/fetch_status.json")
    ind, anomaly_flags = indicators.screen_anomalies(ind)
    if anomaly_flags:
        logger.info("异常值筛查: %d 个可疑点已置为缺失", len(anomaly_flags))
    comp, detail = indicators.build_composite(ind)
    if comp.empty:
        raise RuntimeError("综合分为空: 可用指标权重覆盖率不足")
    signals = indicators.extract_signals(comp)
    logger.info("综合分 %s ~ %s, 信号 top=%d bottom=%d",
                str(comp.date.min())[:10], str(comp.date.max())[:10],
                int((signals.type == "top").sum()) if len(signals) else 0,
                int((signals.type == "bottom").sum()) if len(signals) else 0)

    logger.info("3/4 事件研究回测")
    idx = store.read_frame("index_daily.parquet")
    bench = idx[idx.code == benchmark_code()].copy()
    bench["date"] = pd.to_datetime(bench["date"])
    px = bench.set_index("date")["close"].sort_index()
    bt = backtest.run_event_study(signals, px, detail)

    logger.info("4/4 导出快照")
    snap = build_snapshot.build_snapshot(comp, detail, signals, bt, ind, anomalies=anomaly_flags)
    path = build_snapshot.write_snapshot(snap, android_assets)
    logger.info("快照 %s (%.0f KB), 耗时 %.0fs", path, path.stat().st_size / 1024, time.time() - t0)
    return snap


def benchmark_code() -> str:
    """基准指数代码。"""
    from scripts.radar.config import RADAR_CONFIG

    return RADAR_CONFIG["backtest"]["benchmark"]


def main() -> None:
    """CLI 入口。"""
    ap = argparse.ArgumentParser(description="A股顶底雷达流水线")
    ap.add_argument("--full", action="store_true", help="全量重建")
    ap.add_argument("--skip-stocks", action="store_true", help="跳过个股明细(仅聚合指标)")
    ap.add_argument("--with-holders", action="store_true", help="抓取股东增减持(较慢)")
    ap.add_argument("--android-assets", type=Path, default=None, help="同步快照到 Android assets 目录")
    args = ap.parse_args()
    run(full=args.full, skip_stocks=args.skip_stocks, with_holders=args.with_holders,
        android_assets=args.android_assets)


if __name__ == "__main__":
    main()
