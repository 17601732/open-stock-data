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
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import akshare as ak
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
    existing = store.read_frame("index_daily.parquet")
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
        if not full and not existing.empty:
            sub = existing[existing.code == code]
            if not sub.empty:
                src = src[src["date"] >= str(sub.date.max())]
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
def fetch_pool_symbols() -> pd.DataFrame:
    """抓取沪深300 + 中证500 当前成分股作为固定样本池。"""
    frames = []
    for idx in RADAR_CONFIG["pool"]["index_codes"]:
        df = ak.index_stock_cons_csindex(symbol=idx)
        frames.append(pd.DataFrame({
            "code": df["成分券代码"].astype(str).str.zfill(6),
            "name": df["成分券名称"],
            "source_index": idx,
        }))
    pool = pd.concat(frames).drop_duplicates("code").reset_index(drop=True)
    store.write_frame("pool.csv", pool)
    store.update_status("pool", status="ok", count=len(pool),
                        start=str(pd.Timestamp.now().date()))
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


def fetch_stock_daily(full: bool = False, workers: int = 8, limit: int | None = None) -> pd.DataFrame:
    """抓取样本池个股日线; 已存在的股票只补缺失日期, 逐批落盘便于续抓。"""
    pool = store.read_frame("pool.csv")
    if pool.empty:
        pool = fetch_pool_symbols()
    pool["code"] = pool["code"].astype(str).str.zfill(6)
    existing = store.read_frame("stock_daily.parquet")
    done_codes = set(existing.code.unique()) if not existing.empty else set()
    last_dates = existing.groupby("code")["date"].max().to_dict() if not existing.empty else {}

    todo = []
    for code in pool.code:
        if full or code not in done_codes:
            todo.append((code, HISTORY_START))
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
    """抓取沪深两市融资融券余额并合计。"""
    frames = []
    for fn, mkt in [(ak.macro_china_market_margin_sh, "sse"), (ak.macro_china_market_margin_sz, "szse")]:
        d = fn()
        col = "融资融券余额" if "融资融券余额" in d.columns else d.columns[-1]
        frames.append(pd.DataFrame({"date": pd.to_datetime(d["日期"]).dt.strftime("%Y-%m-%d"),
                                    mkt: pd.to_numeric(d[col], errors="coerce")}))
    m = frames[0].merge(frames[1], on="date", how="outer").sort_values("date")
    m["total"] = m[["sse", "szse"]].sum(axis=1, min_count=1)
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


def fetch_option_pcr(full: bool = False, workers: int = 4) -> pd.DataFrame:
    """抓取 50ETF/300ETF 期权认购认沽成交量, 计算 PCR; 只保留真实可得区间。"""
    existing = store.read_frame("option_pcr_etf.csv")
    idx = store.read_frame("index_daily.parquet")
    if idx.empty:
        logger.error("缺少指数日线, 无法确定交易日; 请先抓指数")
        return existing
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
        merged = pd.concat([existing, new], ignore_index=True) if not existing.empty else new
        merged = merged.drop_duplicates(["date", "underlying"], keep="last").sort_values(["date", "underlying"])
        wide = merged.pivot(index="date", columns="underlying", values=["put_vol", "call_vol"])
        wide.columns = [f"{c[0]}_{c[1]}" for c in wide.columns]
        wide = wide.reset_index()
        for col in ("put_vol_510050", "call_vol_510050", "put_vol_510300", "call_vol_510300"):
            if col not in wide.columns:
                wide[col] = float("nan")
        wide["put_vol"] = wide[["put_vol_510050", "put_vol_510300"]].sum(axis=1, min_count=1)
        wide["call_vol"] = wide[["call_vol_510050", "call_vol_510300"]].sum(axis=1, min_count=1)
        wide["pcr"] = wide["put_vol"] / wide["call_vol"]
        wide["pcr_510050"] = wide["put_vol_510050"] / wide["call_vol_510050"]
        wide["pcr_510300"] = wide["put_vol_510300"] / wide["call_vol_510300"]
        wide = wide.drop_duplicates("date").sort_values("date").round(4)
        store.write_frame("option_pcr_etf.csv", wide)
        store.update_status("option_pcr", status="ok", start=str(wide.date.min()), end=str(wide.date.max()),
                            coverage_days=len(wide), note="仅保留实际可得区间, 无前向填充")
        logger.info("PCR %d 行, %s ~ %s", len(wide), wide.date.min(), wide.date.max())
        return wide
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
    steps = [
        ("index_daily", lambda: fetch_index_daily(full)),
        ("pool", fetch_pool_symbols),
        ("bond_yield", fetch_bond_yield),
        ("margin_daily", fetch_margin),
        ("valuation", fetch_valuation),
    ]
    if not skip_stocks:
        steps.insert(2, ("stock_daily", lambda: fetch_stock_daily(full)))
    steps.append(("option_pcr", lambda: fetch_option_pcr(full)))
    if with_holders:
        steps.append(("holder_change", fetch_holder_change))

    for name, fn in steps:
        t0 = time.time()
        logger.info("→ 抓取 %s", name)
        try:
            fn()
        except Exception as exc:
            logger.error("数据源 %s 失败: %s", name, str(exc)[:160])
            store.update_status(name, status="failed", reason=str(exc)[:160])
        logger.info("← %s 完成, %.0fs", name, time.time() - t0)

    status = store.read_status()
    status["_run"] = {"started": started, "finished": datetime.now().isoformat(timespec="seconds"),
                      "full": full}
    store.write_status(status)
    return status
