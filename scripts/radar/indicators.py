"""指标计算: 由原始数据得到各单项指标原始值, 全部只用当日及之前数据。

统一口径说明:
- 宽度/换手基于固定样本池(沪深300+中证500 当前成分), 存在生存者偏差
- TRIN 优先复用仓库全市场产物 trin_2020_now.csv(2020 起), 缺失时退回样本池估算
- 52周新高低、MA60 使用不复权收盘价, 除权日附近存在轻微断层
- 涨跌停家数用样本池内 |涨跌幅|≥9.5% 近似, 不是全市场精确口径
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from scripts.radar import store
from scripts.radar.config import RADAR_CONFIG

EPS = 1e-9
TRIN_CSV = Path(__file__).resolve().parents[2] / "trin_2020_now.csv"


def _repo_trin() -> pd.DataFrame | None:
    """复用仓库既有全市场 TRIN 产物(scripts/calc_trin_30d.py 每日更新 trin_2020_now.csv)。

    只在 CSV 存在且字段完整时覆盖样本池估算, 保证与已有 TRIN 口径一致;
    CSV 缺失时退回样本池计算, 并在快照元数据中标注数据来源。
    """
    if not TRIN_CSV.exists():
        return None
    try:
        d = pd.read_csv(TRIN_CSV)
    except Exception:
        return None
    need = {"date", "trin_raw", "trin_ma30"}
    if not need.issubset(set(d.columns)) or d.empty:
        return None
    d["date"] = pd.to_datetime(d["date"]).dt.strftime("%Y-%m-%d")
    return d[["date", "trin_raw", "trin_ma30"]].dropna().drop_duplicates("date").set_index("date").sort_index()


def _completed_session(trading: pd.DatetimeIndex) -> pd.Timestamp:
    """最后一个已完成交易日(北京时间 15:30 前, 当天日线仍属盘中未完成数据)。"""
    now = datetime.now(UTC) + timedelta(hours=8)
    today = pd.Timestamp(now.date())
    if now.hour * 60 + now.minute < 15 * 60 + 30 and today in trading:
        prior = trading[trading < today]
        if len(prior):
            return prior.max()
    return trading.max()


def _pool_panel() -> pd.DataFrame:
    """样本池个股面板, 附带 MA60 与 52 周高低点。"""
    df = store.read_frame("stock_daily.parquet")
    if df.empty:
        return df
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["code", "date"])
    g = df.groupby("code", group_keys=False)
    df["ma60"] = g["close"].transform(lambda s: s.rolling(60, min_periods=40).mean())
    df["hi250"] = g["close"].transform(lambda s: s.rolling(250, min_periods=60).max())
    df["lo250"] = g["close"].transform(lambda s: s.rolling(250, min_periods=60).min())
    tr = df["turnover"].clip(lower=0.01)
    df["float_cap"] = df["amount"] / (tr / 100.0)
    return df


def trin_source_info() -> dict:
    """TRIN 数据来源与缺口诊断(供快照元数据与 App 详情页提示)。

    结论: trin_raw/trin_30d 复用仓库 trin_2020_now.csv, 该文件由
    scripts/calc_trin_30d.py 从新浪「当日全市场快照」生成, 上游没有历史回溯接口,
    因此历史缺失日无法任意补抓, 只能靠每日运行落库(次日开盘前运行可补回上一交易日)。
    """
    csv = _repo_trin()
    idx = store.read_frame("index_daily.parquet")
    info: dict = {
        "key": "trin_raw",
        "source": "trin_2020_now.csv (Sina 当日全市场快照)",
        "recoverable": False,
        "upstream": "Market_Center.getHQNodeData (snapshot only)",
    }
    if csv is None:
        info.update({"status": "missing", "last_date": None, "missing_dates": [],
                     "note": "TRIN CSV 不存在, 已退回样本池估算"})
        return info
    info["last_date"] = str(csv.index.max())[:10]
    info["start_date"] = str(csv.index.min())[:10]
    if not idx.empty:
        cal = sorted({str(d)[:10] for d in pd.to_datetime(idx.loc[idx.code == "000300", "date"])})
        have = {pd.Timestamp(d).strftime("%Y-%m-%d") for d in csv.index}
        missing = [d for d in cal if info["start_date"] <= d <= cal[-1] and d not in have]
        info["missing_dates"] = missing
        info["status"] = "partial" if missing else "ok"
    else:
        info["missing_dates"] = []
        info["status"] = "unknown"
    return info


def build_indicators() -> pd.DataFrame:
    """返回 index=date(交易日), 列=各指标原始值的 DataFrame。"""
    out: dict[str, pd.Series] = {}

    val = store.read_frame("valuation.parquet")
    if not val.empty:
        val["date"] = pd.to_datetime(val["date"])
        for code, pre in [("hs300", "hs300"), ("alla", "alla")]:
            sub = val[val.code == code].dropna(subset=["pe"]).set_index("date").sort_index()
            if len(sub):
                out[f"{pre}_pe"] = sub["pe"]
            sub_pb = val[val.code == code].dropna(subset=["pb"]).set_index("date").sort_index()
            if len(sub_pb):
                out[f"{pre}_pb"] = sub_pb["pb"]

    bond = store.read_frame("bond_yield.parquet")
    if not bond.empty:
        bond["date"] = pd.to_datetime(bond["date"])
        out["cn10y"] = bond.set_index("date")["cn10y"].sort_index()

    panel = _pool_panel()
    pool_cap = pd.Series(dtype=float)
    if not panel.empty:
        by = panel.groupby("date")
        adv = by.apply(lambda d: (d.pct_chg > 0).sum(), include_groups=False)
        dec = by.apply(lambda d: (d.pct_chg < 0).sum(), include_groups=False)
        amt_up = by.apply(lambda d: d.amount[d.pct_chg > 0].sum(), include_groups=False)
        amt_dn = by.apply(lambda d: d.amount[d.pct_chg < 0].sum(), include_groups=False)

        out["adv_pct_5d"] = ((adv / (adv + dec + EPS)).rolling(5, min_periods=1).mean())
        trin = ((adv + 0.5) / (dec + 0.5)) / ((amt_up + 1.0) / (amt_dn + 1.0))
        out["trin_30d"] = trin.rolling(30, min_periods=10).mean()
        out["trin_raw"] = trin
        out["turnover"] = by["turnover"].median()
        cap = by["float_cap"].sum()
        pool_cap = cap
        out["amount_floatcap"] = by["amount"].sum() / (cap + EPS)
        out["above_ma60"] = by.apply(
            lambda d: (d.close > d.ma60).sum() / max(int(d.ma60.notna().sum()), 1), include_groups=False)
        nh = by.apply(lambda d: (d.close >= d.hi250).sum(), include_groups=False)
        nl = by.apply(lambda d: (d.close <= d.lo250).sum(), include_groups=False)
        out["nh_nl"] = nh - nl
        out["nh_nl_ema5"] = out["nh_nl"].ewm(span=5, adjust=False).mean()
        out["nh_nl_ema20"] = out["nh_nl"].ewm(span=20, adjust=False).mean()
        breadth = adv / (adv + dec + EPS)
        out["breadth_ema5"] = breadth.ewm(span=5, adjust=False).mean()
        out["breadth_ema20"] = breadth.ewm(span=20, adjust=False).mean()
        zt = by.apply(lambda d: (d.pct_chg >= 9.5).sum(), include_groups=False)
        dt = by.apply(lambda d: (d.pct_chg <= -9.5).sum(), include_groups=False)
        out["limit_net"] = (zt - dt).rolling(5, min_periods=1).mean()
        out["pool_size"] = by.size()

    mar = store.read_frame("margin_daily.parquet")
    if not mar.empty:
        mar["date"] = pd.to_datetime(mar["date"])
        mi = mar.set_index("date").sort_index()
        if not pool_cap.empty and "total" in mi.columns:
            m = mi["total"]
            cap_al = pool_cap.reindex(pool_cap.index.union(m.index)).ffill().reindex(m.index)
            out["margin_ratio"] = m / (cap_al + EPS)
        # 详情页: 存量(融资余额/融券余额/两融合计) + 流量(融资买入额/融券卖出量)
        for dst, src in {"margin_fin_bal": "fin_bal", "margin_short_bal": "short_bal",
                         "margin_total": "total", "margin_fin_buy": "fin_buy",
                         "margin_short_sell_vol": "short_sell_vol"}.items():
            if src in mi.columns:
                out[dst] = mi[src]

    idx = store.read_frame("index_daily.parquet")
    if not idx.empty:
        px = idx[idx.code == "000300"].copy()
        px["date"] = pd.to_datetime(px["date"])
        px = px.set_index("date")["close"].sort_index()
        out["hs300_close"] = px
        ma250 = px.rolling(250, min_periods=120).mean()
        out["bias_yearline"] = px / (ma250 + EPS) - 1.0
        out["vol20"] = px.pct_change().rolling(20, min_periods=15).std() * np.sqrt(244)

    pcr = store.read_frame("option_pcr_etf.csv")
    if not pcr.empty:
        pcr["date"] = pd.to_datetime(pcr["date"])
        p = pcr.set_index("date").sort_index()
        out["pcr_5d"] = p["pcr"].rolling(5, min_periods=1).mean()
        # 详情页需要的日频原始 PCR 与认购/认沽成交量(总量口径)
        out["pcr_raw"] = p["pcr"]
        out["pcr_call_vol"] = p["call_vol"] if "call_vol" in p.columns else np.nan
        out["pcr_put_vol"] = p["put_vol"] if "put_vol" in p.columns else np.nan

    hc = store.read_frame("holder_change.parquet")
    if not hc.empty:
        hc["date"] = pd.to_datetime(hc["date"])
        inc = hc["direction"].astype(str).str.contains("增持|买入|increase", case=False, na=False)
        signed = pd.Series(np.where(inc, hc["value"].abs(), -hc["value"].abs()), index=hc.index)
        wk = signed.groupby(hc["date"]).sum().resample("W").sum()
        out["net_increase_20w"] = wk.rolling(20, min_periods=4).sum()
        # 详情页日频明细(按公告日聚合, 单位: 万股 / 家)
        out["holder_inc"] = hc[inc].groupby("date")["value"].sum()
        out["holder_dec"] = hc[~inc].groupby("date")["value"].sum()
        out["holder_net"] = out["holder_inc"].sub(out["holder_dec"], fill_value=0)
        out["holder_inc_count"] = hc[inc].groupby("date")["code"].nunique()
        out["holder_dec_count"] = hc[~inc].groupby("date")["code"].nunique()

    # 优先复用仓库既有全市场 TRIN(2020 起); 缺失时用上面的样本池估算
    repo_trin = _repo_trin()
    if repo_trin is not None:
        idx = pd.to_datetime(repo_trin.index)
        out["trin_raw"] = pd.Series(repo_trin["trin_raw"].to_numpy(dtype=float), index=idx)
        out["trin_30d"] = pd.Series(repo_trin["trin_ma30"].to_numpy(dtype=float), index=idx)

    df = pd.DataFrame(out).sort_index()
    if "net_increase_20w" in df.columns:
        df["net_increase_20w"] = df["net_increase_20w"].ffill()

    # 统一到交易日网格: 低频(月度估值/周度增减持)按有限步长前向填充, 只搬已发布的历史值
    trading = df.index
    idx = store.read_frame("index_daily.parquet")
    if not idx.empty:
        trading = pd.DatetimeIndex(sorted(pd.to_datetime(
            idx.loc[idx.code == "000300", "date"]).unique()))
    df = df.reindex(trading.union(df.index)).sort_index()
    limits = {"hs300_pe": 62, "hs300_pb": 62, "alla_pe": 5, "erp": 0,
              "net_increase_20w": 15, "pcr_5d": 8, "cn10y": 8,
              "holder_inc": 0, "holder_dec": 0, "holder_net": 0,
              "holder_inc_count": 0, "holder_dec_count": 0,
              "margin_fin_buy": 0, "margin_short_sell_vol": 0}
    for col in df.columns:
        limit = limits.get(col, 8)
        if limit:
            df[col] = df[col].ffill(limit=limit)
    df = df.reindex(trading)
    if not idx.empty and len(trading):
        # 只保留已完成交易日: 北京时间 15:30 前抓到当天的指数日线是盘中未完成值,
        # 若把它当成收盘数据会导致快照 cutoff 提前一天、指标失真(如 MA60 掉值)。
        df = df.loc[: _completed_session(trading)]

    # 增减持是流量: 无公告的交易日保持缺失, 不做 0 填充。
    # 这样「当月无公告(无数据)」与「当月有公告但增持=减持(净额为零)」在聚合时才可区分。

    if "hs300_pe" in df.columns and "cn10y" in df.columns:
        df["erp"] = 1.0 / df["hs300_pe"] - df["cn10y"] / 100.0
    if "pcr_5d" in df.columns:
        # 展示项: 5日均PCR 的5日变化率(%), 比绝对水平更有参考价值
        df["pcr_chg5"] = df["pcr_5d"].pct_change(5) * 100.0
    return df.drop(columns=["cn10y"], errors="ignore")


def rolling_percentile(s: pd.Series, window: int, min_days: int) -> pd.Series:
    """当日值在滚动窗口内的历史分位(0-100), 严格只看当日及之前。"""
    vals = s.values.astype(float)
    out = np.full(len(vals), np.nan)
    for i in range(len(vals)):
        if np.isnan(vals[i]):
            continue
        w = vals[max(0, i - window + 1): i + 1]
        w = w[~np.isnan(w)]
        if len(w) < min_days:
            continue
        out[i] = float((w <= vals[i]).mean() * 100.0)
    return pd.Series(out, index=s.index)


def screen_anomalies(ind: pd.DataFrame, k: float = 12.0) -> tuple[pd.DataFrame, list[dict]]:
    """异常值筛查: 单日变动超过 12 倍滚动252日MAD 且幅度超过中位水平 12% 时标记可疑, 置为缺失。

    比率型派生指标(股债性价比/TRIN/PCR)本身波动大, 阈值放宽, 避免把正常波动误判为异常。
    """
    cleaned = ind.copy()
    flags: list[dict] = []
    loose = {"erp", "trin_30d", "trin_raw", "pcr_5d", "pcr_510050", "pcr_510300"}
    for key in ind.columns:
        s = ind[key].astype(float)
        if s.notna().sum() < 60:
            continue
        # 按自然日间隔归一: 长假后的首日跳变不应被当成异常(否则会误删整段节后数据)
        steps = pd.Series(s.index, index=s.index).diff().dt.days.fillna(1).clip(lower=1)
        diff = s.diff().abs() / steps
        mad = diff.rolling(252, min_periods=40).median()
        level = s.abs().rolling(252, min_periods=40).median()
        mult = 20.0 if key in loose else k
        thresh = 0.20 if key in loose else 0.12
        suspicious = (diff > mult * mad) & (diff > thresh * level) & mad.notna() & (mad > 0)
        for dt in s.index[suspicious.fillna(False)]:
            flags.append({"date": str(dt)[:10], "key": key, "from": _safe_round(s.shift(1)[dt]),
                          "to": _safe_round(s[dt])})
        cleaned.loc[suspicious.fillna(False), key] = np.nan
    flags.sort(key=lambda x: x["date"], reverse=True)
    return cleaned, flags[:100]


def _safe_round(v) -> float | None:
    """安全取整, 缺失返回 None。"""
    try:
        return None if pd.isna(v) else round(float(v), 4)
    except (TypeError, ValueError):
        return None


def build_composite(ind: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """分位标准化 + 加权合成; 缺失指标按可用权重重新归一, 覆盖率不足则当日无分。"""
    cfg = RADAR_CONFIG["composite"]
    pcfg = RADAR_CONFIG["percentile"]
    weights = cfg["weights"]

    pct, oriented = {}, {}
    for key, meta in weights.items():
        if key not in ind.columns:
            continue
        p = rolling_percentile(ind[key], pcfg["window_days"], pcfg["min_days"])
        pct[key] = p
        oriented[key] = p if meta["direction"] == "hot_high" else 100.0 - p
    if not oriented:
        return pd.DataFrame(columns=["date", "score", "coverage"]), pd.DataFrame()

    ori = pd.DataFrame(oriented)
    w = pd.Series({k: m["weight"] for k, m in weights.items() if k in ori.columns})
    avail = ori.notna()
    cov = avail.mul(w, axis=1).sum(axis=1) / w.sum()
    score = ori.fillna(0).mul(w, axis=1).sum(axis=1) / avail.mul(w, axis=1).sum(axis=1).replace(0, np.nan)
    score = score.where(cov >= cfg["min_weight_coverage"])
    raw_score = score.copy()

    if cfg.get("calibrate", False):
        # 校准: 加权均值 → 其自身滚动十年历史分位(0-100), 同样无未来函数
        # 校准用较短的 min_days(60), 避免把 2015 年头部数据整体丢掉
        score = rolling_percentile(score.dropna(), pcfg["window_days"],
                                   cfg.get("calibrate_min_days", 60))
        score = score.reindex(raw_score.index)

    comp = pd.DataFrame({"date": score.index, "score": score.values,
                         "coverage": cov.values, "raw_score": raw_score.values})
    comp = comp.dropna(subset=["score"]).reset_index(drop=True)
    comp["raw_score"] = comp["raw_score"].round(2)

    details = []
    for key in ori.columns:
        details.append(pd.DataFrame({
            "date": ori.index, "key": key,
            "raw": ind[key].values if key in ind.columns else np.nan,
            "pct": pct[key].values, "oriented": ori[key].values, "weight": w[key],
        }))
    detail = pd.concat(details, ignore_index=True)
    return comp, detail


def extract_signals(comp: pd.DataFrame) -> pd.DataFrame:
    """阈值穿越触发信号, 同向信号冷却 cooldown_days 个交易日。"""
    cfg = RADAR_CONFIG["composite"]
    top, bottom = cfg["top_threshold"], cfg["bottom_threshold"]
    cool = RADAR_CONFIG["backtest"]["cooldown_days"]
    s = comp.set_index("date")["score"]
    events, last = [], {"top": -10**9, "bottom": -10**9}
    prev = np.nan
    for i, (dt, v) in enumerate(s.items()):
        if not np.isnan(prev):
            if prev < top <= v and i - last["top"] > cool:
                events.append({"date": dt, "type": "top", "score": float(v)})
                last["top"] = i
            elif prev > bottom >= v and i - last["bottom"] > cool:
                events.append({"date": dt, "type": "bottom", "score": float(v)})
                last["bottom"] = i
        prev = v
    return pd.DataFrame(events)


def weekly_history(ind: pd.DataFrame, keys: list[str], years: int = 10) -> dict:
    """导出周频原始值历史, 供 App 端在本机重算分位(体积远小于日频)。"""
    out = {}
    for key in keys:
        if key not in ind.columns:
            continue
        s = ind[key].dropna()
        s = s[s.index >= s.index.max() - pd.Timedelta(days=365 * years)]
        s = s.resample("W").last().dropna()
        out[key] = [[str(d.date()), round(float(v), 4)] for d, v in s.items()]
    return out


def daily_history(ind: pd.DataFrame, keys: list[str]) -> dict:
    """导出日频原始序列(共享日期轴 + 各指标等长数组), 供 App 图表缩放/平移使用。"""
    if ind.empty:
        return {}
    dates = [str(d)[:10] for d in ind.index]
    values = {}
    for key in keys:
        if key not in ind.columns:
            continue
        values[key] = [None if pd.isna(v) else round(float(v), 4) for v in ind[key].to_numpy()]
    return {"dates": dates, "values": values,
            "start": dates[0], "end": dates[-1], "note": "日频, 与 dates 等长; null=当日无有效值"}


def score_diagnostics(comp: pd.DataFrame) -> pd.DataFrame:
    """综合分的技术诊断: MACD(12,26,9) 与布林带(20, 2倍标准差)。仅展示, 不参与评分。"""
    s = comp.set_index("date")["score"].astype(float)
    dif = s.ewm(span=12, adjust=False).mean() - s.ewm(span=26, adjust=False).mean()
    dea = dif.ewm(span=9, adjust=False).mean()
    mid = s.rolling(20, min_periods=20).mean()
    std = s.rolling(20, min_periods=20).std(ddof=0)
    return pd.DataFrame({
        "date": s.index,
        "dif": dif.values,
        "dea": dea.values,
        "hist": (dif - dea).values * 2,
        "boll_mid": mid.values,
        "boll_up": (mid + 2 * std).values,
        "boll_low": (mid - 2 * std).values,
    })


def confirm_signals(comp: pd.DataFrame, delay: int = 3) -> pd.DataFrame:
    """实验项: 阈值触发后需连续 delay 个交易日仍处于同一区间, 才计入确认信号。"""
    cfg = RADAR_CONFIG["composite"]
    top, bottom = cfg["top_threshold"], cfg["bottom_threshold"]
    s = comp.set_index("date")["score"].astype(float)
    pos = {d: i for i, d in enumerate(s.index)}
    events = extract_signals(comp)
    rows = []
    for _, e in events.iterrows():
        j = pos.get(e["date"], -1) + delay
        if j < 0 or j >= len(s):
            continue
        v = s.iloc[j]
        if pd.isna(v):
            continue
        if (e["type"] == "top" and v >= top) or (e["type"] == "bottom" and v <= bottom):
            rows.append({"date": str(e["date"])[:10],
                         "confirmed_date": str(s.index[j])[:10],
                         "type": e["type"], "score": round(float(e["score"]), 1),
                         "score_confirmed": round(float(v), 1), "delay": delay})
    return pd.DataFrame(rows)
