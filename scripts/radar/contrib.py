"""指标边际贡献量化(滚动/样本外口径)。

输入: 快照里的 indicator_history(周频原始值) + series(日频 HS300 与综合分)。
输出: 每个指标 IC / IC_IR / 正交化 IC / LOO 边际贡献 / 是否冗余 / 0-100 贡献度。

约束: 纯描述性统计, 不据此重算 composite.weights。
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

ROLL_WEEKS = 104          # 滚动窗口 2 年
STEP_WEEKS = 13           # 每季度重估一次
MIN_OBS = 60              # 窗口内最少观测
FWD_DAYS = (20, 60, 120)
PRIMARY_FWD = 60
PCT_WINDOW = 504          # 约 10 年(周频)
PCT_MIN = 250


def _oriented_history(hist: dict, weights: dict) -> pd.DataFrame:
    rows = {}
    for key, items in hist.items():
        if not items:
            continue
        s = pd.Series({pd.Timestamp(d): float(v) for d, v in items if v is not None})
        s = s[~s.index.duplicated(keep="last")].sort_index()
        rows[key] = s
    df = pd.DataFrame(rows).sort_index()
    return df


def _rolling_pct(s: pd.Series, window: int = PCT_WINDOW, min_obs: int = PCT_MIN) -> pd.Series:
    def _last_pct(a: np.ndarray) -> float:
        cur = a[-1]
        valid = a[~np.isnan(a)]
        if len(valid) < min_obs:
            return np.nan
        return float((valid <= cur).mean() * 100.0)
    return s.rolling(window, min_periods=min_obs).apply(_last_pct, raw=True)


def _forward_return(dates: pd.DatetimeIndex, px: np.ndarray, i: int, days: int) -> float:
    j = i + days
    if j >= len(px) or not np.isfinite(px[i]) or not np.isfinite(px[j]):
        return np.nan
    return float(px[j] / px[i] - 1.0)


def _max_dd(dates: pd.DatetimeIndex, px: np.ndarray, i: int, days: int) -> float:
    j = min(i + days, len(px) - 1)
    if j <= i:
        return np.nan
    seg = px[i:j + 1]
    base = seg[0]
    return float((seg / base - 1.0).min()) if np.isfinite(base) and base else np.nan


def _spearman(x: np.ndarray, y: np.ndarray) -> float:
    m = np.isfinite(x) & np.isfinite(y)
    if m.sum() < MIN_OBS:
        return np.nan
    return float(pd.Series(x[m]).rank().corr(pd.Series(y[m]).rank()))


def _ols_resid(y: np.ndarray, xmat: np.ndarray) -> np.ndarray:
    """滚动拟合 + 样本外预测: 用过去 104 周拟合, 残差只保留接下来 13 周的样本。"""
    out = np.full(len(y), np.nan)
    n = len(y)
    start = ROLL_WEEKS
    while start < n:
        end = min(start + STEP_WEEKS, n)
        tr = slice(max(0, start - ROLL_WEEKS), start)
        trx = xmat[tr]
        keep = np.isfinite(trx).mean(axis=0) >= 0.8   # 只用该窗口里覆盖率 >=80% 的对照指标
        trx = trx[:, keep]
        m = np.isfinite(y[tr]) & np.all(np.isfinite(trx), axis=1)
        if m.sum() >= MIN_OBS and trx.shape[1] > 0:
            amat = np.column_stack([np.ones(int(m.sum())), trx[m]])
            beta, *_ = np.linalg.lstsq(amat, y[tr][m], rcond=None)
            tex = xmat[start:end][:, keep]
            te = np.isfinite(y[start:end]) & np.all(np.isfinite(tex), axis=1)
            if te.any():
                xte = np.column_stack([np.ones(int(te.sum())), tex[te]])
                idx = np.arange(start, end)[te]
                out[idx] = y[idx] - xte @ beta
        start = end
    return out


def _ic_stats(x: np.ndarray, y: np.ndarray, dates: pd.DatetimeIndex) -> dict:
    ics = []
    n = len(x)
    start = ROLL_WEEKS
    while start <= n:
        lo = max(0, start - ROLL_WEEKS)
        ic = _spearman(x[lo:start], y[lo:start])
        if np.isfinite(ic):
            ics.append(ic)
        start += STEP_WEEKS
    if not ics:
        return {"ic": None, "ir": None, "pos": None, "t": None, "n": 0}
    arr = np.array(ics)
    mean = float(arr.mean())
    std = float(arr.std(ddof=1)) if len(arr) > 1 else float("nan")
    ir = mean / std if std and np.isfinite(std) and std > 0 else None
    t = mean / std * math.sqrt(len(arr)) if std and std > 0 else None
    return {"ic": round(mean, 4), "ir": round(ir, 3) if ir is not None else None,
            "pos": round(float((arr > 0).mean()), 3),
            "t": round(t, 2) if t is not None else None, "n": len(arr)}


def _composite_score(pct: pd.DataFrame, weights: dict) -> pd.Series:
    cols = [c for c in pct.columns if c in weights]
    if not cols:
        return pd.Series(dtype=float)
    w = pd.Series({c: float(weights[c]) for c in cols})
    w = w / w.sum()
    raw = (pct[cols] * w).sum(axis=1) / pct[cols].notna().mul(w).sum(axis=1).replace(0, np.nan)
    return _rolling_pct(raw, min_obs=60)


def _signal_eval(score: pd.Series, px: np.ndarray, sdates: pd.DatetimeIndex,
                 top: float, bottom: float, fwd: int) -> dict:
    hit, dd, n = [], [], 0
    last = -10 ** 9
    for i, d in enumerate(score.index):
        if not np.isfinite(score.iloc[i]):
            continue
        si = int(sdates.searchsorted(d))
        if si >= len(sdates) - 1:
            continue
        s = float(score.iloc[i])
        if s < top and s > bottom:
            continue
        if i - last < 12:      # 约 60 交易日冷却
            continue
        last = i
        r = _forward_return(sdates, px, si, fwd)
        if not np.isfinite(r):
            continue
        n += 1
        hit.append(r < 0 if s >= top else r > 0)
        if s >= top:
            dd.append(_max_dd(sdates, px, si, fwd))
    return {"n": n, "hit": float(np.mean(hit)) if hit else None,
            "maxdd": float(np.nanmean(dd)) if dd else None}


def compute_contributions(snapshot: dict, weights: dict, directions: dict,
                          top_threshold: float = 85, bottom_threshold: float = 15) -> dict:
    hist = snapshot.get("indicator_history") or {}
    series = snapshot.get("series") or []
    sdates = pd.DatetimeIndex([pd.Timestamp(r["date"]) for r in series])
    px = np.array([r.get("hs300") or np.nan for r in series], dtype=float)
    hist_df = _oriented_history(hist, weights)
    if hist_df.empty:
        return {"indicators": [], "method": "no history"}

    pct = pd.DataFrame({c: _rolling_pct(hist_df[c]) for c in hist_df.columns})
    oriented = pct.copy()
    for c in oriented.columns:
        if directions.get(c) == "hot_low":
            oriented[c] = 100.0 - oriented[c]
    keys = list(oriented.columns)

    # 未来收益(周频点 -> 日频序列)
    fwd = {}
    for days in FWD_DAYS:
        arr = []
        for d in oriented.index:
            si = int(sdates.searchsorted(d))
            arr.append(_forward_return(sdates, px, si, days) if si < len(sdates) else np.nan)
        fwd[days] = np.array(arr, dtype=float)

    end_idx = sdates.searchsorted(oriented.index, side="right") - 1
    end_idx = np.clip(end_idx, 0, len(sdates) - 1)

    full_score = _composite_score(oriented, weights)
    base = {days: _signal_eval(full_score, px, sdates, top_threshold, bottom_threshold, days)
            for days in FWD_DAYS}
    base_ic = {days: _ic_stats(full_score.to_numpy(dtype=float), fwd[days], oriented.index)
               for days in FWD_DAYS}

    out = []
    for key in keys:
        x = oriented[key].to_numpy(dtype=float)
        ic = {days: _ic_stats(x, fwd[days], oriented.index) for days in FWD_DAYS}

        others = [k for k in keys if k != key]
        orth = oriented[key].to_numpy(dtype=float)
        resid = _ols_resid(orth, oriented[others].to_numpy(dtype=float))
        orth_ic = {days: _ic_stats(resid, fwd[days], oriented.index) for days in FWD_DAYS}

        loo_w = {k: v for k, v in weights.items() if k != key}
        if key in weights and loo_w:
            loo_score = _composite_score(oriented, loo_w)
            loo = {days: _signal_eval(loo_score, px, sdates, top_threshold, bottom_threshold, days)
                   for days in FWD_DAYS}
            loo_ic = {days: _ic_stats(loo_score.to_numpy(dtype=float), fwd[days], oriented.index)
                      for days in FWD_DAYS}
        else:
            loo = {days: {"n": 0, "hit": None, "maxdd": None} for days in FWD_DAYS}
            loo_ic = {days: {"ic": None} for days in FWD_DAYS}

        deltas, score_deltas, dd_deltas = {}, {}, {}
        for days in FWD_DAYS:
            b, lv = base[days], loo[days]
            deltas[days] = (round(lv["hit"] - b["hit"], 4)
                            if b["hit"] is not None and lv["hit"] is not None else None)
            bi, li = base_ic[days]["ic"], loo_ic[days]["ic"]
            score_deltas[days] = (round(li - bi, 4) if bi is not None and li is not None else None)
            dd_deltas[days] = (round(lv["maxdd"] - b["maxdd"], 4)
                               if b["maxdd"] is not None and lv["maxdd"] is not None else None)

        primary = ic[PRIMARY_FWD]
        opp = orth_ic[PRIMARY_FWD]
        raw_sig = primary["t"] is not None and abs(primary["t"]) >= 2.0
        redundant = None if opp["t"] is None else bool(raw_sig and abs(opp["t"]) < 1.5)

        valid = hist_df[key].dropna()
        out.append({
            "key": key,
            "ic": {str(d): ic[d]["ic"] for d in FWD_DAYS},
            "ic_ir": {str(d): ic[d]["ir"] for d in FWD_DAYS},
            "ic_pos": {str(d): ic[d]["pos"] for d in FWD_DAYS},
            "ic_t": {str(d): ic[d]["t"] for d in FWD_DAYS},
            "orth_ic": {str(d): orth_ic[d]["ic"] for d in FWD_DAYS},
            "orth_t": {str(d): orth_ic[d]["t"] for d in FWD_DAYS},
            "loo_delta_hit": {str(d): deltas[d] for d in FWD_DAYS},
            "loo_delta_ic": {str(d): score_deltas[d] for d in FWD_DAYS},
            "loo_delta_maxdd": {str(d): dd_deltas[d] for d in FWD_DAYS},
            "loo": {str(d): loo[d] for d in FWD_DAYS},
            "n_windows": primary["n"], "n_obs": int(np.isfinite(x).sum()),
            "sample_start": str(valid.index.min().date()) if len(valid) else None,
            "sample_end": str(valid.index.max().date()) if len(valid) else None,
            "redundant": redundant,
            "in_weights": key in weights,
            "display_only": key not in weights,
        })

    # 0-100 贡献度: 45% |IC60| + 30% IC_IR + 25% LOO 分数 IC 增量(各成分按指标间排名归一)
    def _rank01(vals):
        s = pd.Series([v if v is not None else np.nan for v in vals], dtype=float)
        r = s.rank(pct=True)
        return [0.0 if pd.isna(x) else float(x) for x in r]
    vec_ic = _rank01([abs(r["ic"]["60"]) if r["ic"]["60"] is not None else None for r in out])
    vec_ir = _rank01([abs(r["ic_ir"]["60"]) if r["ic_ir"]["60"] is not None else None for r in out])
    vec_mg = _rank01([r["loo_delta_ic"].get("60") for r in out])
    for r, a, b, c in zip(out, vec_ic, vec_ir, vec_mg, strict=False):
        r["contribution_score"] = round(100 * (0.45 * a + 0.30 * b + 0.25 * c), 1)

    out.sort(key=lambda r: r["contribution_score"], reverse=True)
    return {
        "method": "滚动样本外: 104周窗口/13周步长, 前瞻20/60/120交易日; 正交化=对其余指标OLS取残差; "
                  "LOO=去掉该指标后重算加权分, 比较信号命中率/最大回撤与分数IC",
        "weighting": "贡献度 = 45%|IC60| + 30%IC_IR60 + 25%LOO 分数IC增量, 各成分按指标间排名归一(0-1)",
        "primary_horizon": PRIMARY_FWD,
        "baseline": {str(d): base[d] for d in FWD_DAYS},
        "baseline_score_ic": {str(d): base_ic[d] for d in FWD_DAYS},
        "indicators": out,
    }
