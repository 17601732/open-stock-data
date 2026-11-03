"""除权除息日历: 用东财分红送配的批量数据(按报告期一次取全市场)给 TDX 不复权价格打标。

为什么需要: hsjday.zip 的 .day 是不复权价, 除权日(分红/送转)会造成价格向下跳空。
跨除权日的收益率、均线、N 日累计涨幅都会被污染; 而 TDX 包内不含权息资料,
仅靠「跌幅异常」猜测会把真实暴跌误判为除权(实测精确率 0/18, 召回率 0/37)。
因此改用披露数据里的「除权除息日」作为权威日历。

数据源: akshare stock_fhps_em(date=报告期), 每个报告期一次请求返回全市场,
仅保留「方案进度=实施分配」且有除权除息日的记录。缓存 30 天(除权日会提前公告)。

产物 data/radar/ex_rights.parquet: code / ex_date(YYYY-MM-DD) / cash_ratio / bonus_ratio
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pandas as pd

from scripts.radar import store

logger = logging.getLogger(__name__)

TTL_DAYS = 30
YEARS_BACK = 5


def _periods() -> list[str]:
    """报告期: 每年 0331/0630/0930/1231。"""
    now = datetime.now(UTC)
    out = []
    for y in range(now.year - YEARS_BACK, now.year + 1):
        for md in ("1231", "0630", "0930", "0331"):
            p = f"{y}{md}"
            if p <= now.strftime("%Y%m%d"):
                out.append(p)
    return sorted(out)


def fetch_ex_rights(force: bool = False) -> pd.DataFrame:
    """抓取除权除息日历(缓存 30 天)。"""
    existing = store.read_frame("ex_rights.parquet")
    if not existing.empty and not force:
        fetched = pd.to_datetime(existing["fetched_at"].iloc[0], errors="coerce")
        if pd.notna(fetched) and (datetime.now(UTC).replace(tzinfo=None) - fetched).days < TTL_DAYS:
            logger.info("复用除权日历 %d 条", len(existing))
            return existing

    import akshare as ak

    rows = []
    errors = []
    for p in _periods():
        try:
            d = ak.stock_fhps_em(date=p)
            d = d[d["除权除息日"].notna()]
            if "方案进度" in d.columns:
                d = d[d["方案进度"].astype(str).str.contains("实施", na=False)]
            for _, r in d.iterrows():
                rows.append({
                    "code": str(r["代码"]).zfill(6),
                    "ex_date": str(r["除权除息日"])[:10],
                    "cash_ratio": r.get("现金分红-现金分红比例"),
                    "bonus_ratio": r.get("送转股份-送转总比例"),
                    "period": p,
                })
        except Exception as exc:
            errors.append(f"{p}:{str(exc)[:50]}")
            logger.warning("分红送配 %s 抓取失败: %s", p, str(exc)[:80])
    df = pd.DataFrame(rows)
    if df.empty:
        store.update_status("ex_rights", status="failed", reason=" | ".join(errors)[:160])
        raise RuntimeError("除权日历为空 " + " | ".join(errors))
    df = df.drop_duplicates(["code", "ex_date"]).reset_index(drop=True)
    df["fetched_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    store.write_frame("ex_rights.parquet", df)
    store.update_status("ex_rights", status="ok", rows=len(df), codes=int(df["code"].nunique()),
                        start=str(df["ex_date"].min()), end=str(df["ex_date"].max()),
                        periods=len(_periods()), failed=len(errors),
                        note="仅'实施分配'且有除权除息日的记录; 用于 TDX 不复权价格打标")
    logger.info("除权日历 %d 条, %d 只, %s ~ %s", len(df), df["code"].nunique(),
                df["ex_date"].min(), df["ex_date"].max())
    return df


def apply_flags(stock: pd.DataFrame, breadth: pd.DataFrame | None = None) -> pd.DataFrame:
    """给个股明细打 ex_rights 标: 优先权威日历, 缺失时退回启发式跳空检测。"""
    from scripts.radar.fetch_tdx import detect_ex_rights

    cal = store.read_frame("ex_rights.parquet")
    if not cal.empty:
        iso = pd.to_datetime(stock["date"].astype(str), format="%Y%m%d").dt.strftime("%Y-%m-%d")
        key = pd.DataFrame({"code": stock["code"].values, "date": iso.values, "hit": True})
        merged = key.merge(cal[["code", "ex_date"]].assign(hit=True),
                           left_on=["code", "date"], right_on=["code", "ex_date"], how="left")
        stock = stock.copy()
        stock["ex_rights"] = merged["hit_y"].fillna(False).to_numpy()
        source = "eastmoney_fhps"
    else:
        stock = stock.copy()
        stock["ex_rights"] = detect_ex_rights(stock, breadth).to_numpy()
        source = "heuristic_gap"
    store.update_status("ex_rights_flags", flag_source=source,
                        flagged=int(stock["ex_rights"].sum()), rows=len(stock))
    return stock


def main() -> None:
    """CLI 入口。"""
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="抓取除权除息日历")
    ap.add_argument("--force", action="store_true", help="忽略缓存")
    args = ap.parse_args()
    fetch_ex_rights(force=args.force)


if __name__ == "__main__":
    main()
