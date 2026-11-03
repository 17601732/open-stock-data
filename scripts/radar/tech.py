"""技术分析图层: 上证指数/沪深300 的斐波那契回撤、量能、MACD、背离与独立技术分。

全部为展示/证据层, 不参与综合分; 技术分仅用于 A/B/C 对照实验(见 ab_test)。

规则固定且可复现:
- 斐波那契: 对 2014 起日收盘价做 zigzag(20% 阈值)识别波段, 取最近一组高/低点(未走完的
  当前波段取到最新极值)作为锚点; 低点=0.000、高点=1.000, 输出 0.236/0.382/0.5/0.618/0.809。
- 量能: 成交额/20日均额=量比; >=2.0 记放量, <=0.5 记缩量; 同时给 MA5/MA10 均额。
- MACD: DIF=EMA12-EMA26, DEA=EMA9(DIF), 柱=2*(DIF-DEA); 超买/超卖线=柱的 ±2 倍标准差(252日)。
- 背离: 价格创 60 日新高而 DIF 低于上一次 60 日新高时的 DIF -> 顶背离; 新低反之 -> 底背离。
- 技术分: MACD 热度、量能热度、250日区间位置三项滚动十年分位等权平均, 再做自身滚动十年
  分位映射到 0-100; 高=过热(顶部风险), 低=过冷(底部机会)。量能热度在年线上方取正、下方取反。
"""

from __future__ import annotations

import logging
import math

import numpy as np
import pandas as pd

from scripts.radar.config import RADAR_CONFIG
from scripts.radar.indicators import rolling_percentile

logger = logging.getLogger(__name__)

BENCHMARKS = [("000001", "上证指数(999999)"), ("000300", "沪深300")]
START = "2014-01-01"


def _ema(s: pd.Series, span: int) -> pd.Series:
    return s.ewm(span=span, adjust=False).mean()


def zigzag(close: pd.Series, threshold: float) -> list[dict]:
    """收盘价 zigzag: 反转幅度达到 threshold 才确认一个高低点(最后一个为进行中的极值)。"""
    idx = close.index
    vals = close.to_numpy(dtype=float)
    if len(vals) < 2:
        return []
    pivots: list[dict] = []
    trend = 1  # 1=自低点向上, -1=自高点向下
    ext_i, ext_v = 0, vals[0]
    for i in range(1, len(vals)):
        v = vals[i]
        if trend == 1:
            if v > ext_v:
                ext_i, ext_v = i, v
            elif v <= ext_v * (1.0 - threshold):
                pivots.append({"date": idx[ext_i], "price": ext_v, "kind": "high"})
                trend, ext_i, ext_v = -1, i, v
        else:
            if v < ext_v:
                ext_i, ext_v = i, v
            elif v >= ext_v * (1.0 + threshold):
                pivots.append({"date": idx[ext_i], "price": ext_v, "kind": "low"})
                trend, ext_i, ext_v = 1, i, v
    pivots.append({
        "date": idx[ext_i], "price": ext_v,
        "kind": "high" if trend == 1 else "low", "provisional": True,
    })
    return pivots


def fib_anchor(close: pd.Series, threshold: float) -> dict:
    """最近一组 >threshold 波段的起止点; 当前未走完的波段取到最新极值。"""
    piv = zigzag(close, threshold)
    if not piv:
        lo, hi = float(close.min()), float(close.max())
        return {"low": {"date": str(close.idxmin())[:10], "price": lo},
                "high": {"date": str(close.idxmax())[:10], "price": hi},
                "direction": "up", "provisional": True}
    conf = [p for p in piv if not p.get("provisional")]
    last = piv[-1]
    if last["kind"] == "high":  # 当前为上行波段: 高=最新极值, 低=上一个确认低点
        lows = [p for p in conf if p["kind"] == "low"]
        low = lows[-1] if lows else {"date": str(close.idxmin())[:10], "price": float(close.min())}
        high, direction = last, "up"
    else:  # 当前为下行波段: 低=最新极值, 高=上一个确认高点
        highs = [p for p in conf if p["kind"] == "high"]
        high = highs[-1] if highs else {"date": str(close.idxmax())[:10], "price": float(close.max())}
        low, direction = last, "down"
    return {
        "low": {"date": str(low["date"])[:10], "price": round(float(low["price"]), 2)},
        "high": {"date": str(high["date"])[:10], "price": round(float(high["price"]), 2)},
        "direction": direction, "provisional": bool(last.get("provisional", False)),
    }


def fib_levels(low: float, high: float, ratios: list[float]) -> list[dict]:
    return [{"ratio": r, "price": round(low + r * (high - low), 2)} for r in ratios]


def macd(close: pd.Series) -> pd.DataFrame:
    cfg = RADAR_CONFIG["tech"]["macd"]
    dif = _ema(close, cfg["fast"]) - _ema(close, cfg["slow"])
    dea = _ema(dif, cfg["signal"])
    hist = (dif - dea) * 2.0
    return pd.DataFrame({"dif": dif, "dea": dea, "hist": hist})


def divergences(close: pd.Series, dif: pd.Series, lookback: int, gap: int) -> list[dict]:
    """价格新高/新低而 DIF 未同步创新高/新低 -> 背离(同一方向两次极值间隔 >= gap 日)。"""
    hi = close.rolling(lookback, min_periods=lookback).max()
    lo = close.rolling(lookback, min_periods=lookback).min()
    new_hi = (close >= hi).fillna(False)
    new_lo = (close <= lo).fillna(False)
    out: list[dict] = []
    last_hi_i, last_lo_i = -10**9, -10**9
    last_hi_dif, last_lo_dif = np.nan, np.nan
    for i in range(len(close)):
        if new_hi.iloc[i]:
            if i - last_hi_i >= gap and not np.isnan(last_hi_dif) and dif.iloc[i] < last_hi_dif:
                out.append({"date": str(close.index[i])[:10], "type": "bearish",
                            "note": f"价格创{lookback}日新高但 DIF 低于前高"
                                    f"({dif.iloc[i]:.2f}<{last_hi_dif:.2f})"})
            last_hi_i, last_hi_dif = i, dif.iloc[i]
        if new_lo.iloc[i]:
            if i - last_lo_i >= gap and not np.isnan(last_lo_dif) and dif.iloc[i] > last_lo_dif:
                out.append({"date": str(close.index[i])[:10], "type": "bullish",
                            "note": f"价格创{lookback}日新低但 DIF 高于前低"
                                    f"({dif.iloc[i]:.2f}>{last_lo_dif:.2f})"})
            last_lo_i, last_lo_dif = i, dif.iloc[i]
    return out[-12:]


def tech_score_series(px: pd.Series, amount: pd.Series) -> pd.Series:
    """独立技术分(0-100, 高=过热): 三项分位等权后做自身滚动十年分位映射。"""
    cfg = RADAR_CONFIG["tech"]["score"]
    pcfg = RADAR_CONFIG["percentile"]
    ma20 = amount.rolling(20, min_periods=10).mean()
    vol_raw = amount / ma20.replace(0, np.nan)
    m = macd(px)
    macd_raw = (m["dif"] / px.replace(0, np.nan)) * 100.0
    min250 = px.rolling(250, min_periods=120).min()
    max250 = px.rolling(250, min_periods=120).max()
    pos_raw = (px - min250) / (max250 - min250).replace(0, np.nan)
    trend_up = px >= px.rolling(120, min_periods=60).mean()

    macd_pct = rolling_percentile(macd_raw, pcfg["window_days"], pcfg["min_days"])
    vol_pct = rolling_percentile(vol_raw, pcfg["window_days"], pcfg["min_days"])
    vol_pct = vol_pct.where(trend_up, 100.0 - vol_pct)
    pos_pct = rolling_percentile(pos_raw, pcfg["window_days"], pcfg["min_days"])
    parts = pd.DataFrame({"macd": macd_pct, "vol": vol_pct, "pos": pos_pct})
    raw = parts.mean(axis=1)
    score = rolling_percentile(raw.dropna(), pcfg["window_days"], cfg["calibrate_min_days"])
    return score.reindex(raw.index), parts


def build_tech(index_frame: pd.DataFrame, chart_days: int = 500) -> tuple[dict, pd.Series]:
    """构建快照 tech 字段; 同时返回 000300 的完整技术分序列(供 A/B/C 对照)。"""
    tcfg = RADAR_CONFIG["tech"]
    out: dict[str, dict] = {}
    full_score = pd.Series(dtype=float)
    for code, name in BENCHMARKS:
        sub = index_frame[index_frame["code"] == code].copy()
        if sub.empty:
            continue
        sub["date"] = pd.to_datetime(sub["date"])
        sub = sub.sort_values("date").set_index("date")
        sub = sub[sub.index >= pd.Timestamp(START)]
        px = sub["close"].astype(float)
        amount = sub["amount"].astype(float)
        if len(px) < 300:
            logger.warning("技术图层跳过 %s: 样本 %d 行不足", code, len(px))
            continue

        m = macd(px)
        ma20 = amount.rolling(20, min_periods=10).mean()
        vol_ratio = amount / ma20.replace(0, np.nan)
        score, parts = tech_score_series(px, amount)
        if code == "000300":
            full_score = score

        anchor = fib_anchor(px, tcfg["fib"]["threshold"])
        levels = fib_levels(anchor["low"]["price"], anchor["high"]["price"], tcfg["fib"]["ratios"])
        hist_std = m["hist"].rolling(252, min_periods=60).std()
        ob = None if pd.isna(hist_std.iloc[-1]) else round(float(2.0 * hist_std.iloc[-1]), 2)

        vol_flags: list[dict] = []
        for d, r in vol_ratio.tail(30).items():
            if pd.isna(r):
                continue
            if r >= tcfg["vol"]["heavy_mult"]:
                vol_flags.append({"date": str(d)[:10], "type": "heavy", "ratio": round(float(r), 2)})
            elif r <= tcfg["vol"]["light_mult"]:
                vol_flags.append({"date": str(d)[:10], "type": "light", "ratio": round(float(r), 2)})

        n = min(chart_days, len(px))
        tail = px.index[-n:] if n else px.index
        def cut(s: pd.Series, _tail=tail) -> list[float | None]:
            return [None if pd.isna(v) else round(float(v), 2) for v in s.reindex(_tail)]
        out[code] = {
            "code": code, "name": name,
            "dates": [str(d)[:10] for d in tail],
            "open": cut(sub["open"]), "high": cut(sub["high"]),
            "low": cut(sub["low"]), "close": cut(sub["close"]),
            "amount": cut(amount),
            "vol_ma5": cut(amount.rolling(5, min_periods=3).mean()),
            "vol_ma10": cut(amount.rolling(10, min_periods=5).mean()),
            "vol_ratio20": cut(vol_ratio),
            "macd_dif": cut(m["dif"]), "macd_dea": cut(m["dea"]), "macd_hist": cut(m["hist"]),
            "macd_ob": ob, "macd_os": None if ob is None else -ob,
            "fib": {**anchor, "levels": levels},
            "divergences": divergences(px, m["dif"], tcfg["divergence"]["lookback"],
                                       tcfg["divergence"]["gap"]),
            "vol_flags": vol_flags,
            "score_now": None if pd.isna(score.iloc[-1]) else round(float(score.iloc[-1]), 1),
            "component_now": {k: (None if pd.isna(v.iloc[-1]) else round(float(v.iloc[-1]), 1))
                              for k, v in parts.items()},
            "score_dates": [str(d)[:10] for d in tail],
            "score_values": cut(score),
        }
        logger.info("技术图层 %s: 蜡烛 %d 根, 斐波那契 %s->%s(%s), 背离 %d 个",
                    code, n, anchor["low"]["date"], anchor["high"]["date"],
                    anchor["direction"], len(out[code]["divergences"]))

    payload = {
        "default_code": "000001",
        "rule": {
            "fib": (f"2014 起日收盘价 zigzag(阈值 {tcfg['fib']['threshold'] * 100:.0f}%)识别波段, "
                    "取最近一组确认高/低点(未走完波段取最新极值); 低=0.000 高=1.000, "
                    "画 0.236/0.382/0.5/0.618/0.809"),
            "vol": (f"成交额/20日均额=量比, >={tcfg['vol']['heavy_mult']:.1f} 放量, "
                    f"<={tcfg['vol']['light_mult']:.1f} 缩量; MA5/MA10 为均额"),
            "macd": "DIF=EMA12-EMA26, DEA=EMA9, 柱=2*(DIF-DEA); 超买/超卖线=柱 ±2 倍标准差(252日)",
            "divergence": (f"价格创 {tcfg['divergence']['lookback']} 日新高/新低而 DIF 未同步, "
                           f"两次极值间隔 >={tcfg['divergence']['gap']} 日"),
            "score": ("MACD 热度/量能热度/250日区间位置 三项滚动十年分位等权平均, 再映射到 0-100; "
                      "高=过热, 低=过冷; 独立展示, 不进综合分"),
        },
        "note": "技术图层与独立技术分仅作证据展示, 综合分权重与阈值均未改动",
        "benchmarks": out,
    }
    return payload, full_score


def ab_test(comp: pd.DataFrame, tech_full: pd.Series, px: pd.Series,
            detail: pd.DataFrame) -> dict:
    """A=现行综合分, B=0.8*A+0.2*技术分, C=仅技术分; 同一事件研究口径对照。"""
    from scripts.radar import backtest
    from scripts.radar.indicators import extract_signals

    if comp.empty or tech_full.empty:
        return {"available": False, "reason": "综合分或技术分为空"}

    w = RADAR_CONFIG["tech"]["score"]["blend_weight_tech"]
    base = comp.set_index(pd.to_datetime(comp["date"]))["score"].astype(float)
    tech = tech_full.reindex(base.index)
    b = base.where(tech.isna(), (1.0 - w) * base + w * tech)
    groups: dict[str, pd.Series] = {"A": base, "B": b, "C": tech}

    out: dict[str, dict] = {}
    events_full: dict[str, pd.DataFrame] = {}
    for name, s in groups.items():
        frame = pd.DataFrame({"date": s.index, "score": s.values,
                              "coverage": 1.0, "raw_score": s.values}).dropna(subset=["score"])
        sig = extract_signals(frame)
        res = backtest.run_event_study(sig, px, detail)
        events_full[name] = pd.DataFrame(res["events"])
        out[name] = {
            "signals": len(sig),
            "dates": [str(d)[:10] for d in sig["date"]] if len(sig) else [],
            "summary": res["summary"],
        }

    horizons = RADAR_CONFIG["backtest"]["forward_days"]
    compare: dict[str, dict] = {}
    for other in ("B", "C"):
        per_h: dict[str, dict] = {}
        for h in horizons:
            pa, pb = events_full["A"], events_full[other]
            hit_a = _hits(pa, h)
            hit_b = _hits(pb, h)
            na, nb = len(hit_a), len(hit_b)
            pval = None
            if na and nb:
                pval = round(_two_prop_p(float(np.mean(hit_a)), float(np.mean(hit_b)), na, nb), 4)
            per_h[f"{h}d"] = {
                "n_A": na, "n_B": nb,
                "win_A": None if na == 0 else round(float(np.mean(hit_a)), 4),
                "win_B": None if nb == 0 else round(float(np.mean(hit_b)), 4),
                "p_value": pval,
            }
        compare[f"{other}_vs_A"] = per_h

    verdict = _verdict(compare)
    return {
        "available": True,
        "weights": {"A": "现行综合分", "B": "0.8*综合分 + 0.2*技术分", "C": "仅技术分"},
        "blend_weight_tech": w,
        "groups": out,
        "compare": compare,
        "hit_definition": "顶部信号后 ret<0 记为命中; 底部信号后 ret>0 记为命中",
        "p_threshold": 0.10,
        "verdict": verdict,
        "note": "B/C 只用于验证技术分是否值得加权; 未通过显著性检验则仅作展示, 不改综合分",
    }


def _hits(events: pd.DataFrame, h: int) -> list[int]:
    if events.empty or f"ret{h}" not in events.columns:
        return []
    out = []
    for _, e in events.iterrows():
        ret = e[f"ret{h}"]
        if pd.isna(ret):
            continue
        out.append(1 if ((ret < 0) if e["type"] == "top" else (ret > 0)) else 0)
    return out


def _two_prop_p(p1: float, p2: float, n1: int, n2: int) -> float:
    """两比例差异的双侧 z 检验 p 值。"""
    if n1 == 0 or n2 == 0:
        return 1.0
    p = (p1 * n1 + p2 * n2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    if se == 0:
        return 1.0
    z = abs(p1 - p2) / se
    return 2.0 * (1.0 - 0.5 * (1.0 + math.erf(z / math.sqrt(2.0))))


def _verdict(compare: dict) -> str:
    """B 需在 >=2 个周期上 p<0.10 且胜率更高才算显著优于 A。"""
    b = compare.get("B_vs_A", {})
    better = worse = 0
    for v in b.values():
        if v["win_A"] is None or v["win_B"] is None or v["p_value"] is None:
            continue
        if v["p_value"] < 0.10:
            if v["win_B"] > v["win_A"]:
                better += 1
            elif v["win_B"] < v["win_A"]:
                worse += 1
    if better >= 2 and worse == 0:
        return "B 显著优于 A, 可考虑给技术分 20% 权重"
    if worse >= 2:
        return "B 显著劣于 A, 不建议加权"
    return "不显著, 仅作展示, 不改综合分权重"
