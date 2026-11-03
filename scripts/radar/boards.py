"""板块成分数据: 行业板块成员名单(个股 → 板块)。

数据源实测结论:
- hsjday.zip 内**不含**板块/成分文件(仅 sh|sz|bj/lday/*.day), 必须外部取。
- 通达信本地 blocknew/tdxhy 需要客户端目录, Actions 无法获取。
- 主源: 申万一级行业(sw_index_first_info + index_stock_cons), 31 个行业, 覆盖沪深主板/
  创业板/科创板, 不含北交所; 实测覆盖全部活跃 A 股约 90%(北交所 ~350 只未覆盖)。
- 回退: 东方财富行业板块(约 86 个行业, push2 对单 IP 限流) → 新浪行业板块(49 个粗行业,
  实测仅覆盖 30% 活跃个股, 只作最后兜底)。
- 题材(概念)板块未采用: 东财/同花顺概念成分大量重叠、每日变动且无稳定历史,
  无法构建可回测的干净映射, 因此只交付行业板块版本, 题材部分明确标注受限。

产物 data/radar/board_members.parquet: code / board / source / fetched_at
成员名单是快照(非时间序列), 按当前成分回溯历史, 存在成分漂移偏差(已在元数据标注)。
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime

import pandas as pd

from scripts.radar import store

logger = logging.getLogger(__name__)

BOARD_TTL_DAYS = 7


def _sw_members() -> pd.DataFrame:
    """申万一级行业成员(实测覆盖沪深主板/创业板/科创板, 不含北交所)。"""
    import akshare as ak

    info = ak.sw_index_first_info()
    rows = []
    for _, r in info.iterrows():
        code = str(r["行业代码"]).split(".")[0]
        cons = ak.index_stock_cons(symbol=code)
        for c, n in zip(cons["品种代码"], cons["品种名称"], strict=False):
            rows.append({"code": str(c).zfill(6), "board": str(r["行业名称"]), "name": n})
    df = pd.DataFrame(rows)
    if df.empty:
        raise ValueError("申万行业成员为空")
    df["source"] = "sw_industry"
    return df


def _em_members() -> pd.DataFrame:
    """东方财富行业板块成员(回退源)。"""
    import akshare as ak

    names = ak.stock_board_industry_name_em()
    boards = [str(x) for x in names["板块名称"] if not str(x).startswith("昨日")]
    rows = []
    for b in boards:
        try:
            cons = ak.stock_board_industry_cons_em(symbol=b)
            for c, n in zip(cons["代码"], cons["名称"], strict=False):
                rows.append({"code": str(c).zfill(6), "board": b, "name": n})
        except Exception as exc:
            logger.warning("东财板块 %s 成员抓取失败: %s", b, str(exc)[:80])
        time.sleep(0.35)
    df = pd.DataFrame(rows)
    if df.empty:
        raise ValueError("东财行业板块成员为空")
    df["source"] = "em_industry"
    return df


def _sina_members() -> pd.DataFrame:
    """新浪行业板块成员(回退源)。"""
    import akshare as ak

    spot = ak.stock_sector_spot(indicator="新浪行业")
    rows = []
    for label, board in zip(spot["label"], spot["板块"], strict=False):
        try:
            det = ak.stock_sector_detail(sector=label)
            for c, n in zip(det["code"], det["name"], strict=False):
                rows.append({"code": str(c).zfill(6), "board": str(board), "name": n})
        except Exception as exc:
            logger.warning("新浪板块 %s 成员抓取失败: %s", label, str(exc)[:80])
    df = pd.DataFrame(rows)
    if df.empty:
        raise ValueError("新浪行业板块成员为空")
    df["source"] = "sina_industry"
    return df


def fetch_board_members(force: bool = False) -> pd.DataFrame:
    """获取个股→行业板块映射(缓存 7 天), 并记录覆盖率。"""
    existing = store.read_frame("board_members.parquet")
    if not existing.empty and not force:
        fetched = pd.to_datetime(existing["fetched_at"].iloc[0], errors="coerce")
        if pd.notna(fetched) and (datetime.now(UTC).replace(tzinfo=None) - fetched).days < BOARD_TTL_DAYS:
            logger.info("复用板块成员缓存 %d 行 (%s)", len(existing), existing["source"].iloc[0])
            stock = store.read_frame("tdx_stock.parquet")
            if not stock.empty:
                # 覆盖率按「最新交易日仍在交易」的口径计算, 避免把退市股算进分母
                latest = stock["date"].max()
                universe = set(stock.loc[stock["date"] == latest, "code"])
                covered = len(universe & set(existing["code"]))
                store.update_status("boards", status="ok", board_source=existing["source"].iloc[0],
                                    boards=int(existing["board"].nunique()),
                                    codes=int(existing["code"].nunique()),
                                    active_codes=len(universe), covered=covered,
                                    coverage=round(covered / max(len(universe), 1), 4),
                                    note="行业板块成员快照(非时间序列); 题材/概念成分未采用")
            return existing

    df = None
    errors = []
    for fn in (_sw_members, _em_members, _sina_members):
        try:
            df = fn()
            break
        except Exception as exc:
            errors.append(f"{fn.__name__}:{str(exc)[:60]}")
            logger.warning("板块来源 %s 失败: %s", fn.__name__, str(exc)[:120])
    if df is None:
        store.update_status("boards", status="failed", reason=" | ".join(errors))
        raise RuntimeError("板块成员全部来源失败 " + " | ".join(errors))

    df = df.drop_duplicates(["code", "board"]).reset_index(drop=True)
    df["fetched_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    store.write_frame("board_members.parquet", df)

    stock = store.read_frame("tdx_stock.parquet")
    universe = set(stock["code"].unique()) if not stock.empty else set()
    covered = len(universe & set(df["code"]))
    boards = df["board"].nunique()
    store.update_status(
        "boards", status="ok", board_source=df["source"].iloc[0], boards=int(boards),
        codes=int(df["code"].nunique()),
        coverage=round(covered / max(len(universe), 1), 4) if universe else None,
        universe=len(universe),
        note="行业板块成员快照(非时间序列); 题材/概念成分未采用(数据源重叠且不稳定)",
    )
    logger.info("板块成员 %d 行, %d 个行业, 覆盖个股 %d/%d",
                len(df), boards, covered, len(universe))
    return df


def main() -> None:
    """CLI 入口。"""
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="抓取行业板块成员")
    ap.add_argument("--force", action="store_true", help="忽略缓存")
    args = ap.parse_args()
    fetch_board_members(force=args.force)


if __name__ == "__main__":
    main()
