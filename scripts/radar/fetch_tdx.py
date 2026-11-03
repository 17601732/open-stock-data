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

分阶段与增量:
- 已有 breadth/stock 产物时, 只解析最后一个已落盘交易日之后的缺口(默认行为)。
- 产物缺失/口径版本不一致时, 按「年」切片逐段解析(新→旧), 每段落盘到
  data/radar/tdx/parts, 由 manifest.json 记录已完成年份, 支持断点续跑;
  中断后下次只补未完成的年份, 失败只需重跑该段。

CLI:
    python -m scripts.radar.fetch_tdx              # 自动: 有产物走增量, 否则按年补历史
    python -m scripts.radar.fetch_tdx --force      # 强制重新下载全量包(仍按增量/分段解析)
    python -m scripts.radar.fetch_tdx --incremental  # 只解析缺口, 不补历史
    python -m scripts.radar.fetch_tdx --one-segment  # 只补一个缺失年份(调试/超长历史分批)
    python -m scripts.radar.fetch_tdx --from-year 2024 --to-year 2024  # 只跑指定年份段
"""

from __future__ import annotations

import json
import logging
import time
import zipfile
from datetime import datetime, timedelta
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
# 直连下载后校验包内最新交易日: 超过 7 个自然日视为 CDN/镜像返回了旧包, 重试一次后失败
STALE_DAYS = 7
DETAIL_YEARS = 3           # 个股明细保留近 3 年(形态回测需要足够的跨期样本)
PARSE_VERSION = 5          # 解析口径版本: 证券类型白名单/volume 过滤/市场前缀主键变更时递增


class PackageNotUpdated(RuntimeError):
    """包已下载但内容仍是上一交易日: 不落库、不覆盖快照, 等下一次 cron。"""

DAY_DTYPE = np.dtype([
    ("date", "<i4"), ("open", "<i4"), ("high", "<i4"), ("low", "<i4"),
    ("close", "<i4"), ("amount", "<f4"), ("volume", "<i4"), ("reserved", "<i4"),
])
RECORD_BYTES = DAY_DTYPE.itemsize  # 32

CACHE_DIR = store.DATA_DIR / "tdx"
CACHE_ZIP = CACHE_DIR / "hsjday.zip"
# 分阶段解析的落盘目录: 只进 Actions 缓存, 不提交(个股明细约 82MB, 超过仓库舒适体积)
PART_DIR = CACHE_DIR / "parts"
PART_MANIFEST = PART_DIR / "manifest.json"


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


def _zip_latest_date(zp: Path) -> int:
    """抽样读 20 个 .day, 返回包内最新交易日(YYYYMMDD); 无可识别文件返回 0。"""
    members = _parse_members(zp)
    if not members:
        return 0
    step = max(len(members) // 20, 1)
    latest = 0
    with zipfile.ZipFile(zp) as zf:
        for _m, _s, _c, _b, entry in members[::step]:
            a = _read_day(zf, entry)
            if len(a):
                latest = max(latest, int(a["date"][-1]))
    return latest


def _existing_end_num() -> int:
    """已落盘产物里的最后交易日(YYYYMMDD), 用于判断新包是否比旧包还旧。"""
    try:
        b = store.read_frame("tdx_breadth.parquet")
        return int(str(b["date"].max()).replace("-", "")) if not b.empty else 0
    except Exception:
        return 0


def _expected_latest_num(now: datetime | None = None) -> int:
    """当前应到交易日: 工作日盘后(>=15:30)为当日, 否则为上一个工作日。"""
    now = now or datetime.now()
    after_close = now.hour * 60 + now.minute >= 15 * 60 + 30
    d = now.date()
    if not after_close:
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return int(d.strftime("%Y%m%d"))


def ensure_zip(force: bool = False) -> tuple[Path, int]:
    """确保本地有可用的全量包, 返回 (路径, 包内最新交易日)。

    缓存在 24h 内且体积正常时仍需抽检包内最新日期: 通达信日线包发布晚于 18:30,
    只用 mtime 会一直复用 16:30 下的旧包。直连 data.tdx.com.cn 下载后校验体积与
    包内最新交易日; 校验不通过删除重下一次, 仍失败则报错。
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    expected = _expected_latest_num()
    if CACHE_ZIP.exists() and not force:
        size = CACHE_ZIP.stat().st_size
        age = time.time() - CACHE_ZIP.stat().st_mtime
        latest = _zip_latest_date(CACHE_ZIP)
        if size >= MIN_ZIP_BYTES and age < 24 * 3600 and latest >= expected:
            logger.info("复用缓存 %s (%.0f MB, %.1f 小时前, 最新交易日 %d)",
                        CACHE_ZIP, size / 1048576, age / 3600, latest)
            return CACHE_ZIP, latest
        if size >= MIN_ZIP_BYTES and age < 24 * 3600:
            logger.info("缓存包最新交易日 %d 未到应到交易日 %d, 重新下载", latest, expected)
    stale_before = int((datetime.now() - timedelta(days=STALE_DAYS)).strftime("%Y%m%d"))
    errors = []
    for attempt in (1, 2):
        for name, hdrs in (("cookie", {**TDX_HEADERS, "Cookie": TDX_COOKIE}), ("plain", TDX_HEADERS)):
            try:
                t0 = time.time()
                _download_once(CACHE_ZIP, hdrs)
                size = CACHE_ZIP.stat().st_size
                if size < MIN_ZIP_BYTES:
                    raise ValueError(f"包体积 {size / 1048576:.0f} MB < {MIN_ZIP_BYTES / 1048576:.0f} MB")
                latest = _zip_latest_date(CACHE_ZIP)
                if latest < stale_before:
                    raise ValueError(f"包内最新交易日 {latest} 过旧(阈值 {stale_before}), 疑似旧包")
                prev_end = _existing_end_num()
                if prev_end and latest < prev_end:
                    raise ValueError(f"包内最新交易日 {latest} 早于已落盘 {prev_end}, 疑似旧包")
                logger.info("TDX 包下载成功(方式=%s, %.1fs, %.0f MB, 最新交易日 %d)",
                            name, time.time() - t0, size / 1048576, latest)
                return CACHE_ZIP, latest
            except Exception as exc:
                errors.append(f"{name}:{str(exc)[:80]}")
                logger.warning("下载方式 %s 失败/校验未过: %s", name, str(exc)[:120])
        if attempt == 1:
            logger.warning("包校验未通过, 删除后重试一次")
            CACHE_ZIP.unlink(missing_ok=True)
            CACHE_ZIP.with_suffix(".part").unlink(missing_ok=True)
    raise RuntimeError("TDX 包下载失败 " + " | ".join(errors))


# ---------------------------------------------------------------- 解析
# 证券类型白名单: 只保留沪深 A 股。其余(指数/ETF/LOF/REITs/债券/可转债/B 股/板块指数)
# 一律排除 —— 实测包内共 12,233 个 .day, 若只按「排除指数」会多算约 3,100 个非股票品种。
STOCK_BOARDS: dict[str, tuple[str, ...]] = {
    "sh": ("600", "601", "603", "605", "688", "689"),   # 沪主板 + 科创板
    "sz": ("000", "001", "002", "003", "300", "301"),   # 深主板 + 创业板
}
BSE_PREFIXES = ("43", "83", "87", "88", "92")


def _stock_class(market: str, code: str) -> str | None:
    """返回证券类型: sh_main/sh_kcb/sz_main/sz_gem/bse, 非 A 股返回 None。"""
    if market == "sh":
        if code.startswith(("600", "601", "603", "605")):
            return "sh_main"
        if code.startswith(("688", "689")):
            return "sh_kcb"
        return None
    if market == "sz":
        if code.startswith(("000", "001", "002", "003")):
            return "sz_main"
        if code.startswith(("300", "301")):
            return "sz_gem"
        return None
    if market == "bj" and code.startswith(BSE_PREFIXES) and not code.startswith("899"):
        return "bse"
    return None


def _parse_members(zip_path: Path) -> list[tuple[str, str, str, str, str]]:
    """列出包内 A 股 .day 文件: (market, stem, code, board, zip_name)。

    不同 CDN 返回的包内路径分隔符可能是 `/` 也可能是 `\\`, 统一归一化后再判断;
    zip_name 保留原样, 解压时直接按它读取。
    """
    with zipfile.ZipFile(zip_path) as zf:
        out = []
        for name in zf.namelist():
            parts = name.replace("\\", "/").split("/")
            if len(parts) != 3 or parts[1] != "lday" or not name.endswith(".day"):
                continue
            market, stem = parts[0], parts[2].split(".")[0]
            if market not in ("sh", "sz", "bj") or len(stem) < 7:
                continue
            code = stem[2:]
            if not code.isdigit():
                continue
            board = _stock_class(market, code)
            if board is None:
                continue
            out.append((market, stem, code, board, name))
        return out


def _read_day(zf: zipfile.ZipFile, name: str) -> np.ndarray:
    """读取单只证券 .day 文件为结构化数组; 截断/空文件返回空数组。"""
    raw = zf.read(name)
    n = len(raw) // RECORD_BYTES
    if n == 0:
        return np.empty(0, dtype=DAY_DTYPE)
    return np.frombuffer(raw[: n * RECORD_BYTES], dtype=DAY_DTYPE)


def parse_zip(zip_path: Path, detail_cutoff: int | None = None,
              lo_num: int | None = None, hi_num: int | None = None,
              ) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """解析全量包, 返回 (breadth, stock_detail, bse_breadth)。

    breadth 只统计沪深 A 股(2014 起): 上涨/下跌/平盘家数, 上涨量/下跌量, 成交额合计。
    北交所单列返回, 不并入沪深口径(北交所 2021 年才开市, 混入会造成历史序列断层)。
    stock_detail 只保留沪深 A 股最近 DETAIL_YEARS 年, 主键为 market+code(symbol)。

    lo_num/hi_num 限定只聚合 [lo_num, hi_num] 区间的交易日(增量/分段解析用);
    涨跌幅仍用区间前一条记录算 prev_close, 保证缺口首日不会退化为 0/0/0。
    """
    start = _history_start_num()
    lo = max(start, int(lo_num or 0))
    hi = int(hi_num or 99999999)
    # 抽样 40 个文件取最新交易日, 据此确定明细截断点(全量扫描会多花一倍解压时间)
    t_scan = time.time()
    zf = zipfile.ZipFile(zip_path)
    members = _parse_members(zip_path)
    step = max(len(members) // 40, 1)
    latest = 0
    for _market, _stem, _code, _board, entry in members[::step]:
        a = _read_day(zf, entry)
        if len(a):
            latest = max(latest, int(a["date"][-1]))
    latest = latest or int(datetime.now().strftime("%Y%m%d"))
    cutoff = detail_cutoff or int(
        (pd.Timestamp(str(latest)) - pd.DateOffset(years=DETAIL_YEARS)).strftime("%Y%m%d"))
    logger.info("TDX 包内 A 股文件 %d 个, 最新交易日 %d, 明细截断 %d, 解析区间 %d~%d (扫描 %.0fs)",
                len(members), latest, cutoff, lo, hi, time.time() - t_scan)

    # 逐日聚合容器: date -> [up, down, flat, up_vol, down_vol, amount, up_amt, down_amt]
    acc: dict[int, list[float]] = {}
    acc_bse: dict[int, list[float]] = {}
    detail_parts: list[pd.DataFrame] = []
    t0 = time.time()
    for i, (market, _stem, code, board, entry) in enumerate(members, 1):
        a = _read_day(zf, entry)
        if len(a) < 2:
            continue
        # 记录内 date 若未排序, 按 date 排序保证 prev_close 正确
        if np.any(np.diff(a["date"]) < 0):
            a = a[np.argsort(a["date"])]
        # 只保留有价且有成交的记录: 停牌/无价格占位行不计入分母
        price_ok = (a["open"] > 0) & (a["high"] > 0) & (a["low"] > 0) & (a["close"] > 0)
        vol_all = a["volume"].astype(np.float64)
        a = a[price_ok & (vol_all > 0)]
        if len(a) < 2:
            continue
        close = a["close"].astype(np.float64) / 100.0
        prev = np.concatenate([[np.nan], close[:-1]])
        chg = close / prev - 1.0
        # 先算涨跌幅再截断: 缺口/分段的首个保留日需要前一条记录收盘价,
        # 否则其 prev_close 恒为 NaN, 那天会退化成 0/0/0
        keep = (a["date"] >= lo) & (a["date"] <= hi)
        if not keep.any():
            continue
        a, close, prev, chg = a[keep], close[keep], prev[keep], chg[keep]
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
        target = acc_bse if board == "bse" else acc
        for j, d in enumerate(udates):
            row = target.setdefault(int(d), [0.0] * 8)
            for k in range(8):
                row[k] += float(fields[k][j])

        if board == "bse":
            continue
        detail_lo = max(cutoff, lo)
        keep = (dates >= detail_lo) & (dates <= hi)
        if keep.any():
            detail_parts.append(pd.DataFrame({
                "symbol": f"{market}{code}",
                "market": market,
                "code": code,
                "sec_type": board,
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

    breadth = _agg_frame(acc)
    bse_breadth = _agg_frame(acc_bse)
    stock = pd.concat(detail_parts, ignore_index=True) if detail_parts else pd.DataFrame()
    logger.info("解析完成 %.0fs: 沪深A股 breadth %d 行, 北交所 %d 行, 个股明细 %d 行",
                time.time() - t0, len(breadth), len(bse_breadth), len(stock))
    return breadth, stock, bse_breadth


def _agg_frame(acc: dict[int, list[float]]) -> pd.DataFrame:
    """把逐日累加容器转成 breadth 表, 并保证 up+down+flat==total。"""
    cols = ["date", "up", "down", "flat", "up_vol", "down_vol",
            "amount", "up_amt", "down_amt"]
    if not acc:
        return pd.DataFrame(columns=[*cols, "total", "adv_pct", "trin_raw"])
    df = pd.DataFrame([(d, *v) for d, v in sorted(acc.items())], columns=cols)
    df["date"] = pd.to_datetime(df["date"].astype(str)).dt.strftime("%Y-%m-%d")
    df["total"] = df["up"] + df["down"] + df["flat"]
    df["adv_pct"] = df["up"] / df["total"].replace(0, np.nan)
    df["trin_raw"] = (df["up"] / df["down"].replace(0, np.nan)) / (
        df["up_vol"] / df["down_vol"].replace(0, np.nan))
    return df


def validate_breadth(breadth: pd.DataFrame, stock: pd.DataFrame | None = None,
                     bse: pd.DataFrame | None = None) -> dict:
    """宽度硬校验: 家数自洽 / 最新日量级 / 年度分布 / 峰值 / 与样本池集合比对。"""
    if breadth.empty:
        return {"ok": False, "reason": "breadth 为空"}
    d = breadth.copy()
    sum_ok = bool(((d["up"] + d["down"] + d["flat"]) == d["total"]).all())
    latest = d.iloc[-1]
    year = str(latest["date"])[:4]
    annual = {}
    for y in ("2014", "2019", "2021", year):
        s = d[d["date"].astype(str).str[:4] == y]
        if s.empty:
            continue
        annual[y] = {"days": len(s), "median_total": int(s["total"].median()),
                     "max_total": int(s["total"].max()), "last_total": int(s["total"].iloc[-1])}
    max_row = d.loc[d["total"].idxmax()]
    pool = store.read_frame("pool.csv")
    pool_not_in_tdx: list[str] = []
    active_codes = 0
    if stock is not None and not stock.empty:
        last_num = str(latest["date"]).replace("-", "")
        codes = set(stock.loc[stock["date"].astype(str) == last_num, "code"])
        active_codes = len(codes)
        if not pool.empty:
            pool_not_in_tdx = sorted(set(pool["code"].astype(str).str.zfill(6)) - codes)[:10]
    return {
        "ok": sum_ok and bool(max_row["total"] < 6000),
        "sum_matches_total": sum_ok,
        "latest_date": str(latest["date"]),
        "latest": {"up": int(latest["up"]), "down": int(latest["down"]),
                   "flat": int(latest["flat"]), "total": int(latest["total"])},
        # 口径为"当日有成交的沪深A股", 停牌股不计分母; 2026-11-02 实测 5,216
        "expected_latest_range": [5100, 5500],
        "within_expected_range": bool(5100 <= latest["total"] <= 5500),
        "annual": annual,
        "max_total": int(max_row["total"]), "max_total_date": str(max_row["date"]),
        "peak_below_6000": bool(max_row["total"] < 6000),
        "bse_days": len(bse) if bse is not None else 0,
        "bse_latest_total": int(bse.iloc[-1]["total"]) if bse is not None and not bse.empty else 0,
        "pool_codes": len(pool), "pool_not_in_tdx": pool_not_in_tdx,
        "tdx_active_codes": active_codes,
    }


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


# ---------------------------------------------------------------- 分段落盘
def _read_manifest() -> dict:
    """读取分段 manifest; 损坏时按空处理(重新补历史, 不影响增量)。"""
    if PART_MANIFEST.exists():
        try:
            return json.loads(PART_MANIFEST.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("分段 manifest 损坏, 重新开始补历史")
    return {"segments": {}}


def _write_manifest(manifest: dict) -> None:
    PART_DIR.mkdir(parents=True, exist_ok=True)
    PART_MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def _year_range(year: int) -> tuple[int, int]:
    return int(f"{year}0101"), int(f"{year}1231")


def _part_path(kind: str, year: int) -> Path:
    return PART_DIR / f"{kind}_{year}.parquet"


def _write_segment(year: int, breadth: pd.DataFrame, bse: pd.DataFrame,
                   stock: pd.DataFrame) -> None:
    """把单个年份段的解析结果落盘, 并登记到 manifest(断点续跑依据)。"""
    PART_DIR.mkdir(parents=True, exist_ok=True)
    for kind, df in (("breadth", breadth), ("bse", bse), ("stock", stock)):
        if not df.empty:
            df.to_parquet(_part_path(kind, year), index=False)
    manifest = _read_manifest()
    manifest["segments"][str(year)] = {
        "breadth_rows": len(breadth), "bse_rows": len(bse), "stock_rows": len(stock),
        "done_at": datetime.now().isoformat(timespec="seconds"),
    }
    _write_manifest(manifest)
    logger.info("分段 %d 已落盘: breadth %d 行, 北交所 %d 行, 个股明细 %d 行",
                year, len(breadth), len(bse), len(stock))


def _load_parts() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """合并所有已完成年份段; 返回 (breadth, bse, stock)。"""
    manifest = _read_manifest()
    years = sorted(int(y) for y in manifest.get("segments", {}) if y.isdigit())
    frames: dict[str, list[pd.DataFrame]] = {"breadth": [], "bse": [], "stock": []}
    for y in years:
        for kind in frames:
            p = _part_path(kind, y)
            if p.exists():
                frames[kind].append(pd.read_parquet(p))
    out = []
    for kind in ("breadth", "bse", "stock"):
        if not frames[kind]:
            out.append(pd.DataFrame())
            continue
        df = pd.concat(frames[kind], ignore_index=True)
        keys = ["date"] if kind != "stock" else ["symbol", "date"]
        df = df.drop_duplicates(subset=keys, keep="last").sort_values(keys[0])
        out.append(df.reset_index(drop=True))
    return out[0], out[1], out[2]


# ---------------------------------------------------------------- 主流程
def _commit_products(breadth: pd.DataFrame, stock: pd.DataFrame, bse: pd.DataFrame,
                     *, mode: str, t0: float, zp: Path, gap_days: int = 0) -> tuple[pd.DataFrame, pd.DataFrame]:
    """校验并落盘最终产物(下游只读这三个 parquet), 更新状态。"""
    if breadth.empty:
        raise ValueError("解析后 breadth 为空")
    checks = validate_breadth(breadth, stock, bse)
    if not checks["sum_matches_total"]:
        raise ValueError(f"宽度自洽校验失败: {checks['latest']}")
    from scripts.radar import exrights

    if not stock.empty:
        stock = exrights.apply_flags(stock, breadth)
    store.write_frame("tdx_breadth.parquet", breadth)
    if not stock.empty:
        store.write_frame("tdx_stock.parquet", stock)
    else:
        logger.warning("本次解析无个股明细(分段早于明细年限), 保留已有 tdx_stock.parquet")
    if not bse.empty:
        store.write_frame("tdx_bse_breadth.parquet", bse)
    else:
        logger.warning("本次解析无北交所数据, 保留已有 tdx_bse_breadth.parquet")
    store.update_status(
        "tdx", status="ok", path="hsjday.zip", zip_mb=round(zp.stat().st_size / 1048576, 1),
        mode=mode, parse_version=PARSE_VERSION,
        breadth_days=len(breadth), stock_rows=len(stock),
        stock_codes=int(stock["symbol"].nunique()) if not stock.empty else 0,
        start=str(breadth.date.min()), end=str(breadth.date.max()),
        ex_rights_rows=int(stock["ex_rights"].sum()) if "ex_rights" in stock else 0,
        elapsed_sec=round(time.time() - t0, 1), gap_days=gap_days,
        checks=checks, scope="沪深A股(不含北交所)",
        note=".day 为不复权; ex_rights 来自东财分红送配除权除息日(缺失时回退启发式), 跨期收益已排除跨除权样本",
    )
    logger.info("TDX 完成 %.0fs(mode=%s, 缺口 %d 日): 沪深A股 %s~%s 最新 %s, 个股 %d 只/%d 行",
                time.time() - t0, mode, gap_days, breadth.date.min(), breadth.date.max(),
                checks["latest"]["total"], stock["symbol"].nunique() if not stock.empty else 0,
                len(stock))
    return breadth, stock


def _incremental(zp: Path, t0: float) -> tuple[pd.DataFrame, pd.DataFrame] | None:
    """只解析最后一个已落盘交易日之后的数据; 无缺口返回 None(调用方走分段)。"""
    old_breadth = store.read_frame("tdx_breadth.parquet")
    if old_breadth.empty:
        return None
    last = str(old_breadth["date"].max())
    lo = int(last.replace("-", "")) + 1
    ts = time.time()
    new_breadth, new_stock, new_bse = parse_zip(zp, lo_num=lo)
    logger.info("增量解析 %.0fs(起点 %s): breadth +%d 日, 个股 +%d 行",
                time.time() - ts, last, len(new_breadth), len(new_stock))
    if new_breadth.empty:
        logger.info("TDX 无新增交易日(最后已落盘 %s), 跳过重解析", last)
        store.update_status("tdx", status="ok", mode="incremental", parse_version=PARSE_VERSION,
                            start=str(old_breadth.date.min()), end=last,
                            breadth_days=len(old_breadth), gap_days=0,
                            note="无新增交易日; 复用已落盘产物")
        return store.read_frame("tdx_breadth.parquet"), store.read_frame("tdx_stock.parquet")

    old_stock = store.read_frame("tdx_stock.parquet")
    old_bse = store.read_frame("tdx_bse_breadth.parquet")

    def _merge(old: pd.DataFrame, new: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
        if old.empty:
            return new
        merged = pd.concat([old, new], ignore_index=True)
        merged = merged.drop_duplicates(subset=keys, keep="last")
        sort_col = "date" if "date" in merged.columns else keys[0]
        return merged.sort_values(sort_col).reset_index(drop=True)

    breadth = _merge(old_breadth, new_breadth, ["date"])
    stock = _merge(old_stock, new_stock, ["symbol", "date"])
    bse = _merge(old_bse, new_bse, ["date"])
    return _commit_products(breadth, stock, bse, mode="incremental", t0=t0, zp=zp,
                            gap_days=len(new_breadth))


def _staged(zp: Path, t0: float, *, all_segments: bool, from_year: int | None,
            to_year: int | None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """按年分段补历史: 新→旧逐段落盘, manifest 记录进度, 支持断点续跑。"""
    start_year = int(_history_start_num() // 10000)
    this_year = datetime.now().year
    lo_y = from_year or start_year
    hi_y = to_year or this_year
    manifest = _read_manifest()
    done = {int(y) for y in manifest.get("segments", {}) if str(y).isdigit()}
    todo = [y for y in range(hi_y, lo_y - 1, -1) if y not in done]
    if not todo:
        logger.info("分段历史已补齐(%d~%d), 改走增量", lo_y, hi_y)
        return _incremental(zp, t0) or (_load_parts()[0], _load_parts()[2])
    if not all_segments:
        todo = todo[:1]
    logger.info("待补分段 %s(共 %d 段, 逐段落盘, 可断点续跑)", todo, len(todo))
    for year in todo:
        lo_num, hi_num = _year_range(year)
        ts = time.time()
        breadth, stock, bse = parse_zip(zp, lo_num=lo_num, hi_num=hi_num)
        if breadth.empty:
            raise ValueError(f"分段 {year} 解析后 breadth 为空")
        _write_segment(year, breadth, bse, stock)
        logger.info("分段 %d 完成 %.0fs", year, time.time() - ts)
    breadth, bse, stock = _load_parts()
    if breadth.empty:
        # 分段全部落在当前年份之前(个股明细为空)时, 仍以最近可用段为准
        raise ValueError("所有分段合并后 breadth 为空")
    return _commit_products(breadth, stock, bse, mode="staged", t0=t0, zp=zp,
                            gap_days=0)


def _staged_in_progress() -> bool:
    """是否处于「分段补历史」中途: 有 manifest 且仍有缺失年份。"""
    manifest = _read_manifest()
    done = {int(y) for y in manifest.get("segments", {}) if str(y).isdigit()}
    if not done:
        return False
    start_year = int(_history_start_num() // 10000)
    expected = set(range(start_year, datetime.now().year + 1))
    return done != expected


def fetch_tdx(force: bool = False, mode: str = "auto", all_segments: bool = True,
              from_year: int | None = None, to_year: int | None = None,
              ) -> tuple[pd.DataFrame, pd.DataFrame]:
    """下载并解析全量包, 落盘 breadth/个股明细/北交所序列, 更新状态。

    mode: auto(有产物走增量, 否则补分段) / incremental(只补缺口) / full(一次全量) / staged(补分段)
    """
    t0 = time.time()
    try:
        zp, latest = ensure_zip(force)
        # 缓存包可能来自被截断/被反爬页替换的旧下载: 先扫成员, 0 个则丢弃重下
        members = _parse_members(zp)
        if not members:
            logger.warning("缓存包内无 A 股 .day, 删除后重新下载")
            zp.unlink(missing_ok=True)
            zp, latest = ensure_zip(force=True)
            members = _parse_members(zp)
            if not members:
                with zipfile.ZipFile(zp) as zf:
                    sample = zf.namelist()[:5]
                raise ValueError(f"包内无可识别 A 股 .day (%.1f MB), namelist 样例={sample}"
                                 % (zp.stat().st_size / 1048576))

        # 前置守卫: 包已下载但还没更新到应到交易日 → 不落库、不覆盖快照, 等下一次 cron
        expected = _expected_latest_num()
        if latest < expected:
            store.update_status(
                "tdx", status="package_not_updated", path="hsjday.zip",
                zip_mb=round(zp.stat().st_size / 1048576, 1),
                zip_latest=latest, expected_trading_day=expected,
                checked_at=datetime.now().isoformat(timespec="seconds"),
                note="包内最新交易日未到应到交易日, 本次不解析、不落库、不覆盖快照")
            logger.warning("package_not_updated: 包内最新交易日 %d < 应到交易日 %d, 不落库不覆盖快照",
                           latest, expected)
            raise PackageNotUpdated(f"package_not_updated: {latest} < {expected}")

        prev = store.read_status().get("tdx", {})
        has_products = (store.DATA_DIR / "tdx_breadth.parquet").exists() and \
                       (store.DATA_DIR / "tdx_stock.parquet").exists()
        version_ok = prev.get("parse_version") == PARSE_VERSION

        if from_year is not None or to_year is not None:
            return _staged(zp, t0, all_segments=all_segments, from_year=from_year, to_year=to_year)
        if mode == "full":
            breadth, stock, bse = parse_zip(zp)
            return _commit_products(breadth, stock, bse, mode="full", t0=t0, zp=zp)
        if mode == "auto" and has_products and version_ok and _staged_in_progress():
            # 上一轮分段补历史中途退出: 先补完缺失年份, 再补当天缺口
            logger.info("检测到分段补历史未完成, 继续补缺失年份")
            _staged(zp, t0, all_segments=all_segments, from_year=None, to_year=None)
            got = _incremental(zp, t0)
            if got is not None:
                return got
            return store.read_frame("tdx_breadth.parquet"), store.read_frame("tdx_stock.parquet")
        if mode in ("auto", "incremental") and has_products and version_ok:
            got = _incremental(zp, t0)
            if got is not None:
                return got
        if mode == "incremental":
            logger.warning("已落盘产物的口径版本为 %s(需 %s), 增量模式不重解析, 沿用现有产物",
                           prev.get("parse_version"), PARSE_VERSION)
            breadth = store.read_frame("tdx_breadth.parquet")
            if breadth.empty:
                raise ValueError("无已落盘产物, 不能只跑增量; 请去掉 --incremental 补历史")
            return breadth, store.read_frame("tdx_stock.parquet")
        # 冷启动/口径版本变更/产物缺失: 分段补历史(默认一次补完, 可断点续跑)
        return _staged(zp, t0, all_segments=all_segments, from_year=None, to_year=None)
    except PackageNotUpdated:
        raise # 状态已标记为 package_not_updated, 不能被下面的 failed 覆盖
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
    ap.add_argument("--incremental", action="store_true", help="只解析缺口, 不补历史")
    ap.add_argument("--full", action="store_true", help="一次性全量解析(不推荐, 用于应急)")
    ap.add_argument("--one-segment", action="store_true", help="只补一个缺失年份后退出")
    ap.add_argument("--from-year", type=int, default=None, help="分段起始年, 如 2024")
    ap.add_argument("--to-year", type=int, default=None, help="分段结束年, 如 2026")
    args = ap.parse_args()
    mode = "full" if args.full else ("incremental" if args.incremental else "auto")
    fetch_tdx(force=args.force, mode=mode, all_segments=not args.one_segment,
              from_year=args.from_year, to_year=args.to_year)


if __name__ == "__main__":
    main()
