"""50ETF 期权 PCR(兼容原有 option_pcr_50etf.csv 输出)。

修正: 原实现调用的 ak.option_sse_daily 在新版 AKShare 中已不存在,
改用上交所每日期权统计 option_daily_stats_sse, 并按原 CSV schema 输出。
"""

from __future__ import annotations

import logging

import pandas as pd

from scripts.radar import store
from scripts.radar.fetch_inputs import fetch_option_pcr

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def main() -> None:
    """生成 50ETF PCR CSV(保留原列名), 与合并版数据同源。"""
    df = fetch_option_pcr()
    if df.empty or "pcr_510050" not in df.columns or df["pcr_510050"].notna().sum() == 0:
        raise SystemExit("PCR 无有效数据(空表或无 pcr 列), 判为失败")
    out = pd.DataFrame({
        "date": df["date"],
        "pcr_vol": df["pcr_510050"].round(4),
        "pcr_oi": "",
        "put_vol": df["put_vol_510050"].round(0),
        "call_vol": df["call_vol_510050"].round(0),
        "put_oi": "",
        "call_oi": "",
    })
    path = store.DATA_DIR.parents[1] / "option_pcr_50etf.csv"
    out.to_csv(path, index=False)
    logger.info("已写入 %s (%d 行, %s ~ %s)", path, len(out), out.date.min(), out.date.max())


if __name__ == "__main__":
    main()
