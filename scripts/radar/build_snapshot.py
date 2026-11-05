"""构建 App 用快照 data/radar/snapshot.json (同时复制到 Android assets)。

快照字段:
- meta.data_cutoff / generated_at / source_coverage / missing_dates / missing_indicators / notes
- latest(综合分、分位、区间)
- indicators(当前值、历史分位、方向、权重、数据日期)
- series(逐日综合分与沪深300)
- signals / backtest
- indicator_history(周频原始值, 供 App 端在本机重算分位)
"""

from __future__ import annotations

import itertools
import json
import logging
import math
import shutil
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from scripts.radar import alt_inputs, board_score, contrib, pattern_signal, store
from scripts.radar.config import RADAR_CONFIG
from scripts.radar.indicators import (
    confirm_signals,
    daily_history,
    rolling_percentile,
    score_diagnostics,
    trin_source_info,
    weekly_history,
)

if TYPE_CHECKING:
    from pathlib import Path

SNAPSHOT_URL = ("https://raw.githubusercontent.com/17601732/open-stock-data/main/data/radar/snapshot.json")
logger = logging.getLogger(__name__)

# 第二类数据(资金/波动)展示指标; 历史序列由自建缓存积累, 样本不足时只展示不评估贡献度
ALT_INDICATORS = [
    {"key": "alt_etf_net20", "name": "宽基ETF 20日净申购", "group": "资金", "unit": "亿元",
     "direction": "hot_low", "note": "沪深宽基ETF份额20日变化乘以收盘价, 正=净申购; 深市159919不可得"},
    {"key": "alt_if_basis", "name": "IF近月年化基差", "group": "资金", "unit": "%",
     "direction": "hot_low", "note": "负值=贴水; 到期日按当月第三个周五估算"},
    {"key": "alt_iv_50", "name": "50ETF ATM隐含波动率", "group": "波动", "unit": "%",
     "direction": "hot_high", "note": "delta≈0.5 认购/认沽 IV 均值"},
    {"key": "alt_skew_50", "name": "50ETF 25Δ偏斜(skew)", "group": "波动", "unit": "百分点",
     "direction": "hot_low", "note": "25Δ认沽IV - 25Δ认购IV, 越高越恐慌"},
]


def _alt_series(alt: dict) -> dict[str, list[tuple[str, float]]]:
    """把第二类数据模块转成 {指标key: [(date, value)]}。"""
    out: dict[str, list[tuple[str, float]]] = {}
    etf = alt.get("etf_share") or {}
    if etf.get("available"):
        out["alt_etf_net20"] = [(r["date"], r["value"]) for r in etf.get("series") or []
                                if r.get("value") is not None]
    basis = alt.get("futures_basis") or {}
    if basis.get("available"):
        out["alt_if_basis"] = [(r["date"], r["value"]) for r in basis.get("series") or []
                               if r.get("value") is not None]
    iv = alt.get("option_iv") or {}
    if iv.get("available"):
        out["alt_iv_50"] = [(r["date"], r.get("value")) for r in iv.get("series") or []
                            if r.get("value") is not None]
        out["alt_skew_50"] = [(r["date"], r.get("skew")) for r in iv.get("series") or []
                              if r.get("skew") is not None]
    return out


def _alt_display(alt: dict) -> list[dict]:
    """第二类数据指标行; 无数据时显式写"数据不可得", 不填替代值。"""
    series = _alt_series(alt)
    rows = []
    for meta in ALT_INDICATORS:
        s = series.get(meta["key"]) or []
        vals = pd.Series([v for _, v in s], dtype=float).dropna()
        row = {
            "key": meta["key"], "name": meta["name"], "group": meta["group"],
            "weight": 0, "direction": meta["direction"], "scored": False,
            "unit": meta["unit"], "note": meta["note"], "alt": True,
        }
        if vals.empty:
            row.update({"available": False, "value": None, "percentile": None,
                        "date": None, "reason": "数据不可得", "percentile_basis": None})
        else:
            cur = float(vals.iloc[-1])
            pct = round(float((vals <= cur).mean() * 100.0), 1) if len(vals) >= 30 else None
            row.update({
                "available": True, "value": round(cur, 4), "percentile": pct,
                "date": s[-1][0],
                "percentile_basis": f"自建样本(n={len(vals)})" if pct is not None else "样本不足",
            })
        rows.append(row)
    return rows


def _empty_contrib_row(key: str, name: str, group: str, reason: str,
                       n_obs: int = 0) -> dict:
    """无历史/样本不足指标的占位贡献度行(各字段显式 null, 不填替代值)。"""
    null3 = {"20": None, "60": None, "120": None}
    return {
        "key": key, "name": name, "group": group,
        "ic": dict(null3), "ic_ir": dict(null3), "ic_pos": dict(null3), "ic_t": dict(null3),
        "orth_ic": dict(null3), "orth_t": dict(null3),
        "loo_delta_hit": dict(null3), "loo_delta_ic": dict(null3), "loo_delta_maxdd": dict(null3),
        "loo": {}, "n_windows": 0, "n_obs": n_obs,
        "sample_start": None, "sample_end": None,
        "redundant": None, "in_weights": False, "display_only": True,
        "contribution_score": None, "insufficient_history": True, "reason": reason,
    }


def _alt_contribution_rows(series: dict) -> list[dict]:
    """第二类指标按 A 的口径给出贡献度行; 样本不足(远少于 104 周)一律标 insufficient_history。"""
    rows = []
    for meta in ALT_INDICATORS:
        s = series.get(meta["key"]) or []
        row = _empty_contrib_row(meta["key"], meta["name"], meta["group"],
                                 "自建样本不足 104 周, 无法做滚动样本外 IC/LOO(数据不可得或刚上线)", len(s))
        if s:
            row["sample_start"], row["sample_end"] = s[0][0], s[-1][0]
        rows.append(row)
    return rows


def _missing_trading_dates(dates: list[str], reference: pd.Series | None = None) -> list[str]:
    """校验交易日连续性: 优先用指数实际交易日作为日历(避免把法定节假日误判为缺口)。"""
    if not dates:
        return []
    start, end = pd.Timestamp(min(dates)), pd.Timestamp(max(dates))
    have = set(dates)
    expected: list[str] = []
    if reference is not None and len(reference):
        expected = sorted({str(d)[:10] for d in reference.index if start <= pd.Timestamp(d) <= end})
    if not expected:
        try:
            import exchange_calendars as xcals

            cal = xcals.get_calendar("XSHG")
            sched = cal.sessions_in_range(start, end)
            expected = sorted(str(d.date()) for d in sched)
        except Exception:
            expected = [str(d.date()) for d in pd.bdate_range(start, end)]
    return [d for d in expected if d not in have]


def _gap_info(dates: list[str], reference: pd.Series | None) -> list[dict]:
    """把相邻日期的大间隔区分成节假日正常休市 / 真实缺失。

    以指数实际交易日为日历: 间隔内没有任何指数交易日 -> holiday; 有 -> missing 并列出缺失交易日。
    """
    sd = sorted(pd.Timestamp(d) for d in dates)
    ref = sorted(pd.Timestamp(d) for d in reference.index) if reference is not None and len(reference) else []
    out: list[dict] = []
    for a, b in itertools.pairwise(sd):
        days = (b - a).days
        if days <= 3:
            continue
        between = [d for d in ref if a < d < b]
        out.append({
            "from": str(a.date()), "to": str(b.date()), "days": days,
            "type": "missing" if between else "holiday",
            "missing_sessions": [str(d.date()) for d in between],
        })
    return out


def _breadth_check(date: str) -> dict | None:
    """截止日涨跌家数一致性: 涨+跌+平必须等于有数据的样本数(优先全市场 TDX 口径)。"""
    brd = store.read_frame("tdx_breadth.parquet")
    if not brd.empty and store.read_status().get("tdx", {}).get("status") == "ok":
        row = brd[brd["date"].astype(str).str[:10] == date]
        if not row.empty:
            r = row.iloc[0]
            return {"date": date, "up": int(r["up"]), "down": int(r["down"]), "flat": int(r["flat"]),
                    "total": int(r["total"]),
                    "sum_matches_total": bool(int(r["up"]) + int(r["down"]) + int(r["flat"]) == int(r["total"])),
                    "scope": "沪深A股(TDX 全量包, 不含北交所)"}
    st = store.read_frame("stock_daily.parquet")
    if st.empty:
        return None
    sub = st[pd.to_datetime(st["date"]) == pd.Timestamp(date)]
    pct = pd.to_numeric(sub["pct_chg"], errors="coerce").dropna()
    if pct.empty:
        return None
    up, down, flat = int((pct > 0).sum()), int((pct < 0).sum()), int((pct == 0).sum())
    total = len(pct)
    return {"date": date, "up": up, "down": down, "flat": flat, "total": total,
            "sum_matches_total": up + down + flat == total,
            "scope": "样本池(沪深300+中证500), 非全市场"}


def _daily_pct_payload(detail: pd.DataFrame, weights: dict, years: int = 10) -> dict:
    """把 detail 里的日频分位(0-100)压成按日期对齐的整数序列, 供 App 回溯每日构成。"""
    if detail is None or detail.empty:
        return {"dates": [], "values": {}, "note": "无可用分位历史"}
    d = detail.assign(date=pd.to_datetime(detail["date"]))
    piv = d.pivot_table(index="date", columns="key", values="pct", aggfunc="last")
    piv = piv[[k for k in weights if k in piv.columns]]
    if piv.empty:
        return {"dates": [], "values": {}, "note": "无可用分位历史"}
    piv = piv[piv.index >= piv.index.max() - pd.Timedelta(days=365 * years)]
    piv = piv.round(0).astype("Int64")
    return {
        "dates": [str(d.date()) for d in piv.index],
        "values": {k: [None if pd.isna(v) else int(v) for v in piv[k]] for k in piv.columns},
        "note": ("日频分位(0-100 整数, 滚动十年窗口, 因果计算); 可据此复算任意交易日的"
                 "各指标分位/方向化/权重贡献, 不是用当前值代替历史"),
    }


def _valuation_diag(detail: pd.DataFrame, px: pd.Series, weights: dict) -> dict:
    """估值维度诊断: PE 分位波动、与股债性价比共线、正交残差 IC。"""
    vkeys = [k for k in ("hs300_pe", "hs300_pb", "alla_pe", "erp") if k in weights]
    if detail is None or detail.empty or "hs300_pe" not in vkeys:
        return {"available": False}
    d = detail.assign(date=pd.to_datetime(detail["date"]))
    piv = d[d["key"].isin(vkeys)].pivot_table(
        index="date", columns="key", values="oriented", aggfunc="last")
    if "hs300_pe" not in piv.columns or "erp" not in piv.columns:
        return {"available": False}
    pair = piv[["hs300_pe", "erp"]].dropna()
    px = px.sort_index()
    ret60 = (px.shift(-60) / px - 1.0)
    ret60.index = pd.to_datetime(ret60.index)

    def _ic(series: pd.Series) -> dict:
        df = pd.DataFrame({"s": series, "f": ret60.reindex(series.index)}).dropna()
        df = df[df.index >= pd.Timestamp("2021-01-01")]
        if len(df) < 30:
            return {"n": len(df), "ic": None}
        ics = df.groupby(df.index.to_period("M")).apply(
            lambda g: g["s"].rank().corr(g["f"].rank()) if len(g) >= 5 else np.nan)
        ics = ics.dropna()
        if ics.empty:
            return {"n": len(df), "ic": None}
        std = float(ics.std())
        return {"n": int(len(df)), "n_months": int(len(ics)),
                "ic": round(float(ics.mean()), 4),
                "ic_ir": round(float(ics.mean() / std), 3) if std > 0 else None}

    corr = float(pair["hs300_pe"].corr(pair["erp"]))
    spearman = float(pair["hs300_pe"].rank().corr(pair["erp"].rank()))
    x = np.column_stack([np.ones(len(pair)), pair["erp"].to_numpy()])
    beta, *_ = np.linalg.lstsq(x, pair["hs300_pe"].to_numpy(), rcond=None)
    resid = pd.Series(pair["hs300_pe"].to_numpy() - x @ beta, index=pair.index)
    p1 = pair["hs300_pe"].tail(250)
    val_total = sum(float(weights[k]) for k in vkeys)
    total = sum(float(v["weight"]) for v in weights.values())
    return {
        "available": True,
        "weight_share_pct": round(val_total / total * 100, 1),
        "weight_sum": val_total,
        "corr_pearson": round(corr, 3),
        "corr_spearman": round(spearman, 3),
        "r2": round(corr ** 2, 3),
        "pe_pct_1y": {
            "range": round(float(p1.max() - p1.min()), 1),
            "std": round(float(p1.std()), 1),
            "mean_abs_chg_20d": round(float(p1.diff(20).abs().dropna().mean()), 1),
            "flat_share_pct": round(float((p1.diff() == 0).mean()) * 100, 1),
        },
        "ic_oos": {"pe": _ic(pair["hs300_pe"]), "erp": _ic(pair["erp"]),
                   "residual": _ic(resid)},
        "dailyized": bool(RADAR_CONFIG.get("valuation", {}).get("dailyize")),
        "decision_note": ("PE 与 ERP 相关性>0.85, 合并为单一估值维度、上限维持 22 分"
                          "(下调到15分样本外 IC 更差, 未改权重); PE/PB 月度源改为日频价格展开;"
                          "20日均值平滑样本外略差, 默认关闭"),
    }


def _drivers(detail: pd.DataFrame, date: str, sig_type: str, limit: int = 5) -> list[dict]:
    """信号当日贡献最大的前 limit 个指标。

    贡献 = 权重 * (方向化分位 - 50) / 可用权重合计, 即该指标把综合分推离中性(50)的分数;
    顶部取正向最大, 底部取负向最大(把分数往下拉最多的指标)。
    """
    weights = RADAR_CONFIG["composite"]["weights"]
    d = detail[pd.to_datetime(detail["date"]) == pd.Timestamp(date)].copy()
    d = d[d["key"].isin(weights.keys())].dropna(subset=["pct", "oriented"])
    if d.empty:
        return []
    total_w = float(d["weight"].sum())
    if total_w <= 0:
        return []
    d["contrib"] = d["weight"] * (d["oriented"] - 50.0) / total_w
    abs_sum = float(d["contrib"].abs().sum())
    d = d.sort_values("contrib", ascending=(sig_type == "bottom"))
    out = []
    for _, r in d.head(limit).iterrows():
        meta = weights[r["key"]]
        out.append({
            "key": r["key"], "name": meta["name"], "group": meta["group"],
            "weight": round(float(meta["weight"]), 3),
            "percentile": round(float(r["pct"]), 1),
            "oriented": round(float(r["oriented"]), 1),
            "contrib": round(float(r["contrib"]), 1),
            "share": round(abs(float(r["contrib"])) / abs_sum * 100, 1) if abs_sum else None,
        })
    return out


def _signal_extras(detail: pd.DataFrame, date: str, sig_type: str,
                   confirmed_dates: dict[str, str]) -> dict:
    """信号的可解释字段: 贡献前 5 指标 + 是否确认信号。"""
    return {
        "drivers": _drivers(detail, date, sig_type),
        "confirmed": date in confirmed_dates,
        "confirmed_date": confirmed_dates.get(date),
    }


def build_snapshot(comp: pd.DataFrame, detail: pd.DataFrame, signals: pd.DataFrame,
                   backtest_result: dict, indicator_frame: pd.DataFrame,
                   include_history: bool = True,
                   anomalies: list[dict] | None = None,
                   tech: dict | None = None,
                   abtest: dict | None = None,
                   alt: dict | None = None) -> dict:
    """组装快照字典。"""
    weights = RADAR_CONFIG["composite"]["weights"]
    status = store.read_status()

    latest = comp.iloc[-1]
    prev = comp.iloc[-2] if len(comp) > 1 else latest
    latest_date = str(latest["date"])[:10]
    det = detail[pd.to_datetime(detail.date) == pd.Timestamp(latest_date)]

    indicators, missing = [], []
    pcfg = RADAR_CONFIG["percentile"]
    for key, meta in weights.items():
        row = det[(det.key == key)]
        if row.empty or pd.isna(row.iloc[0]["pct"]):
            st = status.get(key, {})
            missing.append({"key": key, "name": meta["name"], "group": meta["group"],
                            "reason": st.get("reason", "当日无有效值")})
            continue
        r = row.iloc[0]
        vdate = None
        if key in indicator_frame.columns:
            s = indicator_frame[key].dropna()
            vdate = str(s.index.max())[:10] if not s.empty else None
        indicators.append({
            "key": key, "name": meta["name"], "group": meta["group"],
            "weight": meta["weight"], "direction": meta["direction"],
            "value": None if pd.isna(r["raw"]) else round(float(r["raw"]), 4),
            "percentile": round(float(r["pct"]), 1),
            "oriented": round(float(r["oriented"]), 1),
            "date": vdate, "scored": True,
        })

    # 仅展示的参考指标(权重 0, 不进入综合分)
    display_cfg = RADAR_CONFIG.get("display", {})
    for key, meta in display_cfg.items():
        if key not in indicator_frame.columns:
            continue
        s = indicator_frame[key].dropna()
        if s.empty:
            continue
        vdate = s.index.max()
        pct_series = rolling_percentile(indicator_frame[key], pcfg["window_days"], pcfg["min_days"])
        pv = pct_series.get(vdate)
        indicators.append({
            "key": key, "name": meta["name"], "group": meta["group"],
            "weight": 0, "direction": "neutral", "scored": False,
            "unit": meta.get("unit"), "note": meta.get("note"),
            "value": round(float(s.iloc[-1]), 4),
            "percentile": None if pv is None or pd.isna(pv) else round(float(pv), 1),
            "oriented": None,
            "date": str(vdate)[:10],
        })

    # 第二类数据(ETF 份额/期货基差/期权 IV): 只展示, 不进入综合分; 无数据写"数据不可得"
    alt = alt if alt is not None else alt_inputs.load_payload()
    alt_rows = _alt_display(alt)
    indicators.extend(alt_rows)
    alt_series = _alt_series(alt)

    idx = store.read_frame("index_daily.parquet")
    px = pd.Series(dtype=float)
    if not idx.empty:
        sub = idx[idx.code == "000300"].copy()
        sub["date"] = pd.to_datetime(sub["date"]).dt.strftime("%Y-%m-%d")
        px = sub.set_index("date")["close"].sort_index()
        # 盘中抓到的当日指数日线尚未收盘, 不能当成最新收盘价(会与 data_cutoff 错位)
        px = px.loc[:latest_date]

    series = pd.DataFrame({"date": comp["date"].astype(str).str[:10], "score": comp["score"].round(2)})
    if "raw_score" in comp.columns:
        series["raw_score"] = comp["raw_score"].round(2)
    series["hs300"] = [None if d not in px.index or pd.isna(px[d]) else round(float(px[d]), 2)
                       for d in series["date"]]
    score_dates = series["date"].tolist()

    sig = signals.copy()
    if len(sig):
        sig["date"] = pd.to_datetime(sig["date"]).dt.strftime("%Y-%m-%d")
        sig["score"] = sig["score"].round(1)

    cf = confirm_signals(comp)
    signals_confirmed = []
    confirmed_dates: dict[str, str] = {}
    if len(cf):
        for _, r in cf.iterrows():
            item = {k: (None if (isinstance(v, float) and pd.isna(v)) else v) for k, v in r.items()}
            item["drivers"] = _drivers(detail, item["date"], item["type"])
            signals_confirmed.append(item)
            confirmed_dates[str(item["date"])] = str(item["confirmed_date"])

    # 每条信号补齐: 贡献前 5 指标 + 是否确认信号
    if len(sig):
        extras = [_signal_extras(detail, d, t, confirmed_dates)
                  for d, t in zip(sig["date"], sig["type"], strict=False)]
        sig = pd.concat([sig.reset_index(drop=True), pd.DataFrame(extras)], axis=1)

    # 回测事件补齐同样的可解释字段与命中标记(顶部 ret<0 命中, 底部 ret>0 命中)
    events = (backtest_result or {}).get("events") or []
    for e in events:
        d = str(e.get("date"))
        e.update(_signal_extras(detail, d, e.get("type", ""), confirmed_dates))
        for h in RADAR_CONFIG["backtest"]["forward_days"]:
            ret = e.get(f"ret{h}")
            if ret is None or pd.isna(ret):
                e[f"hit{h}"] = None
            else:
                e[f"hit{h}"] = bool(ret < 0) if e.get("type") == "top" else bool(ret > 0)
        e["hit"] = e.get("hit20")

    ta = score_diagnostics(comp).copy()
    ta["date"] = pd.to_datetime(ta["date"]).dt.strftime("%Y-%m-%d")
    score_ta = {
        "dates": ta["date"].tolist(),
        "values": {c: [None if pd.isna(v) else round(float(v), 2) for v in ta[c]]
                   for c in ["dif", "dea", "hist", "boll_mid", "boll_up", "boll_low"]},
        "note": "MACD(12,26,9) 与布林带(20,2倍标准差)仅作拐点/背离参考, 不参与综合分",
    }

    detail_keys = ["pcr_raw", "pcr_call_vol", "pcr_put_vol",
                   "margin_fin_bal", "margin_short_bal", "margin_total",
                   "margin_fin_buy", "margin_short_sell_vol",
                   "holder_inc", "holder_dec", "holder_net",
                   "holder_inc_count", "holder_dec_count"]
    hist_keys = list(weights) + list(RADAR_CONFIG.get("display", {})) + detail_keys
    indicator_daily = daily_history(indicator_frame, hist_keys) if include_history else {}
    daily_pct = _daily_pct_payload(detail, weights) if include_history else {}
    valuation_diag = _valuation_diag(detail, px, weights)

    # ---- 指标边际贡献(滚动样本外, 描述性统计; 不据此重算权重) ----
    meta_lookup: dict[str, dict] = {k: dict(v) for k, v in weights.items()}
    meta_lookup.update({k: dict(v) for k, v in RADAR_CONFIG.get("display", {}).items()})
    meta_lookup.update({m["key"]: dict(m) for m in ALT_INDICATORS})
    try:
        hist_all = (weekly_history(indicator_frame,
                                   list(weights) + list(RADAR_CONFIG.get("display", {})))
                    if include_history else {})
        weights_map = {k: float(v["weight"]) for k, v in weights.items()}
        directions_map = {k: v["direction"] for k, v in weights.items()}
        for k in RADAR_CONFIG.get("display", {}):
            directions_map.setdefault(k, "hot_high")
        contr = contrib.compute_contributions(
            {"indicator_history": hist_all, "series": series.to_dict("records")},
            weights_map, directions_map,
            RADAR_CONFIG["composite"]["top_threshold"],
            RADAR_CONFIG["composite"]["bottom_threshold"])
        rows = contr.get("indicators") or []
        for r in rows:
            m = meta_lookup.get(r["key"], {})
            r["name"] = m.get("name", r["key"])
            r["group"] = m.get("group", "其他")
            r["in_weights"] = r["key"] in weights
            r["display_only"] = not r["in_weights"]
            r["insufficient_history"] = bool(
                r.get("n_windows", 0) == 0 and not (r.get("ic") or {}).get("60"))
        covered = {r["key"] for r in rows} | {m["key"] for m in ALT_INDICATORS}
        rows.extend(_alt_contribution_rows(alt_series))
        for key, m in meta_lookup.items():
            if key in covered:
                continue
            rows.append(_empty_contrib_row(key, m.get("name", key), m.get("group", "其他"),
                                           "该指标无独立历史序列(仅当日展示值), 无法计算 IC/LOO"))
            rows[-1]["in_weights"] = key in weights
            rows[-1]["display_only"] = key not in weights
        rows.sort(key=lambda r: (r["contribution_score"] is None,
                                 -(r["contribution_score"] or 0.0)))
        for i, r in enumerate(rows, 1):
            r["rank"] = i
        contrib_payload = {
            "available": bool(rows),
            "method": contr.get("method"),
            "weighting": contr.get("weighting"),
            "weight_change": "权重未动: 贡献度仅为描述性统计, 未用于重算 composite.weights",
            "primary_horizon": contr.get("primary_horizon", 60),
            "baseline": contr.get("baseline"),
            "baseline_score_ic": contr.get("baseline_score_ic"),
            "n_indicators": len(rows),
            "indicators": rows,
        }
    except Exception as exc:
        logger.warning("指标贡献度计算失败: %s", str(exc)[:160])
        contrib_payload = {"available": False, "reason": str(exc)[:160], "indicators": []}

    latest_close = px.iloc[-1] if len(px) else None
    score = float(latest["score"])
    zone = ("top" if score >= RADAR_CONFIG["composite"]["top_threshold"]
            else "bottom" if score <= RADAR_CONFIG["composite"]["bottom_threshold"] else "neutral")

    notes = [
        "交易日历以沪深300指数实际交易日为准; meta.gap_info 中 holiday=节假日正常休市, missing=真实缺失",
        "两融余额由交易所 T+1/T+2 发布, 截止日当日可能无值, 属正常发布滞后(不作为缺口); 前值填充窗口按指标配置",
        "涨跌家数/成交额/TRIN 分母默认用通达信全量包(沪深A股约5200只, 不含北交所); 涨跌停家数仍为样本池近似计数, 非全市场精确口径",
        "两融余额为沪深两市合计, 分母用样本池流通市值, 绝对水平偏高但历史分位口径一致",
        "涨跌停家数为样本池内|涨跌幅|>=9.5%的近似计数, 非全市场精确口径(未区分主板/创业板/ST)",
        "期权PCR仅覆盖实际可得区间(起点见 meta.source_coverage.option_pcr), 缺失日期不做前向填充",
        "估值类为乐咕乐股口径, 沪深300 PE/PB 为月度频率, 已按日对齐(近端阶梯状)",
        "个股使用不复权价格; 同日形态(上影/振幅/放量)不受影响, 跨日收益/均线/N日涨幅会受除权污染, 已在除权日打标并在板块回测中剔除跨除权样本",
        "分位为滚动十年窗口(不足十年用扩展窗口, 至少250个观测), 严格无未来函数",
        "回测为事件研究, 非交易策略回测; 2021年起为样本外",
        "增减持明细按公告日聚合(公告日不等于增减持发生日); 无公告的交易日留空, 不做 0 填充, 聚合时「无公告」与「净额为零」可区分",
        "两融明细口径: 融资余额/融券余额/合计为存量(周/月取期末值), 融资买入额/融券卖出量为流量(周/月求和), 两融余额占比为比率(周/月取均值)",
        "TRIN 日频原始值等参考指标为展示项(权重 0), 不参与综合分; 综合分权重与阈值均未改动",
        "个股/涨跌家数默认来自通达信盘后全量包 hsjday.zip(TDX), 逐只接口仅在包解析失败或个别缺码时兜底, "
        "meta.stock_source 标注本次实际路径",
        "除权日以东方财富分红送配日历(stock_fhps_em 的除权除息日)为权威口径打标, "
        "抽样校验精确率/召回均约95%; 无可靠复权因子, 因此不对外推算复权价, 跨除权样本直接剔除",
        "板块风险提醒只用行业板块(申万一级, 31 个, 覆盖沪深创业板/科创板约 88% 活跃个股, 不含北交所); "
        "题材/概念成分无干净且可回溯的数据源, 未提供",
        "板块形态为风险提示, 不参与综合分; 参数可在 App 内调整(只影响本地展示与阈值, 不改变历史统计口径)",
        "指标贡献度为滚动样本外描述性统计(104周窗口/13周步长, 前瞻20/60/120交易日), "
        "仅用于排序与展示, 未据此重算综合分权重(权重未动)",
        "第二类数据(宽基ETF份额/股指期货基差/期权IV与skew)仅展示, 不进入综合分; "
        "深市159919份额与中金所官方结算接口不可得, 均显式标注为数据不可得",
    ]

    try:
        pattern = pattern_signal.build_pattern_module(include_history=include_history)
    except Exception as exc:
        logger.warning("板块形态模块失败: %s", str(exc)[:160])
        pattern = {"available": False, "reason": str(exc)[:160]}

    try:
        boards = board_score.build_board_module(include_history=include_history)
    except Exception as exc:
        logger.warning("板块顶底模块失败: %s", str(exc)[:160])
        boards = {"available": False, "reason": str(exc)[:160]}

    trin_info = trin_source_info()
    indicator_gaps: dict[str, dict] = {}
    if trin_info.get("missing_dates") or trin_info.get("status") != "ok":
        indicator_gaps["trin_raw"] = trin_info
        indicator_gaps["trin_30d"] = trin_info

    snapshot = {
        "meta": {
            "generated_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"),
            "data_cutoff": latest_date,
            "latest_index_date": str(px.index.max())[:10] if len(px) else None,
            "source_coverage": status,
            "missing_dates": _missing_trading_dates(score_dates, px)[-60:],
            "gap_info": _gap_info(score_dates, px),
            "breadth_check": _breadth_check(latest_date),
            "trading_calendar": "沪深300指数实际交易日",
            "missing_indicators": missing,
            "indicator_gaps": indicator_gaps,
            "trin_last_date": trin_info.get("last_date"),
            "anomalies": anomalies or [],
            "notes": notes,
            "display_indicators": [{"key": k, **v} for k, v in RADAR_CONFIG.get("display", {}).items()],
            "stock_source": status.get("stock_daily", {}).get("stock_source", "unknown"),
            "tdx": status.get("tdx"),
            "boards": status.get("boards"),
            "ex_rights": status.get("ex_rights"),
            "ex_rights_flags": status.get("ex_rights_flags"),
            "snapshot_url": SNAPSHOT_URL,
            "schema_version": 6,
        },
        "latest": {
            "date": latest_date,
            "score": round(score, 1),
            "prev_score": round(float(prev["score"]), 1),
            "coverage": round(float(latest["coverage"]), 3),
            "hs300": None if latest_close is None or pd.isna(latest_close) else round(float(latest_close), 2),
            "zone": zone,
        },
        "thresholds": {
            "top": RADAR_CONFIG["composite"]["top_threshold"],
            "bottom": RADAR_CONFIG["composite"]["bottom_threshold"],
        },
        "indicators": indicators,
        "indicator_contributions": contrib_payload,
        "alt_indicators": alt,
        "series": series.to_dict("records"),
        "signals": sig.to_dict("records") if len(sig) else [],
        "backtest": backtest_result,
        "indicator_history": weekly_history(indicator_frame, list(weights)) if include_history else {},
        "indicator_daily": indicator_daily,
        "indicator_daily_pct": daily_pct,
        "valuation_diag": valuation_diag,
        "score_ta": score_ta,
        "signals_confirmed": signals_confirmed,
        "pattern": pattern,
        "boards": boards,
        "tech": tech or {"available": False},
        "tech_abtest": abtest or {"available": False},
    }
    return snapshot


def _json_safe(obj):
    """递归把 NaN/Infinity 转成 None。

    Android 端 org.json 遇到裸 NaN 会抛 "Forbidden numeric value: NaN",
    整份快照都无法解析(实测 signals[].confirmed_date)。
    """
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj


def write_snapshot(snapshot: dict, android_assets: Path | None = None) -> Path:
    """写入 data/radar/snapshot.json, 可选复制到 Android assets。"""
    path = store.ensure_dir() / "snapshot.json"
    path.write_text(json.dumps(_json_safe(snapshot), ensure_ascii=False,
                             separators=(",", ":"), allow_nan=False), encoding="utf-8")
    if android_assets:
        android_assets.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, android_assets / "snapshot.json")
    return path
