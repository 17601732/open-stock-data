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

from scripts.radar import backtest, build_snapshot, fetch_inputs, fetch_tdx, indicators, store, tech

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("radar")


def run(full: bool = False, skip_stocks: bool = False, with_holders: bool = False,
        android_assets: Path | None = None) -> dict:
    """执行完整流水线, 返回快照字典。"""
    t0 = time.time()
    # 缓存为空/残缺(如 Actions 首次运行、缓存丢失或只存了最近 60 天)时自动退化为全量:
    # 滚动分位需要每只指数至少 250 个交易日, 数据不够时综合分会为空
    if not full:
        idx_existing = store.read_frame("index_daily.parquet")
        if idx_existing.empty or len(idx_existing) < 600:
            full = True
            logger.warning("本地原始数据不足(指数 %d 行), 自动切换为全量模式", len(idx_existing))
    logger.info("1/4 抓取输入数据")
    try:
        fetch_inputs.fetch_all(full=full, with_holders=with_holders, skip_stocks=skip_stocks)
    except fetch_tdx.PackageNotUpdatedError as exc:
        # 通达信包发布晚于 cron: 包内还没到应到交易日时直接跳过, 不覆盖已有快照, 退出码 0
        logger.warning("package_not_updated: %s", exc)
        logger.warning("package_not_updated: 本次不落库、不覆盖快照, 等下一次 cron")
        return {"skipped": True, "reason": "package_not_updated"}

    logger.info("2/4 计算指标与综合分")
    ind = indicators.build_indicators()
    if ind.empty:
        raise RuntimeError("指标为空: 原始数据不足, 请检查 data/radar/fetch_status.json")
    ind, anomaly_flags = indicators.screen_anomalies(ind)
    if anomaly_flags:
        logger.info("异常值筛查: %d 个可疑点已置为缺失", len(anomaly_flags))
    comp, detail = indicators.build_composite(ind)
    if comp.empty:
        # 兜底: 增量数据不足以算分位时, 自动全量重算一次, 避免每日定时任务直接失败
        if not full:
            logger.warning("综合分为空, 自动回退全量重算")
            return run(full=True, skip_stocks=skip_stocks, with_holders=with_holders,
                       android_assets=android_assets)
        raise RuntimeError("综合分为空: 可用指标权重覆盖率不足")
    signals = indicators.extract_signals(comp)
    logger.info("综合分 %s ~ %s, 信号 top=%d bottom=%d",
                str(comp.date.min())[:10], str(comp.date.max())[:10],
                int((signals.type == "top").sum()) if len(signals) else 0,
                int((signals.type == "bottom").sum()) if len(signals) else 0)

    logger.info("3/4 事件研究回测 + 技术图层")
    idx = store.read_frame("index_daily.parquet")
    bench = idx[idx.code == benchmark_code()].copy()
    bench["date"] = pd.to_datetime(bench["date"])
    px = bench.set_index("date")["close"].sort_index()
    # 回测的价格序列同样截断到最后已完成交易日, 避免盘中未完成日进入前向收益
    px = px.loc[:comp["date"].max()]
    bt = backtest.run_event_study(signals, px, detail)

    tech_payload, tech_full = tech.build_tech(idx)
    abtest = tech.ab_test(comp, tech_full, px, detail)
    logger.info("技术分对照: %s", abtest.get("verdict"))

    logger.info("4/4 导出快照")
    snap = build_snapshot.build_snapshot(comp, detail, signals, bt, ind, anomalies=anomaly_flags,
                                         tech=tech_payload, abtest=abtest)
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
