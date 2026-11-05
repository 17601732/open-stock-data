"""第二类数据: ETF 份额变化 / 股指期货基差 / 期权 IV 与 skew。

设计原则:
- 每个源先小样本实测再全量(见 README 记录), 取不到的显式标记 unavailable, 不做替代值。
- 历史序列落盘到 data/radar/alt/ (在 Actions 缓存里, 不进 git), 增量补抓。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

import akshare as ak
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

ALT_DIR = Path("data/radar/alt")
WATCH_ETF = {"510300": "沪深300ETF", "510050": "上证50ETF", "510500": "中证500ETF",
             "588000": "科创50ETF", "159919": "沪深300ETF(深)"}
INDEX_FOR = {"IF": "000300", "IH": "000016", "IC": "000905", "IM": "000852"}


def _run(fn, timeout=90, default=None):
    box = {}

    def go():
        try:
            box["v"] = fn()
        except BaseException as exc:
            box["e"] = exc
    th = threading.Thread(target=go, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        logger.warning("alt 数据源超时(%ss)", timeout)
        return default
    if "e" in box:
        logger.warning("alt 数据源失败: %s", str(box["e"])[:120])
        return default
    return box["v"]


def _load(name: str) -> pd.DataFrame:
    p = ALT_DIR / name
    return pd.read_csv(p, dtype={"code": str, "underlying": str}) if p.exists() else pd.DataFrame()


def _save(df: pd.DataFrame, name: str) -> None:
    ALT_DIR.mkdir(parents=True, exist_ok=True)
    df.drop_duplicates().to_csv(ALT_DIR / name, index=False)


# ---------------------------------------------------------------- ETF 份额
def _etf_daily(date: str) -> pd.DataFrame:
    df = _run(lambda: ak.fund_etf_scale_sse(date=date.replace("-", "")), 90)
    if df is None or df.empty:
        return pd.DataFrame()
    df = df[df["基金代码"].astype(str).isin(WATCH_ETF)].copy()
    df["date"] = date
    df["code"] = df["基金代码"].astype(str)
    df["shares"] = pd.to_numeric(df["基金份额"], errors="coerce")
    return df[["date", "code", "shares"]].dropna()


def etf_share_module(dates: list[str], lookback: int = 120) -> dict:
    cache = _load("etf_share.csv")
    have = set(cache["date"].astype(str)) if not cache.empty else set()
    todo = [d for d in dates[-lookback:] if d not in have][-120:]
    rows = []
    for d in todo:
        got = _etf_daily(d)
        if not got.empty:
            rows.append(got)
    if rows:
        cache = pd.concat([cache, *rows]).drop_duplicates(["date", "code"], keep="last")
        _save(cache, "etf_share.csv")
    if cache.empty:
        return {"available": False, "reason": "上交所 ETF 份额接口无返回"}

    cache["date"] = cache["date"].astype(str)
    cache = cache.sort_values("date")
    latest = cache["date"].max()
    price = {}
    spot = _run(lambda: ak.fund_etf_spot_em(), 90)
    if spot is not None and not spot.empty and "最新价" in spot.columns:
        price = dict(zip(spot["代码"].astype(str), pd.to_numeric(spot["最新价"], errors="coerce"), strict=False))

    px_hist = {}
    start = cache["date"].min().replace("-", "")
    for code in WATCH_ETF:
        h = _run(lambda c=code: ak.fund_etf_hist_em(
            symbol=c, period="daily", start_date=start, end_date=latest.replace("-", ""),
            adjust=""), 90)
        if h is not None and not h.empty:
            h = h.copy()
            h["date"] = pd.to_datetime(h["日期"]).dt.strftime("%Y-%m-%d")
            px_hist[code] = h.set_index("date")["收盘"].astype(float)

    items = []
    for code, name in WATCH_ETF.items():
        sub = cache[cache["code"] == code].set_index("date")["shares"]
        if sub.empty:
            items.append({"code": code, "name": name, "available": False,
                          "reason": "深市 159919 不在上交所份额接口内" if code == "159919" else "无数据"})
            continue
        px = float(price.get(code) or (px_hist.get(code, pd.Series(dtype=float)).iloc[-1]
                                       if code in px_hist and len(px_hist[code]) else np.nan))
        d5 = sub.diff(5).iloc[-1]
        d20 = sub.diff(20).iloc[-1]
        items.append({
            "code": code, "name": name, "available": True, "shares": float(sub.iloc[-1]),
            "chg_5d": None if pd.isna(d5) else float(d5),
            "chg_20d": None if pd.isna(d20) else float(d20),
            "net_20d": None if pd.isna(d20) or not np.isfinite(px) else round(float(d20 * px) / 1e8, 2),
        })

    piv = cache.pivot_table(index="date", columns="code", values="shares", aggfunc="last")
    net = piv.diff(20) * pd.DataFrame({c: px_hist.get(c, pd.Series(dtype=float))
                                       for c in piv.columns})
    series = net.sum(axis=1, min_count=1).dropna() / 1e8
    hist = pd.DataFrame({"date": series.index, "value": series.round(2).values})
    return {
        "available": True, "asof": latest, "items": items,
        "series": hist.tail(260).to_dict("records"),
        "note": "上交所 ETF 份额(份, 交易所披露原值), 20日净申购额=份额变化乘以收盘价(亿元); "
                "深市 159919 接口不可用, 该标的标记为缺",
    }


# ---------------------------------------------------------------- 期权 IV
def _option_daily(date: str) -> pd.DataFrame:
    df = _run(lambda: ak.option_risk_indicator_sse(date=date.replace("-", "")), 90)
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    df["cid"] = df["CONTRACT_ID"].astype(str)
    df["underlying"] = df["cid"].str[:6]
    df["cp"] = df["cid"].str[6]
    df["iv"] = pd.to_numeric(df["IMPLC_VOLATLTY"], errors="coerce")
    df["delta"] = pd.to_numeric(df["DELTA_VALUE"], errors="coerce")
    out = []
    for und, g in df.groupby("underlying"):
        calls = g[(g.cp == "C") & g.iv.notna() & g.delta.notna()]
        puts = g[(g.cp == "P") & g.iv.notna() & g.delta.notna()]
        if calls.empty:
            continue
        atm = calls.iloc[(calls.delta - 0.5).abs().argsort()[:1]]
        strike_key = atm["cid"].str.slice(7).values[0]
        put_atm = puts[puts.cid.str.slice(7) == strike_key]
        atm_iv = float(np.nanmean([atm.iv.values[0]] + ([put_atm.iv.values[0]] if len(put_atm) else [])))
        c25 = calls.iloc[(calls.delta - 0.25).abs().argsort()[:1]]
        p25 = puts.iloc[(puts.delta + 0.25).abs().argsort()[:1]]
        skew = (float(p25.iv.values[0]) - float(c25.iv.values[0])
                if len(p25) and len(c25) else np.nan)
        out.append({"date": date, "underlying": und, "atm_iv": round(atm_iv * 100, 2),
                    "skew25": None if pd.isna(skew) else round(skew * 100, 2),
                    "n": len(g)})
    return pd.DataFrame(out)


def option_iv_module(dates: list[str], lookback: int = 120) -> dict:
    cache = _load("option_iv.csv")
    have = set(cache["date"].astype(str)) if not cache.empty else set()
    todo = [d for d in dates[-lookback:] if d not in have][-120:]
    rows = []
    for d in todo:
        got = _option_daily(d)
        if not got.empty:
            rows.append(got)
    if rows:
        cache = pd.concat([cache, *rows]).drop_duplicates(["date", "underlying"], keep="last")
        _save(cache, "option_iv.csv")
    if cache.empty:
        return {"available": False, "reason": "上交所期权风险指标接口无返回"}
    cache["date"] = cache["date"].astype(str)
    cache = cache.sort_values("date")
    latest = cache["date"].max()
    items = []
    for und, label in [("510050", "50ETF"), ("510300", "300ETF"), ("588000", "科创50ETF")]:
        sub = cache[cache["underlying"] == und]
        if sub.empty:
            items.append({"underlying": und, "name": label, "available": False})
            continue
        last = sub.iloc[-1]
        items.append({"underlying": und, "name": label, "available": True,
                      "atm_iv": last.atm_iv, "skew25": last.skew25})
    hist = cache[cache["underlying"] == "510050"][["date", "atm_iv", "skew25"]].copy()
    hist["value"] = hist["atm_iv"].round(2)
    hist["skew"] = pd.to_numeric(hist["skew25"], errors="coerce").round(2)
    return {
        "available": True, "asof": latest, "items": items,
        "series": hist[["date", "value", "skew"]].tail(260).to_dict("records"),
        "note": "ATM IV 取 delta≈0.5 认购/认沽均值; skew25 = 25Δ认沽IV - 25Δ认购IV(百分点)",
    }


# ---------------------------------------------------------------- 期货基差
def _expiry(code: str) -> pd.Timestamp:
    yymm = code[2:]
    year, month = 2000 + int(yymm[:2]), int(yymm[2:])
    d = pd.Timestamp(year=year, month=month, day=1)
    fridays = pd.date_range(d, d + pd.offsets.MonthEnd(0), freq="W-FRI")
    return fridays[2]  # 第三个周五


def _contract_hist(code: str) -> pd.DataFrame:
    df = _run(lambda: ak.futures_zh_daily_sina(symbol=code), 90)
    if df is None or df.empty:
        return pd.DataFrame()
    cols = {c.lower(): c for c in df.columns}
    dcol = cols.get("date")
    ccol = cols.get("close")
    if not dcol or not ccol:
        return pd.DataFrame()
    out = pd.DataFrame({"code": code, "date": pd.to_datetime(df[dcol]).dt.strftime("%Y-%m-%d"),
                        "close": pd.to_numeric(df[ccol], errors="coerce")}).dropna()
    return out


def futures_basis_module(dates: list[str], lookback: int = 250) -> dict:
    cache = _load("futures_price.csv")
    start = pd.Timestamp(dates[max(0, len(dates) - lookback)]) if dates else pd.Timestamp.today()
    end = pd.Timestamp(dates[-1]) if dates else pd.Timestamp.today()
    months = pd.period_range(start, end + pd.offsets.MonthEnd(2), freq="M")
    codes = [f"{p}{m.year % 100:02d}{m.month:02d}" for p in INDEX_FOR for m in months]
    have = set(cache["code"].astype(str)) if not cache.empty else set()
    todo = [c for c in codes if c not in have][:60]
    rows = [_contract_hist(c) for c in todo]
    rows = [r for r in rows if not r.empty]
    if rows:
        cache = pd.concat([cache, *rows]).drop_duplicates(["code", "date"], keep="last")
        _save(cache, "futures_price.csv")
    if cache.empty:
        return {"available": False, "reason": "新浪股指期货合约日线不可得"}

    cache["date"] = cache["date"].astype(str)
    spot = {}
    for p, idx in INDEX_FOR.items():
        s = _run(lambda i=idx: ak.index_zh_a_hist(
            symbol=i, period="daily", start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d")), 90)
        if s is not None and not s.empty:
            s = s.copy()
            s["date"] = pd.to_datetime(s["日期"]).dt.strftime("%Y-%m-%d")
            spot[p] = s.set_index("date")["收盘"].astype(float)

    items, series_frames = [], []
    for p in INDEX_FOR:
        if p not in spot or spot[p].empty:
            items.append({"product": p, "available": False, "reason": "现货指数日线不可得"})
            continue
        s = spot[p]
        sub = cache[cache["code"].str.startswith(p)]
        vals = []
        for d in dates[-lookback:]:
            if d not in s.index:
                continue
            live = sorted({c for c in sub["code"].unique() if _expiry(c) >= pd.Timestamp(d)})
            if len(live) < 2:
                continue
            near, nxt = live[0], live[1]
            fn = sub[(sub.code == near) & (sub.date == d)]["close"]
            f2 = sub[(sub.code == nxt) & (sub.date == d)]["close"]
            if fn.empty:
                continue
            days = max((_expiry(near) - pd.Timestamp(d)).days, 1)
            ann = (float(fn.iloc[0]) / float(s[d]) - 1) * 365 / days * 100
            ann2 = ((float(f2.iloc[0]) / float(s[d]) - 1) * 365 /
                    max((_expiry(nxt) - pd.Timestamp(d)).days, 1) * 100) if not f2.empty else None
            vals.append({"date": d, "near": near, "next": nxt,
                         "near_ann": round(ann, 2), "next_ann": None if ann2 is None else round(ann2, 2)})
        if not vals:
            items.append({"product": p, "available": False, "reason": "无连续合约价格"})
            continue
        live = vals[-1]
        hist = pd.Series({v["date"]: v["near_ann"] for v in vals}).sort_index()
        pct = round(float((hist <= live["near_ann"]).mean() * 100), 1)
        items.append({"product": p, "available": True, "asof": live["date"],
                      "near": live["near"], "next": live["next"],
                      "near_ann": live["near_ann"], "next_ann": live["next_ann"],
                      "pct_hist": pct})
        if p == "IF":
            series_frames = [{"date": k, "value": v} for k, v in hist.items()]
    return {
        "available": any(i.get("available") for i in items),
        "items": items,
        "series": series_frames[-260:],
        "note": "年化基差=(期货/现货-1)乘以365/剩余天数; 合约到期日=当月第三个周五; 负值=贴水",
    }


def build_alt_module(dates: list[str]) -> dict:
    t0 = time.time()
    etf = etf_share_module(dates)
    basis = futures_basis_module(dates)
    iv = option_iv_module(dates)
    logger.info("alt 数据模块完成, %.0fs", time.time() - t0)
    return {"etf_share": etf, "futures_basis": basis, "option_iv": iv,
            "asof": dates[-1] if dates else None}


def save_payload(payload: dict) -> Path:
    """落盘第二类数据模块(缓存里, 供 build_snapshot 读取)。"""
    ALT_DIR.mkdir(parents=True, exist_ok=True)
    p = ALT_DIR / "payload.json"
    p.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
    return p


def load_payload() -> dict:
    p = ALT_DIR / "payload.json"
    if not p.exists():
        return {"available": False, "reason": "未运行第二类数据抓取(数据不可得)"}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("读取 alt payload 失败: %s", exc)
        return {"available": False, "reason": str(exc)[:120]}
