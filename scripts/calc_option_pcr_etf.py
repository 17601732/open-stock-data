"""期权 PCR: 50ETF(510050) + 300ETF(510300) 认沽成交量 / 认购成交量。

沿用仓库既有 CSV 更新风格(增量补缺失交易日, 只保留真实可得区间)。
数据源: 上海证券交易所每日期权统计 AKShare option_daily_stats_sse。
PCR 历史覆盖有限(从 2019-12-23 起, 300ETF 期权上市日起), 缺失日期不做前向填充。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from scripts.radar import store
from scripts.radar.fetch_inputs import fetch_option_pcr

if TYPE_CHECKING:
    import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def report(df: pd.DataFrame) -> None:
    """打印覆盖率、缺口与最新分位, 便于人工核对。"""
    if df.empty:
        print("PCR 无数据")
        return
    print(f"PCR 区间: {df.date.min()} ~ {df.date.max()}, 共 {len(df)} 个交易日")
    idx = store.read_frame("index_daily.parquet")
    if not idx.empty:
        dates = sorted(d for d in idx[idx.code == "000300"].date.unique() if d >= "2019-12-23")
        have = set(df.date)
        missing = [d for d in dates if d not in have]
        print(f"相对交易日缺口: {len(missing)} 天" + (f", 示例 {missing[:5]}" if missing else ""))
    s = df.set_index("date")["pcr"]
    ma5 = s.rolling(5, min_periods=1).mean()
    pct = (s.rank(pct=True) * 100).iloc[-1]
    print(f"最新 PCR: {s.iloc[-1]:.4f}, 5日均: {ma5.iloc[-1]:.4f}, 全区间分位: {pct:.1f}")


def main() -> None:
    """抓取/更新并输出核对信息。"""
    df = fetch_option_pcr()
    report(df)


if __name__ == "__main__":
    main()
