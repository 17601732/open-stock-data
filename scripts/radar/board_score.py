"""板块顶底: 申万一级行业的日频指标、自身历史分位打分、信号与多重比较校正验证。

数据口径(全部来自本地 parquet, 缺什么就标注缺失, 不用全市场值顶替):
- 个股: tdx_stock.parquet(通达信全量包, 含 open/high/low/close/prev_close/volume/amount/ex_rights)
- 成分: board_members.parquet(申万一级 31 行业, 当前成分快照, 按当前成分回溯 -> 存在成分漂移偏差)
- 指数: 板块等权指数由成分股日收益等权平均累乘; 市值加权需要个股流通市值, 本数据集没有该字段,
  因此以「成交额加权指数」替代并明确标注, 不冒充市值加权。
- 个股行情仅覆盖约 3 年(2023-11 起), 分位用扩展窗口(min_days=120), 不能与大盘 2015 起的结论直接对比。

打分: 7 个指标各自做板块自身历史分位, 等权平均后再做自身历史分位映射到 0-100;
高=过热(顶部风险), 低=过冷(底部机会); 阈值默认顶 85 / 底 15, 同向冷却 20 个交易日。

验证: 逐板块事件研究(20/60/120 日), 与「该板块全部交易日(随机同日基线)」比较胜率与均值;
31 个板块 x 2 方向 x 3 周期同时检验, 必须报告原始 p、Bonferroni 与 BH-FDR 校正结果;
另做同日横截面检验, 降低逐板块时序检验被噪音骗的概率。
"""

from __future__ import annotations

import logging
import math

import numpy as np
import pandas as pd

from scripts.radar import store
from scripts.radar.config import RADAR_CONFIG

logger = logging.getLogger(__name__)


def _expand_pct(s: pd.Series, min_days: int) -> pd.Series:
    """扩展窗口历史分位(0-100), 只用当日及之前的数据。"""
    v = s.to_numpy(dtype=float)
    out = np.full(len(v), np.nan)
    for i in range(len(v)):
        if np.isnan(v[i]):
            continue
        w = v[: i + 1]
        w = w[~np.isnan(w)]
        if len(w) >= min_days:
            out[i] = float((w <= v[i]).mean() * 100.0)
    return pd.Series(out, index=s.index)


def _metrics(stock: pd.DataFrame, boards: pd.DataFrame, pcfg: dict) -> dict:
    """逐股派生字段 -> 板块 x 日期聚合 -> 宽表指标。"""
    df = stock.copy()
    df["date"] = pd.to_datetime(df["date"].astype(str), format="%Y%m%d")
    df = df.sort_values(["symbol", "date"], kind="mergesort")
    g = df.groupby("symbol", sort=False)

    df["ret"] = df["close"] / df["prev_close"].replace(0, np.nan) - 1.0
    df.loc[df["ex_rights"].fillna(False), "ret"] = np.nan
    ma60 = g["close"].transform(lambda s: s.rolling(60, min_periods=60).mean())
    df["above60"] = np.where(ma60.notna(), (df["close"] >= ma60).astype(float), np.nan)

    # 放量长上影(与 pattern_signal 同一套形态定义): 均量取前 20 日(不含当日)
    w = int(RADAR_CONFIG["pattern"]["volume_window"])
    vol_ma = g["volume"].transform(
        lambda s: s.shift(1).rolling(w, min_periods=max(5, w // 2)).mean())
    rng = (df["high"] - df["low"]).replace(0, np.nan)
    upper = df["high"] - df[["open", "close"]].max(axis=1)
    body = (df["close"] - df["open"]).abs()
    amp = rng / df["prev_close"].replace(0, np.nan)
    shadow = (amp >= float(RADAR_CONFIG["pattern"]["min_amplitude"])) & (
        upper / rng >= float(RADAR_CONFIG["pattern"]["shadow_ratio"])) & (
        upper >= float(RADAR_CONFIG["pattern"]["shadow_body_mult"]) * body)
    surge = df["volume"] / vol_ma.replace(0, np.nan) >= float(
        RADAR_CONFIG["pattern"]["volume_mult"])
    df["pattern_hit"] = (shadow & surge).astype(float)

    bm = boards.drop_duplicates("code")[["code", "board"]]
    df = df.merge(bm, on="code", how="inner")
    df["ret_ok"] = df["ret"].notna().astype(float)
    df["above_ok"] = df["above60"].notna().astype(float)
    df["up"] = np.where(df["ret_ok"] > 0, (df["ret"] > 0).astype(float), np.nan)
    df["down"] = np.where(df["ret_ok"] > 0, (df["ret"] < 0).astype(float), np.nan)
    df["flat"] = np.where(df["ret_ok"] > 0, (df["ret"] == 0).astype(float), np.nan)
    df["ret_amt"] = np.where(df["ret_ok"] > 0, df["ret"] * df["amount"], np.nan)
    df["amount_ok"] = np.where(df["ret_ok"] > 0, df["amount"], np.nan)

    mkt_amount = df.groupby("date")["amount"].sum()
    agg = df.groupby(["board", "date"]).agg(
        eligible=("code", "size"),
        valid=("ret_ok", "sum"),
        up=("up", "sum"),
        down=("down", "sum"),
        flat=("flat", "sum"),
        above=("above60", "sum"),
        above_n=("above_ok", "sum"),
        amount=("amount", "sum"),
        ret_amt=("ret_amt", "sum"),
        amount_ok=("amount_ok", "sum"),
        eq_ret=("ret", "mean"),
        pattern_hits=("pattern_hit", "sum"),
    ).reset_index()
    agg["up_ratio"] = agg["up"] / agg["valid"].replace(0, np.nan)
    agg["above60_ratio"] = agg["above"] / agg["above_n"].replace(0, np.nan)
    # agg 是 (date, board) 行: 要按行里的 date 映射全市场成交额, 不能按 amount 数值映射
    # (旧写法 .map(mkt_amount) 会把数值当索引, 结果全是 NaN, 轮动的占比/拥挤度整列失效)
    agg["amount_share"] = agg["amount"] / agg["date"].map(mkt_amount).replace(0, np.nan)
    agg["aw_ret"] = agg["ret_amt"] / agg["amount_ok"].replace(0, np.nan)
    agg["pattern_share"] = agg["pattern_hits"] / agg["valid"].replace(0, np.nan)
    return {"agg": agg, "boards": boards}


def _wide(agg: pd.DataFrame, col: str) -> pd.DataFrame:
    return agg.pivot(index="date", columns="board", values=col).sort_index()


def _components(agg: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """七个打分指标的原始值宽表(方向统一为 high=过热)。"""
    amount = _wide(agg, "amount")
    vol_ma20 = amount.shift(1).rolling(20, min_periods=10).mean()
    eq_ret = _wide(agg, "eq_ret").fillna(0.0)
    eq_index = (1.0 + eq_ret).cumprod()
    roll_max = eq_index.rolling(250, min_periods=120).max()
    return {
        "up_ratio": _wide(agg, "up_ratio"),
        "above60_ratio": _wide(agg, "above60_ratio"),
        "amount_share": _wide(agg, "amount_share"),
        "vol_mult": amount / vol_ma20.replace(0, np.nan),
        "position": eq_index / roll_max.replace(0, np.nan),
        "ma60_dev": eq_index / eq_index.rolling(60, min_periods=40).mean() - 1.0,
        "pattern_share": _wide(agg, "pattern_share").fillna(0.0),
        "_eq_index": eq_index,
        "_aw_index": (1.0 + _wide(agg, "aw_ret").fillna(0.0)).cumprod(),
        "_eq_ret": eq_ret,
        "_drawdown": eq_index / roll_max.replace(0, np.nan) - 1.0,
    }


def _scores(comp: dict[str, pd.DataFrame], scfg: dict) -> pd.DataFrame:
    """七项分位等权平均 -> 自身历史分位映射 0-100。"""
    pcfg = scfg["percentile"]
    weights = scfg["weights"]
    pcts = []
    names = []
    for key, meta in weights.items():
        frame = comp[key].apply(lambda s: _expand_pct(s, pcfg["min_days"]))
        pcts.append(frame if meta["direction"] == "hot_high" else 100.0 - frame)
        names.append(key)
    stack = np.stack([p.to_numpy(dtype=float) for p in pcts])
    w = np.array([float(m["weight"]) for m in weights.values()])[:, None, None]
    avail = np.isfinite(stack).sum(axis=0)
    raw = np.nansum(stack * w, axis=0) / np.where(avail > 0, np.nansum(
        np.where(np.isfinite(stack), w, np.nan), axis=0), np.nan)
    raw = np.where(avail >= 4, raw, np.nan)
    raw_df = pd.DataFrame(raw, index=comp[names[0]].index, columns=comp[names[0]].columns)
    score = raw_df.apply(lambda s: _expand_pct(s.dropna(), pcfg["min_days"]).reindex(s.index))
    return score


def _signals(score: pd.Series, scfg: dict) -> list[dict]:
    top = float(scfg["top_threshold"])
    bottom = float(scfg["bottom_threshold"])
    cool = int(scfg["cooldown_days"])
    s = score.astype(float)
    out, last = [], {"top": -10**9, "bottom": -10**9}
    prev = np.nan
    for i, (dt, v) in enumerate(s.items()):
        if not np.isnan(prev) and not np.isnan(v):
            if prev < top <= v and i - last["top"] > cool:
                out.append({"date": str(dt)[:10], "type": "top", "score": round(float(v), 1)})
                last["top"] = i
            elif prev > bottom >= v and i - last["bottom"] > cool:
                out.append({"date": str(dt)[:10], "type": "bottom", "score": round(float(v), 1)})
                last["bottom"] = i
        prev = v
    return out


def _fwd(r: np.ndarray, i: int, h: int) -> tuple[float, float] | None:
    seg = r[i + 1: i + 1 + h]
    seg = seg[np.isfinite(seg)]
    if len(seg) < max(2, int(h * 0.6)):
        return None
    nav = np.cumprod(1.0 + seg)
    return float(nav[-1] - 1.0), float((nav / np.maximum.accumulate(nav) - 1.0).min())


def _two_prop_p(p1: float, p2: float, n1: int, n2: int) -> float:
    if n1 <= 0 or n2 <= 0:
        return 1.0
    p = (p1 * n1 + p2 * n2) / (n1 + n2)
    se = math.sqrt(max(p * (1 - p) * (1 / n1 + 1 / n2), 0.0))
    if se == 0:
        return 1.0
    z = abs(p1 - p2) / se
    return 2.0 * (1.0 - 0.5 * (1.0 + math.erf(z / math.sqrt(2.0))))


def _mean_p(m1: float, m2: float, s1: float, n1: int, s2: float, n2: int) -> float:
    se = math.sqrt(max(s1 * s1 / n1 + s2 * s2 / n2, 0.0))
    if se == 0 or not np.isfinite(se):
        return 1.0
    z = abs(m1 - m2) / se
    return 2.0 * (1.0 - 0.5 * (1.0 + math.erf(z / math.sqrt(2.0))))


def _bh(pvals: list[float]) -> list[float]:
    """Benjamini-Hochberg q 值。"""
    m = len(pvals)
    order = sorted(range(m), key=lambda i: pvals[i])
    q = [1.0] * m
    running = 1.0
    for rank, idx in enumerate(reversed(order), start=1):
        j = m - rank + 1
        running = min(running, pvals[idx] * m / j)
        q[idx] = min(1.0, running)
    return q


def _validate(score: pd.DataFrame, eq_ret: pd.DataFrame, scfg: dict) -> dict:
    horizons = [int(h) for h in scfg["validation"]["forward_days"]]
    alpha = float(scfg["validation"]["alpha"])
    dates = list(eq_ret.index)
    tests: list[dict] = []
    per_board: dict[str, dict] = {}
    for board in score.columns:
        events = _signals(score[board], scfg)
        r = eq_ret[board].to_numpy(dtype=float)
        rec: dict[str, dict] = {"board": board,
                                "n_events": {"top": sum(e["type"] == "top" for e in events),
                                             "bottom": sum(e["type"] == "bottom" for e in events)}}
        for typ in ("top", "bottom"):
            ev = [e for e in events if e["type"] == typ]
            hrec: dict[str, dict] = {}
            for h in horizons:
                sig_rets = []
                sig_mdd = []
                for e in ev:
                    i = dates.index(pd.Timestamp(e["date"]))
                    f = _fwd(r, i, h)
                    if f is not None:
                        sig_rets.append(f[0])
                        sig_mdd.append(f[1])
                base = []
                for i in range(len(r) - 1):
                    f = _fwd(r, i, h)
                    if f is not None:
                        base.append(f[0])
                if not sig_rets or not base:
                    continue
                sr = np.array(sig_rets)
                br = np.array(base)
                hit = float((sr < 0).mean()) if typ == "top" else float((sr > 0).mean())
                bhit = float((br < 0).mean()) if typ == "top" else float((br > 0).mean())
                p_win = _two_prop_p(hit, bhit, len(sr), len(br))
                p_mean = _mean_p(float(sr.mean()), float(br.mean()),
                                 float(sr.std(ddof=1)) if len(sr) > 1 else 0.0, len(sr),
                                 float(br.std(ddof=1)), len(br))
                # 方向必须一致才算"有效": 顶部信号命中=下跌, 底部信号命中=上涨
                t = {"board": board, "type": typ, "horizon": h, "n": len(sr),
                     "p": p_win, "p_mean": p_mean, "directional": hit > bhit}
                tests.append(t)
                hrec[f"{h}d"] = {
                    "n": len(sr),
                    "mean": round(float(sr.mean()) * 100, 2),
                    "win": round(hit * 100, 1),
                    "mdd": round(float(np.mean(sig_mdd)) * 100, 2) if sig_mdd else None,
                    "base_mean": round(float(br.mean()) * 100, 2),
                    "base_win": round(bhit * 100, 1),
                    "p_raw": round(p_win, 4),
                    "p_mean": round(p_mean, 4),
                }
            rec[typ] = hrec
        per_board[board] = rec

    # Bonferroni / BH-FDR
    m = len(tests)
    q = _bh([t["p"] for t in tests])
    for t, qv in zip(tests, q, strict=False):
        t["p_bonf"] = min(1.0, t["p"] * m)
        t["q_fdr"] = qv
    for t in tests:
        rec = per_board[t["board"]][t["type"]][f"{t['horizon']}d"]
        rec["p_bonf"] = round(t["p_bonf"], 4)
        rec["q_fdr"] = round(t["q_fdr"], 4)
        rec["significant"] = bool(t["p_bonf"] < alpha and t["directional"])
        rec["significant_fdr"] = bool(t["q_fdr"] < alpha and t["directional"])

    sig = [t for t in tests if t["p_bonf"] < alpha and t["directional"]]
    fdr = [t for t in tests if t["q_fdr"] < alpha and t["directional"]]
    looks = [t for t in tests if t["p"] < alpha <= t["p_bonf"] and t["directional"]]
    wrong = [t for t in tests if t["p"] < alpha and not t["directional"]]
    insufficient = [t for t in tests if t["n"] < 3]
    return {
        "horizons": horizons,
        "n_tests": m,
        "alpha": alpha,
        "per_board": per_board,
        "significant_bonferroni": [_fmt_test(t) for t in sig],
        "significant_fdr": [_fmt_test(t) for t in fdr],
        "looks_good_only": [_fmt_test(t) for t in looks],
        "wrong_direction": [_fmt_test(t) for t in wrong],
        "insufficient": [_fmt_test(t) for t in insufficient],
    }


def _fmt_test(t: dict) -> dict:
    return {"board": t["board"], "type": t["type"], "horizon": t["horizon"], "n": t["n"],
            "p_raw": round(t["p"], 4), "p_bonf": round(t["p_bonf"], 4),
            "q_fdr": round(t["q_fdr"], 4)}


def _cross_section(score: pd.DataFrame, eq_ret: pd.DataFrame, dates: list, scfg: dict) -> dict:
    """同日横截面: 触发顶信号的板块, 之后是否系统性跑输未触发板块。"""
    horizons = [int(h) for h in scfg["validation"]["forward_days"]]
    out: dict[str, dict] = {}
    for h in horizons:
        spreads = []
        for i, dt in enumerate(dates):
            if i + h >= len(dates):
                break
            row = score.loc[dt]
            triggered = [b for b in score.columns
                         if not pd.isna(row.get(b))
                         and float(row.get(b)) >= float(scfg["top_threshold"])]
            if len(triggered) < 1:
                continue
            others = [b for b in score.columns if b not in triggered]
            if len(others) < 2:
                continue
            def fwd(board: str, _i: int = i, _h: int = h) -> float:
                seg = eq_ret[board].iloc[_i + 1: _i + 1 + _h].dropna()
                return float(np.prod(1.0 + seg.to_numpy()) - 1.0) if len(seg) else np.nan
            tri = np.nanmean([fwd(b) for b in triggered])
            oth = np.nanmean([fwd(b) for b in others])
            if np.isfinite(tri) and np.isfinite(oth):
                spreads.append(float(oth) - float(tri))
        if not spreads:
            continue
        a = np.array(spreads)
        p = _mean_p(float(a.mean()), 0.0, float(a.std(ddof=1)) if len(a) > 1 else 0.0,
                    len(a), 1e-9, len(a))
        out[f"{h}d"] = {"n_dates": len(a),
                        "mean_spread": round(float(a.mean()) * 100, 2),
                        "p": round(p, 4),
                        "share_triggered_underperform": round(float((a > 0).mean()) * 100, 1),
                        "note": "spread=未触发板块等权收益-触发板块等权收益, 正值=触发板块跑输"}
    return out


def _pad_pct(s: pd.Series, window: int = 252, min_days: int = 60) -> pd.Series:
    """成交额占比的滚动分位(0-100), 用于拥挤度。"""
    return s.rolling(window, min_periods=min_days).apply(
        lambda a: float((a <= a[-1]).mean()) * 100.0, raw=True)


def _rotation(comp: dict, score: pd.DataFrame, dates: list[str]) -> dict:
    """板块相对强弱/轮动(不是顶底)。

    RS = 板块等权指数 / 沪深300(同起点归一); 相对动量 = RS 的 20/60 日变化;
    成交额占比变化与拥挤度(占比滚动分位)来自板块成交额。
    """
    idx = store.read_frame("index_daily.parquet")
    eq = comp["_eq_index"].copy()
    eq.index = pd.to_datetime([str(d)[:10] for d in eq.index])
    if idx.empty or "000300" not in set(idx["code"]):
        return {"available": False, "reason": "缺少沪深300指数日线"}
    hs = idx[idx["code"] == "000300"].copy()
    hs.index = pd.to_datetime(hs["date"])
    hs = pd.to_numeric(hs["close"], errors="coerce").dropna().sort_index()
    hs = hs[~hs.index.duplicated(keep="last")].reindex(eq.index).ffill()

    base = eq.apply(lambda s: s.dropna().iloc[0] if s.notna().any() else np.nan)
    eqn = eq.div(base, axis=1)
    hsn = hs / hs.dropna().iloc[0]
    rs = eqn.div(hsn, axis=0)
    mom20 = (rs / rs.shift(20) - 1.0) * 100
    mom60 = (rs / rs.shift(60) - 1.0) * 100

    share = comp["amount_share"].copy()
    share.index = eq.index
    share_chg20 = (share - share.shift(20)) * 100  # 百分点
    crowd = share.apply(_pad_pct)

    last = -1
    rows = []
    for b in score.columns:
        m20 = mom20[b].iloc[last]
        m60 = mom60[b].iloc[last]
        rows.append({
            "board": b,
            "rs": None if pd.isna(rs[b].iloc[last]) else round(float(rs[b].iloc[last]), 3),
            "mom20": None if pd.isna(m20) else round(float(m20), 2),
            "mom60": None if pd.isna(m60) else round(float(m60), 2),
            "share": None if pd.isna(share[b].iloc[last]) else round(float(share[b].iloc[last]) * 100, 2),
            "share_chg20": None if pd.isna(share_chg20[b].iloc[last]) else round(
                float(share_chg20[b].iloc[last]), 2),
            "crowding": None if pd.isna(crowd[b].iloc[last]) else round(float(crowd[b].iloc[last]), 1),
            "quadrant": ("强强" if (m20 or 0) > 0 and (m60 or 0) > 0 else
                         "强弱" if (m20 or 0) > 0 else
                         "弱强" if (m60 or 0) > 0 else "弱弱")
            if pd.notna(m20) and pd.notna(m60) else None,
        })
    # 相对强弱排名: 20/60 日动量的横截面秩平均
    valid = [r for r in rows if r["mom20"] is not None and r["mom60"] is not None]
    r20 = pd.Series({r["board"]: r["mom20"] for r in valid}).rank(pct=True)
    r60 = pd.Series({r["board"]: r["mom60"] for r in valid}).rank(pct=True)
    strength = (r20 + r60) / 2
    for r in rows:
        r["strength"] = None if r["board"] not in strength else round(
            float(strength[r["board"]]), 3)
    rows.sort(key=lambda r: (r["strength"] is None, -(r["strength"] or 0)))

    validation = _rotation_validation(eq, mom20)
    return {
        "available": True,
        "asof": dates[-1],
        "rows": rows,
        "strong": [r["board"] for r in rows[:3]],
        "weak": [r["board"] for r in rows[-3:]],
        "validation": validation,
        "note": ("相对强弱/轮动(RS=板块等权指数/沪深300), 不是顶底; "
                 "拥挤度=板块成交额占比的252日分位"),
    }


def _rotation_validation(eq: pd.DataFrame, mom20: pd.DataFrame) -> dict:
    """横截面多前3空后3: 20/60 日前瞻超额、胜率、t 值; 同时检验反转效应。"""
    out = {}
    for h in (20, 60):
        fwd = (eq.shift(-h) / eq - 1.0)
        excess = fwd.sub(fwd.mean(axis=1), axis=0)
        spreads, ics = [], []
        for i in range(len(eq) - h):
            m = mom20.iloc[i].dropna()
            e = excess.iloc[i].dropna()
            common = m.index.intersection(e.index)
            if len(common) < 6:
                continue
            mm = m[common].sort_values()
            top, bottom = mm.index[-3:], mm.index[:3]
            spreads.append(float(e[top].mean() - e[bottom].mean()))
            ics.append(float(m[common].rank().corr(e[common].rank())))
        a = np.array(spreads)
        if len(a) < 20:
            out[f"{h}d"] = {"n": len(a), "available": False}
            continue
        n_eff = max(1, len(a) / h)
        t = float(a.mean() / a.std(ddof=1) * np.sqrt(n_eff)) if a.std(ddof=1) > 0 else None
        ic = np.array(ics)
        out[f"{h}d"] = {
            "n": len(a),
            "mean_spread_pct": round(float(a.mean()) * 100, 2),
            "win_rate": round(float((a > 0).mean()) * 100, 1),
            "t": None if t is None else round(t, 2),
            "mean_ic": round(float(ic.mean()), 3),
            "ic_ir": round(float(ic.mean() / ic.std(ddof=1)), 2) if ic.std(ddof=1) > 0 else None,
        }
    v20 = out.get("20d", {})
    valid = bool(v20.get("available", True) and v20.get("t") and abs(v20["t"]) >= 2)
    spread = v20.get("mean_spread_pct", 0) or 0
    if valid and spread > 0:
        out["verdict"] = "横截面动量显著(强跑赢), 可参考"
    elif valid and spread < 0:
        out["verdict"] = "存在显著反转效应(强势跑输)"
    elif spread < 0:
        out["verdict"] = "动量不显著, 方向偏反转(强势略跑输, |t|<2), 仅展示"
    else:
        out["verdict"] = "动量不显著, 仅展示"
    out["display_only"] = not (valid and spread > 0)
    return out


def build_board_module(include_history: bool = True) -> dict:
    """构建快照 boards 字段: 指标、评分、信号、逐板块验证与横截面检验。"""
    scfg = RADAR_CONFIG["board"]
    stock = store.read_frame("tdx_stock.parquet")
    boards = store.read_frame("board_members.parquet")
    if stock.empty or boards.empty:
        logger.warning("缺少 tdx_stock / board_members, 跳过板块顶底模块")
        store.update_status("board", status="missing", reason="缺少 TDX 个股或板块成分数据")
        return {"available": False, "reason": "缺少 TDX 个股或板块成分数据"}

    pcfg = scfg["percentile"]
    built = _metrics(stock, boards, pcfg)
    agg = built["agg"]
    comp = _components(agg)
    score = _scores(comp, scfg)
    eq_ret = comp["_eq_ret"]

    latest = str(agg["date"].max())[:10]
    dates = [str(d)[:10] for d in score.index]
    last_i = len(score) - 1

    ranking = []
    for board in score.columns:
        s = score[board]
        val = s.iloc[last_i]
        row = agg[(agg["board"] == board) & (agg["date"] == score.index[last_i])]
        r = row.iloc[0] if len(row) else None
        recent = [e for e in _signals(s, scfg) if e["date"] >= dates[max(0, last_i - 60)]]
        ranking.append({
            "board": board,
            "score": None if pd.isna(val) else round(float(val), 1),
            "top": bool(None if pd.isna(val) else float(val) >= float(scfg["top_threshold"])),
            "bottom": bool(None if pd.isna(val) else float(val) <= float(scfg["bottom_threshold"])),
            "metrics": None if r is None else {
                "up": int(r["up"]) if pd.notna(r["up"]) else None,
                "down": int(r["down"]) if pd.notna(r["down"]) else None,
                "flat": int(r["flat"]) if pd.notna(r["flat"]) else None,
                "eligible": int(r["eligible"]),
                "up_ratio": None if pd.isna(r["up_ratio"]) else round(float(r["up_ratio"]), 3),
                "above60_ratio": None if pd.isna(r["above60_ratio"]) else round(
                    float(r["above60_ratio"]), 3),
                "amount_share": None if pd.isna(r["amount_share"]) else round(
                    float(r["amount_share"]), 4),
                "vol_mult": None if pd.isna(comp["vol_mult"][board].iloc[last_i]) else round(
                    float(comp["vol_mult"][board].iloc[last_i]), 2),
                "drawdown250": None if pd.isna(comp["_drawdown"][board].iloc[last_i]) else round(
                    float(comp["_drawdown"][board].iloc[last_i]) * 100, 2),
                "ma60_dev": None if pd.isna(comp["ma60_dev"][board].iloc[last_i]) else round(
                    float(comp["ma60_dev"][board].iloc[last_i]) * 100, 2),
                "position": None if pd.isna(comp["position"][board].iloc[last_i]) else round(
                    float(comp["position"][board].iloc[last_i]), 3),
                "pattern_hits": int(r["pattern_hits"]),
                "pattern_share": None if pd.isna(r["pattern_share"]) else round(
                    float(r["pattern_share"]) * 100, 1),
                "eq_index": None if pd.isna(comp["_eq_index"][board].iloc[last_i]) else round(
                    float(comp["_eq_index"][board].iloc[last_i]), 2),
                "aw_index": None if pd.isna(comp["_aw_index"][board].iloc[last_i]) else round(
                    float(comp["_aw_index"][board].iloc[last_i]), 2),
            },
            "recent_signals": recent,
        })
    ranking.sort(key=lambda x: (x["score"] is None, -(x["score"] or 0)))

    all_signals = []
    for board in score.columns:
        r = eq_ret[board].to_numpy(dtype=float)
        for e in _signals(score[board], scfg):
            i = dates.index(e["date"])
            rec = dict(e)
            rec["board"] = board
            for h in scfg["validation"]["forward_days"]:
                f = _fwd(r, i, int(h))
                rec[f"ret{h}"] = None if f is None else round(f[0] * 100, 2)
                rec[f"mdd{h}"] = None if f is None else round(f[1] * 100, 2)
                v = rec[f"ret{h}"]
                rec[f"hit{h}"] = None if v is None else bool(v < 0 if e["type"] == "top" else v > 0)
            all_signals.append(rec)
    all_signals.sort(key=lambda x: x["date"], reverse=True)

    validation = _validate(score, eq_ret, scfg)
    validation["cross_section"] = _cross_section(score, eq_ret, list(score.index), scfg)
    validation["verdict"] = _verdict(validation, scfg)
    try:
        rotation = _rotation(comp, score, dates)
    except Exception as exc:  # 轮动失败不应阻断顶底模块
        logger.warning("板块轮动计算失败: %s", exc)
        rotation = {"available": False, "reason": str(exc)[:160]}

    detail = {}
    detail_dates: list[str] = []
    if include_history:
        # 日期对所有板块一致, 只存一份, 避免快照膨胀
        detail_dates = dates
        for board in score.columns:
            detail[board] = {
                "score": [None if pd.isna(v) else round(float(v)) for v in score[board]],
                "index": [None if pd.isna(v) else round(float(v), 2) for v in comp["_eq_index"][board]],
                "events": [e for e in all_signals if e["board"] == board],
            }

    store.update_status("board", status="ok", latest=latest, boards=int(score.shape[1]),
                        trading_days=len(dates), signals=len(all_signals),
                        verdict=validation["verdict"])
    return {
        "available": True,
        "date": latest,
        "params": {**scfg, "missing": ["市值加权指数(缺个股流通市值, 以成交额加权替代)",
                                       "2015 年起板块历史(个股行情仅约 3 年)"]},
        "universe": {
            "boards": int(score.shape[1]), "mapped_codes": int(boards["code"].nunique()),
            "stock_codes": int(stock["code"].nunique()),
            "trading_days": len(dates), "range": f"{dates[0]} ~ {dates[-1]}",
            "source": str(boards["source"].iloc[0]) if "source" in boards.columns else "unknown",
            "note": "申万一级行业当前成分快照, 按当前成分回溯(成分漂移偏差); 仅行业板块",
        },
        "ranking": ranking,
        "signals": all_signals[:150],
        "detail_dates": detail_dates,
        "detail": detail,
        "rotation": rotation,
        "validation": validation,
    }


def _verdict(validation: dict, scfg: dict) -> str:
    cross = (validation.get("cross_section") or {}).get("20d")
    if validation["significant_bonferroni"]:
        return "部分板块经 Bonferroni 校正后仍显著, 可谨慎参考"
    if cross and cross["p"] < float(scfg["validation"]["alpha"]) and cross["mean_spread"] > 0:
        return "逐板块时序检验不显著, 但同日横截面显示顶信号板块跑输; 仅作参考展示"
    return "板块顶底不可靠, 仅作参考展示"


def main() -> None:
    """CLI 入口: 本地离线重算并打印结论。"""
    import argparse
    import json

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="板块顶底评分与验证")
    ap.add_argument("--out", help="输出 JSON 路径")
    args = ap.parse_args()
    payload = build_board_module()
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        print("saved", args.out)
    if payload.get("available"):
        v = payload["validation"]
        print("universe", payload["universe"])
        print("verdict", v["verdict"])
        print("n_tests", v["n_tests"], "bonf", len(v["significant_bonferroni"]),
              "fdr", len(v["significant_fdr"]), "looks", len(v["looks_good_only"]))
        print("cross", v.get("cross_section"))


if __name__ == "__main__":
    main()
