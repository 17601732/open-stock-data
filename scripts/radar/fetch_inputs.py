"""雷达输入数据抓取: 增量优先, 多源回退, 状态如实记录。

数据源与口径:
- 指数日线: BaoStock(sh.000300/sh.000001) → 新浪 stock_zh_index_daily 回退
- 样本池: 中证指数官网成分股(沪深300 + 中证500), 当前成分固定池
- 个股日线: 东方财富(不复权) → 新浪回退; 不复权避免复权因子变化造成的增量不一致
- 两融余额: AKShare macro_china_market_margin_sh / _sz (沪深合计)
- 10年国债: AKShare bond_china_yield 过滤中债国债收益率曲线
- 估值: 乐咕乐股 沪深300 PE/PB(月度)、全A PE(日度); 全A PB 无公开接口
- 期权PCR: 上交所每日期权统计 option_daily_stats_sse, 510050 + 510300
- 股东增减持: 东方财富 stock_ggcg_em (分页较慢, 可单独开关)
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import akshare as ak
import numpy as np
import pandas as pd

from scripts.radar import store
from scripts.radar.config import RADAR_CONFIG

logger = logging.getLogger(__name__)

HISTORY_START = RADAR_CONFIG["history_start"]
PCR_START = "2019-12-23"  # 300ETF 期权上市日, 两个标的都有数据的起点
RATE_LIMIT_HINTS = ("RateLimit", "限流", "Too many", "频繁")


def _retry(fn, tries: int = 3, wait: int = 45):
    """限流重试; 命中限流提示时退避, 其它异常直接抛出。"""
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as exc:
            last = exc
            if any(h in str(exc) for h in RATE_LIMIT_HINTS):
                logger.warning("命中限流(第%d次), %ds 后重试: %s", i + 1, wait, str(exc)[:120])
                time.sleep(wait * (i + 1))
                continue
            raise
    raise last  # type: ignore[misc]


# 单步硬超时: akshare 内部 HTTP 没有 timeout, 上游挂起会让整条流水线停住
# (实测 2026-11-04 run 41825543356 卡在 pool 42 分钟, 直到 job 45 分钟上限被杀)。
STEP_TIMEOUTS = {
    "index_daily": 300,
    "ex_rights": 300,
    "tdx": 900,
    "stock_daily": 900,
    "pool": 180,
    "boards": 300,
    "bond_yield": 240,
    "margin_daily": 240,
    "valuation": 180,
    "option_pcr": 600,
    "holder_change": 1800,
}
DEFAULT_STEP_TIMEOUT = 600


def _run_with_deadline(fn, timeout: float, name: str):
    """在守护线程里执行 fn; 超过 timeout 抛 TimeoutError。

    线程无法强杀, 但主流程不再被它阻塞(job 不会耗到 45 分钟上限)。
    """
    box: dict = {}

    def _target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - 原样抛回主线程
            box["error"] = exc

    th = threading.Thread(target=_target, name=f"fetch-{name}", daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        raise TimeoutError(f"{name} 超过 {timeout:.0f}s 未返回(上游接口挂起)")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _cn_symbol(code: str) -> str:
    """6位代码 → baostock 代码(sh./sz./bj.)。"""
    if code.startswith("6"):
        return f"sh.{code}"
    if code.startswith(("0", "3")):
        return f"sz.{code}"
    return f"bj.{code}"


def _sina_symbol(code: str) -> str:
    """6位代码 → 新浪代码(sh600000/sz000001)。"""
    return _cn_symbol(code).replace(".", "")


# ---------------------------------------------------------------- 指数
def _index_symbol(code: str) -> str:
    """指数代码 → 交易所前缀代码(sh000300); 不能用个股规则(000300 是沪市指数)。"""
    return str(RADAR_CONFIG["index_codes"].get(code, f"sh.{code}")).replace(".", "")


def _index_from_sina(code: str, days: int = 320) -> pd.DataFrame:
    """新浪指数日线(直连 K 线接口, 单次最多约 1000 根)。"""
    import requests

    r = requests.get(
        "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData",
        params={"symbol": _index_symbol(code), "scale": "240", "ma": "no", "datalen": str(days)},
        timeout=20,
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"},
    )
    r.raise_for_status()
    data = r.json()
    if not data:
        raise ValueError("新浪指数返回空数据")
    return pd.DataFrame({
        "code": code, "date": [row["day"] for row in data],
        "open": pd.to_numeric([row.get("open") for row in data], errors="coerce"),
        "high": pd.to_numeric([row.get("high") for row in data], errors="coerce"),
        "low": pd.to_numeric([row.get("low") for row in data], errors="coerce"),
        "close": pd.to_numeric([row.get("close") for row in data], errors="coerce"),
        "volume": pd.to_numeric([row.get("volume") for row in data], errors="coerce"),
        "amount": None,
    })


def _index_from_tx(code: str, days: int = 320, start: str | None = None,
                   end: str | None = None) -> pd.DataFrame:
    """腾讯指数日线(直连 K 线接口, 含成交额); 可指定日期窗口。"""
    import requests

    r = requests.get(
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
        params={"param": f"{_index_symbol(code)},day,{start or ''},{end or ''},{days},"},
        timeout=20,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    r.raise_for_status()
    day = (r.json().get("data", {}).get(_index_symbol(code), {}) or {}).get("day") or []
    rows = [row[:6] for row in day if isinstance(row, list) and len(row) >= 5]
    if not rows:
        raise ValueError("腾讯指数返回空数据")
    df = pd.DataFrame(rows, columns=["date", "open", "close", "high", "low", "amount"][: len(rows[0])])
    return pd.DataFrame({
        "code": code, "date": df["date"],
        "open": pd.to_numeric(df["open"], errors="coerce"),
        "high": pd.to_numeric(df["high"], errors="coerce"),
        "low": pd.to_numeric(df["low"], errors="coerce"),
        "close": pd.to_numeric(df["close"], errors="coerce"),
        "volume": None,
        "amount": pd.to_numeric(df["amount"], errors="coerce"),
    })


def _index_full_history(code: str) -> pd.DataFrame:
    """按 2 年窗口分段抓全历史(腾讯单次窗口上限约 640 根, 2 年约 490 根)。"""

    def _window(s: str, e: str) -> pd.DataFrame:
        return _index_from_tx(code, 640, s, e)

    start = pd.Timestamp(HISTORY_START)
    end = pd.Timestamp.now().normalize()
    frames: list[pd.DataFrame] = []
    while start <= end:
        w_end = min(start + pd.DateOffset(years=2) - pd.Timedelta(days=1), end)
        s, e = str(start.date()), str(w_end.date())
        frames.append(_retry(lambda s=s, e=e: _window(s, e), tries=3, wait=5))
        start = w_end + pd.Timedelta(days=1)
    out = pd.concat(frames, ignore_index=True).drop_duplicates("date")
    return out.sort_values("date").reset_index(drop=True)


def fetch_index_daily(full: bool = False) -> pd.DataFrame:
    """抓取沪深300与上证指数日线(新浪 → 腾讯回退), 增量补齐缺失日期。"""
    frames: list[pd.DataFrame] = []
    days = 1200 if full else 60
    for code in RADAR_CONFIG["index_codes"]:
        src = None
        last_err: Exception | None = None
        if full:
            # 新浪/腾讯单次请求最多约 1000 根, 全历史必须分段
            try:
                src = _index_full_history(code)
                logger.info("指数 %s 来源 tx 全历史 (%d 行)", code, len(src))
            except Exception as exc:
                last_err = exc
                logger.warning("指数 %s 全历史抓取失败, 退回单段模式: %s", code, str(exc)[:80])
        if src is None:
            for name, fn in (("tx", _index_from_tx), ("sina", _index_from_sina)):
                for attempt in range(2):
                    try:
                        src = fn(code, days)
                        logger.info("指数 %s 来源 %s (%d 行)", code, name, len(src))
                        break
                    except Exception as exc:
                        last_err = exc
                        logger.warning("指数 %s 来源 %s 第%d次失败: %s", code, name, attempt + 1, str(exc)[:80])
                        time.sleep(1)
                if src is not None:
                    break
        if src is None:
            logger.error("指数 %s 全部来源失败: %s", code, last_err)
            continue
        # 增量保留整个 60 日窗口并 upsert: 不能只截取 >= 最新日期的行,
        # 否则窗口内漏跑的交易日会被永久丢弃(连带 PCR 的交易日清单也缺这一天)
        frames.append(src)

    if frames:
        df = pd.concat(frames, ignore_index=True)
        df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
        for col in store.INDEX_SCHEMA:
            if col not in df.columns:
                df[col] = None
        df = df[store.INDEX_SCHEMA].dropna(subset=["close"])
        store.upsert("index_daily.parquet", df, ["code", "date"])
    df = store.read_frame("index_daily.parquet")
    if not df.empty:
        store.update_status("index_daily", status="ok", start=str(df.date.min()), end=str(df.date.max()))
    else:
        store.update_status("index_daily", status="failed", reason="新浪/腾讯均未取到数据")
    return df


def _f(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 样本池
def _pool_one(idx: str) -> pd.DataFrame:
    """单个指数成分股, 单次调用 180s 硬超时(csindex 接口偶发完全不返回)。"""
    df = _run_with_deadline(lambda: ak.index_stock_cons_csindex(symbol=idx),
                            STEP_TIMEOUTS["pool"], f"pool-{idx}")
    return pd.DataFrame({
        "code": df["成分券代码"].astype(str).str.zfill(6),
        "name": df["成分券名称"],
        "source_index": idx,
    })


def fetch_pool_symbols() -> pd.DataFrame:
    """抓取沪深300 + 中证500 当前成分股作为固定样本池。

    成分股名单变化很慢: 接口超时/失败时重试 3 次, 仍失败则回退上次落盘的 pool.csv,
    不因单日接口挂起而中断整条流水线。
    """
    cached = store.read_frame("pool.csv")
    frames = []
    for idx in RADAR_CONFIG["pool"]["index_codes"]:
        for attempt in range(1, 4):
            try:
                frames.append(_pool_one(idx))
                break
            except Exception as exc:  # noqa: BLE001 - 超时/网络异常统一重试
                logger.warning("样本池 %s 第 %d/3 次抓取失败: %s", idx, attempt, str(exc)[:120])
                if attempt < 3:
                    time.sleep(5)
        else:
            if not cached.empty and "source_index" in cached.columns:
                fallback = cached[cached["source_index"] == idx]
                if not fallback.empty:
                    logger.warning("样本池 %s 三次失败, 回退本地缓存 %d 只", idx, len(fallback))
                    frames.append(fallback[["code", "name", "source_index"]])
                    continue
            raise RuntimeError(f"样本池 {idx} 三次抓取均失败, 且无本地缓存")
    pool = pd.concat(frames).drop_duplicates("code").reset_index(drop=True)

    store.write_frame("pool.csv", pool)
    snap_date = str(pd.Timestamp.now().date())
    store.update_status("pool", status="ok", count=len(pool), start=snap_date, end=snap_date,
                        note="当前成分股名单快照(非时间序列), 全历史按此固定池回算")
    logger.info("样本池 %d 只(沪深300+中证500)", len(pool))
    return pool


# ---------------------------------------------------------------- 个股
def _fetch_one_stock(code: str, start: str) -> pd.DataFrame:
    """单只个股日线(不复权)。东财 push2 限流严重, 默认走新浪, 东财仅作回退。

    新浪 turnover 为小数(0.0088), 统一换算为百分数, 与东财口径一致。
    """
    d = ak.stock_zh_a_daily(symbol=_sina_symbol(code), start_date=start.replace("-", ""),
                            end_date=datetime.now().strftime("%Y%m%d"), adjust="")
    d = pd.DataFrame({
        "code": code,
        "date": pd.to_datetime(d["date"]).dt.strftime("%Y-%m-%d"),
        "close": pd.to_numeric(d["close"], errors="coerce"),
        "pct_chg": pd.to_numeric(d["close"], errors="coerce").pct_change() * 100,
        "volume": pd.to_numeric(d["volume"], errors="coerce"),
        "amount": pd.to_numeric(d["amount"], errors="coerce"),
        "turnover": pd.to_numeric(d["turnover"], errors="coerce") * 100.0,
    })
    return d


def _float_caps(codes: list[str] | None = None) -> pd.Series:
    """当前流通市值(元): 东财现货快照 → 新浪快照 → 逐只个股资料; 缓存 3 天。"""
    cached = store.read_frame("float_caps.parquet")
    if not cached.empty and "fetched_at" in cached.columns and len(cached) >= 500:
        ts = pd.to_datetime(cached["fetched_at"].iloc[0], errors="coerce")
        if pd.notna(ts) and (datetime.now() - ts.to_pydatetime()).days < 3:
            return cached.set_index("code")["float_cap"]
    d = None
    try:
        d = ak.stock_zh_a_spot_em()[["代码", "流通市值"]].dropna()
    except Exception as exc:
        logger.warning("东财流通市值快照失败(%s), 改用新浪快照", str(exc)[:80])
        try:
            d = ak.stock_zh_a_spot()[["代码", "流通市值"]].dropna()
        except Exception as exc2:
            logger.warning("新浪流通市值快照失败(%s), 改用逐只个股资料", str(exc2)[:80])
    out = pd.DataFrame()
    if d is not None:
        out = pd.DataFrame({
            "code": d["代码"].astype(str).str.replace(r"\D", "", regex=True).str.zfill(6),
            "float_cap": pd.to_numeric(d["流通市值"], errors="coerce"),
            "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }).dropna(subset=["float_cap"])
    if out.empty and codes:
        # 逐只兜底: stock_individual_info_em 的「流通市值」, 只对样本池 800 只调用
        rows: list[dict] = []

        def _one(c: str) -> dict | None:
            try:
                info = ak.stock_individual_info_em(symbol=c)
                cap = float(info.loc[info["item"] == "流通市值", "value"].iloc[0])
                return {"code": c, "float_cap": cap}
            except Exception:
                return None

        with ThreadPoolExecutor(max_workers=8) as ex:
            for r in ex.map(_one, codes):
                if r and r["float_cap"] > 0:
                    rows.append(r)
        if rows:
            out = pd.DataFrame(rows)
            out["fetched_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            logger.info("逐只流通市值兜底: %d/%d 只", len(out), len(codes))
    if out.empty:
        return pd.Series(dtype=float)
    out = out[out["float_cap"] > 0].drop_duplicates("code", keep="last")
    # 覆盖率不足时不落盘, 避免残缺快照在缓存里留存 3 天
    if len(out) < max(500, int(0.8 * len(codes or []))):
        logger.warning("流通市值覆盖不足(%d 只), 不缓存, 回退旧行推算", len(out))
        return pd.Series(dtype=float)
    store.write_frame("float_caps.parquet", out)
    return out.set_index("code")["float_cap"]


def _stocks_from_tdx(pool: pd.DataFrame) -> pd.DataFrame:
    """把 TDX 全市场个股明细裁剪成样本池行, 并换算成 stock_daily 口径。

    TDX 无换手率/流通股本: 用最近一次已知流通市值(由 API 历史 amount/turnover 反推)
    换算 turnover, 流通股本变化很慢, 近似误差可忽略; 无历史市值的代码留空。
    """
    tdx = store.read_frame("tdx_stock.parquet")
    old = store.read_frame("stock_daily.parquet")
    if not old.empty:
        old["code"] = old["code"].astype(str).str.zfill(6)
        # 清掉历史上误写入的非 ISO 日期行(TDX YYYYMMDD), 避免增量库里两种日期格式混存
        iso = old["date"].astype(str).str.match(r"^\d{4}-\d{2}-\d{2}$")
        if not iso.all():
            old = old[iso].copy()
            store.write_frame("stock_daily.parquet", old)
            logger.warning("stock_daily 清理了 %d 行非标准日期", int((~iso).sum()))
    sub = tdx[tdx["code"].isin(set(pool["code"]))].copy()
    if sub.empty:
        return pd.DataFrame(columns=store.STOCK_SCHEMA)
    # 流通股本: 优先用东财现货快照的当前流通市值折算; 缺失代码才回退旧行反推
    latest_close = sub.sort_values("date").groupby("code")["close"].last()
    fc = _float_caps(sorted(sub["code"].unique().tolist()))
    shares = pd.Series(dtype=float)
    if len(fc):
        shares = (fc / latest_close.reindex(fc.index).replace(0, np.nan)).dropna()
    missing = set(sub["code"]) - set(shares.index)
    if missing and not old.empty and "turnover" in old.columns:
        # 只用 TDX 明细起点之前的 API 行反推股本: 近 3 年行已被 TDX 覆盖(可能缺换手率),
        # 再按最新收盘价折算, 避免把旧市值原样带到今天
        old_valid = old[pd.to_numeric(old["turnover"], errors="coerce").notna()].copy()
        old_valid["turnover"] = pd.to_numeric(old_valid["turnover"], errors="coerce")
        cutoff_iso = pd.to_datetime(str(tdx["date"].min()), format="%Y%m%d").strftime("%Y-%m-%d")
        base = old_valid[(old_valid["date"] < cutoff_iso) & old_valid["code"].isin(missing)]
        last = base.sort_values("date").groupby("code").tail(1).set_index("code")
        cap_old = last["amount"] / (last["turnover"] / 100.0)
        est = (cap_old / last["close"].replace(0, np.nan)).replace([np.inf, -np.inf], np.nan)
        shares = pd.concat([shares, est.dropna()])
    cap_row = sub["code"].map(shares) * sub["close"].astype(float)
    out = pd.DataFrame({
        "code": sub["code"],
        "date": pd.to_datetime(sub["date"].astype(str), format="%Y%m%d").dt.strftime("%Y-%m-%d"),
        "close": sub["close"].astype(float),
        "pct_chg": (sub["close"] / sub["prev_close"].replace(0, np.nan) - 1.0) * 100.0,
        "volume": sub["volume"].astype(float),
        "amount": sub["amount"].astype(float),
        "turnover": (sub["amount"] / cap_row.replace(0, np.nan)) * 100.0,
    })
    return out.dropna(subset=["close"])


def fetch_stock_daily(full: bool = False, workers: int = 8, limit: int | None = None) -> pd.DataFrame:
    """个股日线: 优先用 TDX 全量包产物, 缺失代码/包解析失败时才逐只走接口。"""
    pool = store.read_frame("pool.csv")
    if pool.empty:
        pool = fetch_pool_symbols()
    pool["code"] = pool["code"].astype(str).str.zfill(6)

    tdx = store.read_frame("tdx_stock.parquet")
    tdx_ok = store.read_status().get("tdx", {}).get("status") == "ok"
    source = "api"
    if not tdx.empty and tdx_ok:
        latest = pd.to_datetime(tdx["date"].max(), format="%Y%m%d").strftime("%Y-%m-%d")
        idx_last = ""
        idx = store.read_frame("index_daily.parquet")
        if not idx.empty:
            idx_last = str(idx["date"].max())[:10]
        # 包内最新交易日落后指数 5 个自然日以上视为过期, 走接口兜底
        fresh = tdx_ok and (not idx_last or latest >= str(pd.Timestamp(idx_last) - pd.Timedelta(days=5))[:10])
        if fresh:
            derived = _stocks_from_tdx(pool)
            missing = set(pool["code"]) - set(derived["code"].unique())
            if not derived.empty:
                store.upsert("stock_daily.parquet", derived, ["code", "date"])
                source = "tdx"
                store.update_status("stock_daily", status="ok" if not missing else "partial",
                                    stock_source="tdx", rows_from_tdx=len(derived),
                                    tdx_latest=latest, codes=int(derived["code"].nunique()),
                                    missing_codes=sorted(missing)[:10],
                                    missing_count=len(missing),
                                    start=str(derived["date"].min()), end=latest,
                                    note="TDX 全量包解析; 换手率由当前流通市值(现货快照优先)换算; 接口仅作兜底")
                logger.info("个股日线来源 TDX: %d 行, 缺 %d 只", len(derived), len(missing))
                if not missing:
                    return store.read_frame("stock_daily.parquet")
                # 个别代码 TDX 无数据(新股/长期停牌), 只对这些走接口
                pool = pool[pool["code"].isin(missing)].copy()
            else:
                logger.warning("TDX 明细裁剪为空, 回退接口")
    store.update_status("stock_daily", stock_source=source)
    return _fetch_stock_daily_api(pool, full=full, workers=workers, limit=limit)


def _fetch_stock_daily_api(pool: pd.DataFrame, full: bool = False,
                           workers: int = 8, limit: int | None = None) -> pd.DataFrame:
    """(兜底)抓取样本池个股日线; 已存在的股票只补缺失日期, 逐批落盘便于续抓。"""
    existing = store.read_frame("stock_daily.parquet")
    done_codes = set(existing.code.unique()) if not existing.empty else set()
    last_dates = existing.groupby("code")["date"].max().to_dict() if not existing.empty else {}

    todo = []
    # 以指数交易日为基准做缺口检测: 每只股票补齐「已有区间内的所有缺失交易日」,
    # 而不是只看最后一天, 避免漏跑某天后中间日期永远补不回来
    idx = store.read_frame("index_daily.parquet")
    cal = sorted(idx.loc[idx.code == "000300", "date"].astype(str).unique()) if not idx.empty else []
    have_map = (existing.groupby("code")["date"].apply(lambda s: set(s.astype(str)))
                if not existing.empty else {})
    first_dates = existing.groupby("code")["date"].min().astype(str).to_dict() if not existing.empty else {}
    for code in pool.code:
        if full or code not in done_codes:
            todo.append((code, HISTORY_START))
            continue
        have = have_map.get(code, set())
        if cal:
            gaps = [d for d in cal if d >= first_dates.get(code, HISTORY_START) and d not in have]
            if gaps:
                todo.append((code, gaps[0]))
        elif last_dates.get(code, "") < datetime.now().strftime("%Y-%m-%d"):
            nxt = (pd.Timestamp(last_dates[code]) + timedelta(days=1)).strftime("%Y-%m-%d")
            todo.append((code, nxt))
    if limit:
        todo = todo[:limit]
    logger.info("待抓个股 %d 只 (池内 %d, 已有 %d)", len(todo), len(pool), len(done_codes))

    ok, failed = 0, []
    batch: list[pd.DataFrame] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_fetch_one_stock, c, s): c for c, s in todo}
        for fut in as_completed(futures):
            code = futures[fut]
            try:
                d = fut.result()
                if not d.empty:
                    batch.append(d)
                    ok += 1
            except Exception as exc:
                failed.append({"code": code, "error": str(exc)[:120]})
                logger.warning("个股抓取失败 %s: %s", code, str(exc)[:100])
            if len(batch) >= 20:
                store.upsert("stock_daily.parquet", pd.concat(batch, ignore_index=True), ["code", "date"])
                batch.clear()
            if (ok + len(failed)) % 50 == 0:
                logger.info("个股进度: 成功 %d / 失败 %d / 共 %d", ok, len(failed), len(todo))
    if batch:
        store.upsert("stock_daily.parquet", pd.concat(batch, ignore_index=True), ["code", "date"])

    df = store.read_frame("stock_daily.parquet")
    store.update_status("stock_daily", status="partial" if failed else "ok",
                        stocks_ok=ok, stocks_failed=len(failed),
                        codes=int(df.code.nunique()) if not df.empty else 0,
                        failed_samples=failed[:10],
                        start=str(df.date.min()) if not df.empty else None,
                        end=str(df.date.max()) if not df.empty else None)
    return df


# ---------------------------------------------------------------- 两融 / 国债
def fetch_margin() -> pd.DataFrame:
    """抓取沪深两市融资融券数据并合计。

    保留明细字段供 App 详情页使用:
    存量(融资余额/融券余额/合计)、流量(融资买入额/融券卖出量)。
    """
    cols = {"融资余额": "fin_bal", "融资买入额": "fin_buy",
            "融券余额": "short_bal", "融券卖出量": "short_sell_vol",
            "融资融券余额": "total"}
    frames = []
    for fn, mkt in [(ak.macro_china_market_margin_sh, "sse"), (ak.macro_china_market_margin_sz, "szse")]:
        d = fn()
        f = pd.DataFrame({"date": pd.to_datetime(d["日期"]).dt.strftime("%Y-%m-%d")})
        for src, dst in cols.items():
            f[f"{mkt}_{dst}"] = pd.to_numeric(d[src], errors="coerce") if src in d.columns else np.nan
        frames.append(f)
    m = frames[0].merge(frames[1], on="date", how="outer").sort_values("date")
    for dst in cols.values():
        m[dst] = m[[f"sse_{dst}", f"szse_{dst}"]].sum(axis=1, min_count=1)
    m["sse"] = m["sse_total"]
    m["szse"] = m["szse_total"]
    m = m.dropna(subset=["total"]).reset_index(drop=True)
    store.upsert("margin_daily.parquet", m, ["date"])
    store.update_status("margin_daily", status="ok", start=str(m.date.min()), end=str(m.date.max()))
    logger.info("两融余额 %d 行, %s ~ %s", len(m), m.date.min(), m.date.max())
    return m


def fetch_bond_yield() -> pd.DataFrame:
    """抓取10年期国债到期收益率(中债国债收益率曲线, 2015年起日频)。"""
    d = ak.bond_zh_us_rate(start_date=HISTORY_START.replace("-", ""))
    out = pd.DataFrame({
        "date": pd.to_datetime(d["日期"]).dt.strftime("%Y-%m-%d"),
        "cn10y": pd.to_numeric(d["中国国债收益率10年"], errors="coerce"),
    }).dropna().drop_duplicates("date").sort_values("date")
    store.upsert("bond_yield.parquet", out, ["date"])
    store.update_status("bond_yield", status="ok", start=str(out.date.min()), end=str(out.date.max()))
    logger.info("10年国债 %d 行, %s ~ %s", len(out), out.date.min(), out.date.max())
    return out


# ---------------------------------------------------------------- 估值
def fetch_valuation() -> pd.DataFrame:
    """沪深300 PE/PB(月度) + 全A PE(日度); 全A PB 无接口, 状态标记 missing。"""
    frames = []
    pe = ak.stock_index_pe_lg(symbol="沪深300")
    pb = ak.stock_index_pb_lg(symbol="沪深300")
    a = pd.DataFrame({"date": pd.to_datetime(pe["日期"]).dt.strftime("%Y-%m-%d"),
                      "pe": pd.to_numeric(pe["滚动市盈率"], errors="coerce")})
    b = pd.DataFrame({"date": pd.to_datetime(pb["日期"]).dt.strftime("%Y-%m-%d"),
                      "pb": pd.to_numeric(pb["市净率"], errors="coerce")})
    hs = a.merge(b, on="date", how="outer")
    hs["code"] = "hs300"
    frames.append(hs[["code", "date", "pe", "pb"]])
    try:
        alla = ak.stock_market_pe_lg(symbol="全A股")
        c = pd.DataFrame({"code": "alla",
                          "date": pd.to_datetime(alla["日期"]).dt.strftime("%Y-%m-%d"),
                          "pe": pd.to_numeric(alla["市盈率"], errors="coerce"), "pb": pd.NA})
        frames.append(c)
    except Exception as exc:
        logger.warning("全A PE 抓取失败: %s", exc)

    df = pd.concat(frames, ignore_index=True).drop_duplicates(["code", "date"], keep="last")
    store.upsert("valuation.parquet", df, ["code", "date"])
    hs = df[df.code == "hs300"].dropna(subset=["pe"])
    al = df[df.code == "alla"].dropna(subset=["pe"]) if (df.code == "alla").any() else df.iloc[0:0]
    store.update_status(
        "valuation", status="ok",
        hs300="PE/PB 月度(乐咕乐股)",
        alla_pe=f"{al.date.min()} ~ {al.date.max()}" if not al.empty else "missing",
        alla_pb="无公开接口, 已从指标中剔除",
        start=str(hs.date.min()) if not hs.empty else None,
    )
    logger.info("估值 %d 行", len(df))
    return df


# ---------------------------------------------------------------- 期权 PCR
def _fetch_pcr_one(date: str) -> list[dict]:
    """抓取单日期权统计, 返回 510050/510300 的认购认沽成交量。"""
    d = ak.option_daily_stats_sse(date=date.replace("-", ""))
    # 当日统计未发布/接口字段变动时返回空表或换了列名, 显式报错而不是抛 KeyError
    if d is None or d.empty:
        raise RuntimeError(f"{date} 期权统计未发布(空表)")
    if "合约标的代码" not in d.columns:
        raise RuntimeError(f"{date} 期权统计字段变动: columns={list(d.columns)[:8]}")
    out = []
    for target_code in ("510050", "510300"):
        row = d[d["合约标的代码"].astype(str) == target_code]
        if row.empty:
            continue
        r = row.iloc[0]
        call_v = float(r["认购成交量"])
        put_v = float(r["认沽成交量"])
        out.append({"date": date, "underlying": target_code, "call_vol": call_v, "put_vol": put_v})
    return out


def _pcr_to_long(df: pd.DataFrame) -> pd.DataFrame:
    """把宽表 PCR 历史还原成长表(date/underlying/put_vol/call_vol)。

    旧版本落盘的是宽表(put_vol_510050 等), 新抓的是长表; 若直接按 underlying
    pivot, 宽表行会落进 NaN 分组并在重算 put_vol 时被清空, 导致 PCR 历史凭空断档。
    """
    cols = ["date", "underlying", "put_vol", "call_vol"]
    if "underlying" in df.columns:
        return df.reindex(columns=cols).copy()
    pieces = []
    for u in ("510050", "510300"):
        pc, cc = f"put_vol_{u}", f"call_vol_{u}"
        if pc in df.columns and cc in df.columns:
            pieces.append(pd.DataFrame({
                "date": df["date"], "underlying": u, "put_vol": df[pc], "call_vol": df[cc]}))
    if not pieces:
        return pd.DataFrame(columns=cols)
    long = pd.concat(pieces, ignore_index=True)
    # 旧版 pivot 落盘时, 丢失分 ETF 列的行只剩聚合列(put_vol / put_vol_nan); 用 TOTAL 兜底
    agg_cands = [c for c in ("put_vol", "put_vol_nan") if c in df.columns]
    agg = max(agg_cands, key=lambda c: df[c].notna().sum()) if agg_cands else None
    call_cands = [c for c in ("call_vol", "call_vol_nan") if c in df.columns]
    agg_c = max(call_cands, key=lambda c: df[c].notna().sum()) if call_cands else None
    if agg is not None and agg_c is not None:
        have = long.groupby("date")["put_vol"].apply(lambda x: x.notna().any())
        missing_dates = have.index[~have.to_numpy()]
        if len(missing_dates):
            sub = df[df["date"].isin(missing_dates)]
            pieces.append(pd.DataFrame({
                "date": sub["date"], "underlying": "TOTAL",
                "put_vol": sub[agg], "call_vol": sub[agg_c]}))
            long = pd.concat(pieces, ignore_index=True)
    return long


def _pcr_wide(long: pd.DataFrame) -> pd.DataFrame:
    """长表 → 宽表(含 pcr 与分 ETF 列), 宽表是唯一落盘格式。"""
    cols = ["put_vol_510050", "call_vol_510050", "put_vol_510300", "call_vol_510300"]
    out = ["date", *cols, "put_vol_TOTAL", "call_vol_TOTAL", "put_vol", "call_vol", "pcr",
           "pcr_510050", "pcr_510300"]
    if long.empty:
        return pd.DataFrame(columns=out)
    long = long.drop_duplicates(["date", "underlying"], keep="last")
    wide = long.pivot(index="date", columns="underlying", values=["put_vol", "call_vol"])
    wide.columns = [f"{c[0]}_{c[1]}" for c in wide.columns]
    wide = wide.reset_index()
    for c in [*cols, "put_vol_TOTAL", "call_vol_TOTAL"]:
        if c not in wide.columns:
            wide[c] = float("nan")
    wide["put_vol"] = wide[["put_vol_510050", "put_vol_510300"]].sum(axis=1, min_count=1)
    wide["call_vol"] = wide[["call_vol_510050", "call_vol_510300"]].sum(axis=1, min_count=1)
    # 分 ETF 列缺失的旧行用 TOTAL 聚合列补齐总量(分 ETF 的 pcr_510050/300 无法还原, 保持空)
    wide["put_vol"] = wide["put_vol"].fillna(wide["put_vol_TOTAL"])
    wide["call_vol"] = wide["call_vol"].fillna(wide["call_vol_TOTAL"])
    wide["pcr"] = wide["put_vol"] / wide["call_vol"]
    wide["pcr_510050"] = wide["put_vol_510050"] / wide["call_vol_510050"]
    wide["pcr_510300"] = wide["put_vol_510300"] / wide["call_vol_510300"]
    return wide.drop_duplicates("date").sort_values("date").round(4)


def fetch_option_pcr(full: bool = False, workers: int = 4) -> pd.DataFrame:
    """抓取 50ETF/300ETF 期权认购认沽成交量, 计算 PCR; 只保留真实可得区间。"""
    raw = store.read_frame("option_pcr_etf.csv")
    existing = _pcr_wide(_pcr_to_long(raw)) if not raw.empty else raw
    if not raw.empty:
        # 自愈: 历史落盘可能只有分 ETF 成交量而 pcr 为空(旧版宽长表混用导致), 这里按成交量重算
        raw_na = int(raw["pcr"].isna().sum()) if "pcr" in raw.columns else len(raw)
        fixed_na = int(existing["pcr"].isna().sum())
        if fixed_na < raw_na:
            store.write_frame("option_pcr_etf.csv", existing)
            logger.warning("修复历史 PCR 落盘断档: 恢复 %d 个日期", raw_na - fixed_na)
    idx = store.read_frame("index_daily.parquet")
    if idx.empty:
        # PCR 独立 workflow 没有指数缓存时, 用最近 10 个工作日兜底, 不能整源放弃
        logger.warning("缺少指数日线, 回退最近 10 个工作日作为候选交易日")
        dates = [str(t.date()) for t in pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=10)]
    else:
        dates = sorted(idx[idx.code == "000300"].date.unique())
    start = PCR_START
    done = set(existing.date.unique()) if not existing.empty else set()
    todo = [d for d in dates if d >= start and d not in done]
    logger.info("PCR 待抓 %d 个交易日 (起点 %s)", len(todo), start)

    if not todo and not existing.empty:
        store.update_status("option_pcr", status="ok", start=str(existing.date.min()),
                            end=str(existing.date.max()), coverage_days=len(existing),
                            note="仅保留实际可得区间, 无前向填充")
        logger.info("PCR 已是最新, %d 行", len(existing))
        return existing

    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_retry, lambda d=d: _fetch_pcr_one(d)): d for d in todo}
        for i, fut in enumerate(as_completed(futures), 1):
            try:
                rows.extend(fut.result())
            except Exception as exc:
                logger.warning("PCR 抓取失败 %s: %s", futures[fut], str(exc)[:90])
            if i % 100 == 0:
                logger.info("PCR 进度 %d/%d", i, len(todo))

    if rows:
        new = pd.DataFrame(rows)
        # 宽表/长表统一成长表后再合并, 否则同日期的宽表行会在 pivot 中丢失
        wide = _pcr_wide(pd.concat([_pcr_to_long(existing), new], ignore_index=True))
        gaps = int(wide["pcr"].isna().sum())
        if gaps:
            logger.warning("PCR 合并后仍有 %d 个日期缺少 pcr(仅保留原始成交量列)", gaps)
        store.write_frame("option_pcr_etf.csv", wide)
        store.update_status("option_pcr", status="ok", start=str(wide.date.min()), end=str(wide.date.max()),
                            coverage_days=len(wide), note="仅保留实际可得区间, 无前向填充")
        logger.info("PCR %d 行, %s ~ %s", len(wide), wide.date.min(), wide.date.max())
        return wide
    if todo:
        # 有交易日要抓却一条没抓到: 如实失败, 不能拿存量数据冒充成功
        store.update_status("option_pcr", status="failed",
                            reason=f"{len(todo)} 个交易日未抓到数据", unavailable_days=sorted(todo)[-10:])
        raise RuntimeError(f"PCR 抓取全部失败: {len(todo)} 个交易日无数据, 最近 {sorted(todo)[-3:]}")
    if not existing.empty:
        # 部分日期接口尚未发布统计(如盘中/非交易日), 保留存量数据, 不把整源判为失败
        store.update_status("option_pcr", status="ok", start=str(existing.date.min()),
                            end=str(existing.date.max()), coverage_days=len(existing),
                            unavailable_days=sorted(set(todo))[-10:],
                            note="仅保留实际可得区间, 无前向填充")
        logger.info("PCR 无新增可得数据, 保留存量 %d 行", len(existing))
        return existing
    store.update_status("option_pcr", status="failed", reason="期权统计接口未返回数据")
    return existing


# ---------------------------------------------------------------- 增减持
HOLDER_API = "https://datacenter-web.eastmoney.com/api/data/v1/get"


def _holder_page(start: str, end: str, page: int) -> tuple[int, list[dict]]:
    """请求东财增减持指定页, 返回(总页数, 记录)。"""
    import requests

    params = {
        "sortColumns": "END_DATE,SECURITY_CODE,EITIME",
        "sortTypes": "-1,-1,-1",
        "pageSize": "500",
        "pageNumber": str(page),
        "reportName": "RPT_SHARE_HOLDER_INCREASE",
        "columns": "ALL",
        "source": "WEB",
        "client": "WEB",
        "filter": f"(END_DATE>='{start}')(END_DATE<='{end}')",
    }
    r = requests.get(HOLDER_API, params=params, timeout=30)
    r.raise_for_status()
    js = r.json()
    result = js.get("result")
    if not result:
        return 0, []
    return int(result.get("pages") or 0), list(result.get("data") or [])


def _fetch_holder_month(start: str, end: str) -> list[dict]:
    """按月窗口抓取增减持(全量查询只保留近两个月, 必须分窗口取历史)。"""
    rows: list[dict] = []
    pages, page = 1, 1
    while page <= pages:
        pages, data = _retry(lambda p=page: _holder_page(start, end, p), tries=4, wait=8)
        for r in data:
            num = _f(r.get("CHANGE_NUM"))
            sym = _f(r.get("CHANGE_NUM_SYMBOL"))
            if num is None:
                continue
            signed = sym if sym is not None else (num if "增持" in str(r.get("DIRECTION")) else -num)
            rows.append({
                "date": str(r.get("END_DATE") or "")[:10],
                "code": str(r.get("SECURITY_CODE") or "").zfill(6),
                "direction": str(r.get("DIRECTION") or ""),
                "value": round(abs(signed), 4),
                "signed": round(signed, 4),
            })
        page += 1
    return rows


def fetch_holder_change(full: bool = False) -> pd.DataFrame:
    """按自然月窗口抓取股东增减持(万股), 逐季落盘, 单月失败不中断。"""
    existing = store.read_frame("holder_change.parquet")
    end_month = pd.Timestamp.now().normalize().replace(day=1)
    months = pd.date_range(pd.Timestamp(HISTORY_START).replace(day=1), end_month, freq="MS")
    if full or existing.empty:
        todo = list(months)
    else:
        # 已抓取的月份跳过(支持中断续抓), 最近两个月总是重抓以覆盖后续补录
        have = set(pd.to_datetime(existing["date"]).dt.to_period("M").astype(str))
        todo = [m for m in months if str(m)[:7] not in have or m >= months[-2]]

    rows: list[dict] = []
    failed: list[str] = []
    for i, m in enumerate(todo, 1):
        m_end = (m + pd.offsets.MonthEnd(0)).normalize()
        try:
            rows.extend(_fetch_holder_month(str(m.date()), str(m_end.date())))
        except Exception as exc:
            failed.append(f"{str(m.date())[:7]}:{str(exc)[:60]}")
            logger.warning("增减持 %s 抓取失败: %s", str(m.date())[:7], exc)
        if i % 12 == 0 and rows:
            store.upsert("holder_change.parquet", pd.DataFrame(rows), ["date", "code", "direction", "value"])
            rows.clear()
            logger.info("增减持进度 %d/%d 月", i, len(todo))

    if rows:
        store.upsert("holder_change.parquet", pd.DataFrame(rows), ["date", "code", "direction", "value"])
    out = store.read_frame("holder_change.parquet")
    if out.empty:
        store.update_status("holder_change", status="failed",
                            reason=f"未取到增减持数据 {failed[:3]}")
        return out
    store.update_status("holder_change", status="partial" if failed else "ok",
                        start=str(out.date.min()), end=str(out.date.max()), rows=len(out),
                        failed_months=failed[-10:],
                        note="股东增减持按公告日聚合, 单位为万股(未折算金额)")
    logger.info("增减持 %d 行, %s ~ %s", len(out), out.date.min(), out.date.max())
    return out


def fetch_all(full: bool = False, with_holders: bool = False, skip_stocks: bool = False) -> dict:
    """按顺序抓取全部输入; 单源失败不影响其它源。"""
    started = datetime.now().isoformat(timespec="seconds")
    from scripts.radar import boards, exrights, fetch_tdx

    steps = [
        ("index_daily", lambda: fetch_index_daily(full)),
        # 除权日历先于 TDX: 包解析后直接按权威日历打标(失败则用启发式跳空兜底)
        ("ex_rights", lambda: exrights.fetch_ex_rights(force=False)),
        # 全量包解析失败不阻断: 个股会自动回退逐只接口, 并在 fetch_status.stock_source 标注
        ("tdx", lambda: fetch_tdx.fetch_tdx(force=False)),
        ("pool", fetch_pool_symbols),
        ("boards", lambda: boards.fetch_board_members(force=False)),
        ("bond_yield", fetch_bond_yield),
        ("margin_daily", fetch_margin),
        ("valuation", fetch_valuation),
    ]
    if not skip_stocks:
        steps.insert(3, ("stock_daily", lambda: fetch_stock_daily(full)))
    steps.append(("option_pcr", lambda: fetch_option_pcr(full)))
    if with_holders:
        steps.append(("holder_change", fetch_holder_change))

    for name, fn in steps:
        t0 = time.time()
        logger.info("→ 抓取 %s", name)
        try:
            _run_with_deadline(fn, STEP_TIMEOUTS.get(name, DEFAULT_STEP_TIMEOUT), name)
        except fetch_tdx.PackageNotUpdatedError:
            raise # 包未更新到应到交易日: 终止整条流水线, 不落库不覆盖快照
        except Exception as exc:
            logger.error("数据源 %s 失败: %s", name, str(exc)[:160])
            store.update_status(name, status="failed", reason=str(exc)[:160])
        logger.info("← %s 完成, %.0fs", name, time.time() - t0)

    status = store.read_status()
    status["_run"] = {"started": started, "finished": datetime.now().isoformat(timespec="seconds"),
                      "full": full}
    store.write_status(status)
    return status
