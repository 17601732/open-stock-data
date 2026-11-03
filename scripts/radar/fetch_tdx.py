"""通达信盘后全量包(hsjday.zip)解析: 全市场个股 OHLCV + 逐日涨跌家数聚合。

数据源: https://data.tdx.com.cn/vipdoc/hsjday.zip (约 526MB, 解压约 909MB)
- 包内结构: sh/lday/*.day、sz/lday/*.day、bj/lday/*.day (每只证券一个 32 字节定长记录文件)
- .day 记录格式(小端, 实测):
    offset 0   int32   date     YYYYMMDD
    offset 4   int32   open     0.01 元
    offset 8   int32   high     0.01 元
    offset 12  int32   low      0.01 元
    offset 16  int32   close    0.01 元
    offset 20  float32 amount   元
    offset 24  int32   volume   股
    offset 28  int32   reserved (个股恒为 65536, 未使用)
- 价格口径: 不复权(与仓库 stock_daily 一致)。除权日会在不复权收盘价上产生跳空,
  本模块用东财分红送配的「除权除息日」打标 ex_rights(缺失时回退跳空启发式),
  供跨期收益计算排除样本。
- 解析产物:
    data/radar/tdx_breadth.parquet  逐日涨跌家数/上涨量/下跌量/成交额(全市场, 2014 起)
    data/radar/tdx_stock.parquet    近 DETAIL_DAYS 个交易日全市场个股 OHLCV(供形态计算)
  原始 zip 只进 Actions 缓存, 不入库、不提交。

CLI:
    python -m scripts.radar.fetch_tdx            # 增量(缓存 24h 内直接复用)
    python -m scripts.radar.fetch_tdx --force    # 强制重新下载
"""

from __future__ import annotations

import logging
import time
import zipfile
from datetime import datetime
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from scripts.radar import store

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

TDX_URL = "https://data.tdx.com.cn/vipdoc/hsjday.zip"
# 站点前置了 JS 反爬, 直接请求会得到 HTML; 携带该 cookie 才能拿到 zip(已实测 200/application/zip)
TDX_COOKIE = "__tst_status=1037432201#; EO_Bot_Ssid=2459828224"
TDX_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                 "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Referer": "https://data.tdx.com.cn/",
    "Accept": "application/zip,*/*",
}
MIN_ZIP_BYTES = 450 * 1024 * 1024
DETAIL_YEARS = 3           # 个股明细保留近 3 年(形态回测需要足够的跨期样本)

DAY_DTYPE = np.dtype([
    ("date", "<i4"), ("open", "<i4"), ("high", "<i4"), ("low", "<i4"),
    ("close", "<i4"), ("amount", "<f4"), ("volume", "<i4"), ("reserved", "<i4"),
])
RECORD_BYTES = DAY_DTYPE.itemsize  # 32

CACHE_DIR = store.DATA_DIR / "tdx"
CACHE_ZIP = CACHE_DIR / "hsjday.zip"


def _history_start_num() -> int:
    from scripts.radar.config import RADAR_CONFIG

    return int(str(RADAR_CONFIG["history_start"]).replace("-", ""))


# ---------------------------------------------------------------- 下载
def _download_once(dest: Path, headers: dict) -> None:
    """流式下载到 dest.part, 结束后改名; 支持 Range 断点续传。"""
    import requests

    part = dest.with_suffix(".part")
    pos = part.stat().st_size if part.exists() else 0
    h = dict(headers)
    if pos:
        h["Range"] = f"bytes={pos}-"
    with requests.get(TDX_URL, headers=h, stream=True, timeout=(10, 60)) as r:
        if pos and r.status_code == 200:
            logger.info("服务端不支持断点续传, 从头下载")
            pos = 0
            part.unlink(missing_ok=True)
        elif r.status_code != 206 and pos:
            r.raise_for_status()
        r.raise_for_status()
        total = int(r.headers.get("Content-Length") or 0) + pos
        mode = "ab" if pos else "wb"
        done = pos
        with open(part, mode) as fh:
            for chunk in r.iter_content(1024 * 1024):
                if not chunk:
                    continue
                fh.write(chunk)
                done += len(chunk)
                if done // (64 * 1024 * 1024) != (done - len(chunk)) // (64 * 1024 * 1024):
                    logger.info("下载 %.0f/%.0f MB", done / 1048576, total / 1048576)
    size = part.stat().st_size
    if size < MIN_ZIP_BYTES:
        raise ValueError(f"下载体积异常 {size} 字节, 疑似被反爬页替换")
    part.replace(dest)
    logger.info("下载完成 %.0f MB", size / 1048576)


def ensure_zip(force: bool = False) -> Path:
    """确保本地有可用的全量包; 24 小时内且体积正常则复用。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if CACHE_ZIP.exists() and not force:
        size = CACHE_ZIP.stat().st_size
        age = time.time() - CACHE_ZIP.stat().st_mtime
        if size >= MIN_ZIP_BYTES and age < 24 * 3600:
            logger.info("复用缓存 %s (%.0f MB, %.1f 小时前)", CACHE_ZIP, size / 1048576, age / 3600)
            return CACHE_ZIP
    errors = []
    for name, hdrs in (("cookie", {**TDX_HEADERS, "Cookie": TDX_COOKIE}), ("plain", TDX_HEADERS)):
        try:
            t0 = time.time()
            _download_once(CACHE_ZIP, hdrs)
            logger.info("TDX 包下载成功(方式=%s, %.1fs)", name, time.time() - t0)
            return CACHE_ZIP
        except Exception as exc:
            errors.append(f"{name}:{str(exc)[:80]}")
            logger.warning("下载方式 %s 失败: %s", name, str(exc)[:120])
    raise RuntimeError("TDX 包下载失败 " + " | ".join(errors))


# ---------------------------------------------------------------- 解析
def _is_stock(market: str, code: str) -> bool:
    """只保留 A 股个股, 排除指数/ETF/债券/B股/板块指数。

    sh/lday 里混有 5xxxxx(ETF)、11x/12x(债券)、000/880(指数); sz 里有 159xxx(ETF)、
    12x(债券)、200(B股)、399(指数)。若不剔除, 涨跌家数与成交额都会被污染。
    """
    if market == "sh":
        return code.startswith(("600", "601", "603", "605", "688", "689"))
    if market == "sz":
        return code.startswith(("000", "001", "002", "003", "300", "301", "302"))
    return code.startswith(("43", "83", "87", "88", "92")) and not code.startswith("899")


def _parse_members(zip_path: Path) -> list[tuple[str, str, str]]:
    """列出包内个股 .day 文件: (market, stem, code), stem 形如 sh600000。"""
    with zipfile.ZipFile(zip_path) as zf:
        out = []
        for name in zf.namelist():
            parts = name.split("/")
            if len(parts) != 3 or parts[1] != "lday" or not name.endswith(".day"):
                continue
            market, stem = parts[0], parts[2].split(".")[0]
            if market not in ("sh", "sz", "bj") or len(stem) < 7:
                continue
            code = stem[2:]
            if not code.isdigit() or not _is_stock(market, code):
                continue
            out.append((market, stem, code))
        return out


def _read_day(zf: zipfile.ZipFile, name: str) -> np.ndarray:
    """读取单只证券 .day 文件为结构化数组; 截断/空文件返回空数组。"""
    raw = zf.read(name)
    n = len(raw) // RECORD_BYTES
    if n == 0:
        return np.empty(0, dtype=DAY_DTYPE)
    return np.frombuffer(raw[: n * RECORD_BYTES], dtype=DAY_DTYPE)


def parse_zip(zip_path: Path, detail_cutoff: int | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """解析全量包, 返回 (breadth, stock_detail)。

    breadth 按日聚合全市场(2014 起): 上涨/下跌/平盘家数, 上涨量/下跌量, 成交额合计。
    stock_detail 只保留最近 DETAIL_DAYS 个交易日, 供形态/板块计算。
    """
    start = _history_start_num()
    # 抽样 40 个文件取最新交易日, 据此确定明细截断点(全量扫描会多花一倍解压时间)
    t_scan = time.time()
    zf = zipfile.ZipFile(zip_path)
    members = _parse_members(zip_path)
    step = max(len(members) // 40, 1)
    latest = 0
    for market, stem, _code in members[::step]:
        a = _read_day(zf, f"{market}/lday/{stem}.day")
        if len(a):
            latest = max(latest, int(a["date"][-1]))
    latest = latest or int(datetime.now().strftime("%Y%m%d"))
    cutoff = detail_cutoff or int(
        (pd.Timestamp(str(latest)) - pd.DateOffset(years=DETAIL_YEARS)).strftime("%Y%m%d"))
    logger.info("TDX 包内个股文件 %d 个, 最新交易日 %d, 明细截断 %d (扫描 %.0fs)",
                len(members), latest, cutoff, time.time() - t_scan)

    # 逐日聚合容器: date -> [up, down, flat, up_vol, down_vol, amount, up_amt, down_amt]
    acc: dict[int, list[float]] = {}
    detail_parts: list[pd.DataFrame] = []
    t0 = time.time()
    for i, (market, stem, code) in enumerate(members, 1):
        a = _read_day(zf, f"{market}/lday/{stem}.day")
        if len(a) < 2:
            continue
        a = a[(a["date"] >= start) & (a["date"] <= 99999999)]
        if len(a) < 2:
            continue
        # 记录内 date 若未排序, 按 date 排序保证 prev_close 正确
        if np.any(np.diff(a["date"]) < 0):
            a = a[np.argsort(a["date"])]
        close = a["close"].astype(np.float64) / 100.0
        prev = np.concatenate([[np.nan], close[:-1]])
        chg = close / prev - 1.0
        vol = a["volume"].astype(np.float64)
        amt = a["amount"].astype(np.float64)
        up = (chg > 0.0005) & np.isfinite(chg)
        dn = (chg < -0.0005) & np.isfinite(chg)
        fl = ~up & ~dn & np.isfinite(chg)
        dates = a["date"]
        # 向量化按日聚合: unique + bincount, 避免逐日布尔筛选的 O(日期x记录) 开销
        udates, inv = np.unique(dates, return_inverse=True)
        n = len(udates)
        fields = (
            np.bincount(inv, weights=up.astype(np.float64), minlength=n),
            np.bincount(inv, weights=dn.astype(np.float64), minlength=n),
            np.bincount(inv, weights=fl.astype(np.float64), minlength=n),
            np.bincount(inv, weights=np.where(up, vol, 0.0), minlength=n),
            np.bincount(inv, weights=np.where(dn, vol, 0.0), minlength=n),
            np.bincount(inv, weights=amt, minlength=n),
            np.bincount(inv, weights=np.where(up, amt, 0.0), minlength=n),
            np.bincount(inv, weights=np.where(dn, amt, 0.0), minlength=n),
        )
        for j, d in enumerate(udates):
            row = acc.setdefault(int(d), [0.0] * 8)
            for k in range(8):
                row[k] += float(fields[k][j])

        keep = dates >= cutoff
        if keep.sum() >= 2:
            detail_parts.append(pd.DataFrame({
                "code": code,
                "market": market,
                "date": dates[keep].astype("U8"),
                "open": a["open"][keep].astype(np.float64) / 100.0,
                "high": a["high"][keep].astype(np.float64) / 100.0,
                "low": a["low"][keep].astype(np.float64) / 100.0,
                "close": close[keep],
                "prev_close": prev[keep],
                "volume": vol[keep],
                "amount": amt[keep],
            }))
        if i % 1500 == 0:
            logger.info("解析进度 %d/%d (%.0fs)", i, len(members), time.time() - t0)
    zf.close()

    breadth = pd.DataFrame(
        [(d, *v) for d, v in sorted(acc.items())],
        columns=["date", "up", "down", "flat", "up_vol", "down_vol",
                 "amount", "up_amt", "down_amt"],
    )
    breadth["date"] = pd.to_datetime(breadth["date"].astype(str)).dt.strftime("%Y-%m-%d")
    breadth["total"] = breadth["up"] + breadth["down"] + breadth["flat"]
    breadth["adv_pct"] = breadth["up"] / breadth["total"].replace(0, np.nan)
    breadth["trin_raw"] = (breadth["up"] / breadth["down"].replace(0, np.nan)) / (
        breadth["up_vol"] / breadth["down_vol"].replace(0, np.nan))
    stock = pd.concat(detail_parts, ignore_index=True) if detail_parts else pd.DataFrame()
    logger.info("解析完成 %.0fs: breadth %d 行, 个股明细 %d 行",
                time.time() - t0, len(breadth), len(stock))
    return breadth, stock


def detect_ex_rights(stock: pd.DataFrame, market_breadth: pd.DataFrame | None = None,
                     z_threshold: float = 7.5) -> pd.Series:
    """标记疑似除权除息日(不复权价格跳变), 返回布尔 Series(与 stock 同索引)。

    规则: 单日跌幅超过 z_threshold%, 且当日振幅 (high-low)/prev_close <= 15%。
    实测该启发式不可靠(对 10 只样本近 3 年: 精确率 0/18、召回率 0/37), 原因是常见的
    分红除权仅 1%~3%, 与普通下跌无法区分; 仅作 ex_rights.parquet 抓取失败时的最后兜底。
    """
    if stock.empty:
        return pd.Series(dtype=bool)
    df = stock
    chg = df["close"] / df["prev_close"] - 1.0
    amp = (df["high"] - df["low"]) / df["prev_close"].replace(0, np.nan)
    if market_breadth is not None and not market_breadth.empty:
        m = market_breadth.set_index("date")["adv_pct"]
        base = df["date"].map(m).fillna(0.5)
        # 市场普跌日(上涨占比<30%)放宽, 避免把系统性下跌误判成除权
        extra = np.where(base < 0.30, 4.0, 0.0)
    else:
        extra = 0.0
    thresh = -np.asarray(z_threshold, dtype=float) / 100.0 - np.asarray(extra) / 100.0
    return (chg <= thresh) & (amp <= 0.15)


def fetch_tdx(force: bool = False) -> tuple[pd.DataFrame, pd.DataFrame]:
    """下载并解析全量包, 落盘 breadth 与个股明细, 更新状态。"""
    t0 = time.time()
    out_breadth = store.DATA_DIR / "tdx_breadth.parquet"
    out_stock = store.DATA_DIR / "tdx_stock.parquet"
    try:
        zp = ensure_zip(force)
        # 解析产物比 zip 新 → 说明本次包已解析过, 直接复用(省 2-4 分钟)
        if not force and out_breadth.exists() and out_stock.exists() \
                and out_breadth.stat().st_mtime >= zp.stat().st_mtime \
                and out_stock.stat().st_mtime >= zp.stat().st_mtime:
            breadth = store.read_frame("tdx_breadth.parquet")
            stock = store.read_frame("tdx_stock.parquet")
            # 除权日历更新过(或之前是启发式打的标)时, 重新打标
            flags = store.read_status().get("ex_rights_flags", {})
            cal = store.read_frame("ex_rights.parquet")
            if not cal.empty and flags.get("flag_source") != "eastmoney_fhps":
                from scripts.radar import exrights

                stock = exrights.apply_flags(stock, breadth)
                store.write_frame("tdx_stock.parquet", stock)
                logger.info("已用东财除权日历重新打标: %d 行", int(stock["ex_rights"].sum()))
            logger.info("复用已解析 TDX 产物 (%d 日 / %d 行)", len(breadth), len(stock))
            prev = store.read_status().get("tdx", {})
            parse_sec = prev.get("elapsed_sec") if prev.get("mode") == "parse" else None
            store.update_status(
                "tdx", status="ok", path="hsjday.zip", zip_mb=round(zp.stat().st_size / 1048576, 1),
                mode="cached", breadth_days=len(breadth), stock_rows=len(stock),
                stock_codes=int(stock["code"].nunique()),
                start=str(breadth["date"].min()), end=str(breadth["date"].max()),
                ex_rights_rows=int(stock["ex_rights"].sum()) if "ex_rights" in stock else 0,
                elapsed_sec=parse_sec, cached_sec=round(time.time() - t0, 1),
                note="复用已解析产物; .day 为不复权, 除权日按东财日历打标",
            )
            return breadth, stock
        breadth, stock = parse_zip(zp)
        if breadth.empty:
            raise ValueError("解析后 breadth 为空")
        from scripts.radar import exrights

        stock = exrights.apply_flags(stock, breadth)
        store.write_frame("tdx_breadth.parquet", breadth)
        store.write_frame("tdx_stock.parquet", stock)
        store.update_status(
            "tdx", status="ok", path="hsjday.zip", zip_mb=round(zp.stat().st_size / 1048576, 1),
            mode="parse",
            breadth_days=len(breadth), stock_rows=len(stock),
            stock_codes=int(stock["code"].nunique()),
            start=str(breadth.date.min()), end=str(breadth.date.max()),
            ex_rights_rows=int(stock["ex_rights"].sum()),
            elapsed_sec=round(time.time() - t0, 1),
            note=".day 为不复权; ex_rights 来自东财分红送配除权除息日(缺失时回退启发式), 跨期收益已排除跨除权样本",
        )
        logger.info("TDX 完成 %.0fs: breadth %s~%s, 个股 %d 只/%d 行",
                    time.time() - t0, breadth.date.min(), breadth.date.max(),
                    stock["code"].nunique(), len(stock))
        return breadth, stock
    except Exception as exc:
        store.update_status("tdx", status="failed", reason=str(exc)[:160])
        logger.error("TDX 解析失败: %s", str(exc)[:200])
        raise


def main() -> None:
    """CLI 入口。"""
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="下载并解析通达信盘后全量包")
    ap.add_argument("--force", action="store_true", help="忽略缓存强制重新下载")
    args = ap.parse_args()
    fetch_tdx(force=args.force)


if __name__ == "__main__":
    main()
