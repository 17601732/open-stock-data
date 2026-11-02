"""事件研究法回测: 信号触发后 N 个交易日的指数收益、期间最大回撤、胜率、样本数。

不是交易策略回测, 不模拟买卖与仓位; 收益自信号日收盘起算(信号于当日收盘后确认, 无未来函数)。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from scripts.radar.config import RADAR_CONFIG


def _forward_stats(px: pd.Series, dt: pd.Timestamp, horizon: int) -> dict | None:
    """前向 N 日收益/最大回撤/最大涨幅; 数据不足 N 日时按实际可得天数计。"""
    loc = px.index.searchsorted(dt)
    if loc >= len(px):
        return None
    end = min(loc + horizon, len(px) - 1)
    if end <= loc:
        return None
    window = px.iloc[loc: end + 1].to_numpy(dtype=float)
    base = window[0]
    path = window / base - 1.0
    peak = np.maximum.accumulate(window)
    return {
        "ret": float(path[-1]),
        "mdd": float((window / peak - 1.0).min()),
        "max_gain": float(path.max()),
        "days": int(end - loc),
    }


def run_event_study(signals: pd.DataFrame, px: pd.Series, detail: pd.DataFrame) -> dict:
    """输出事件明细、分区间汇总、样本内外拆分与指标贡献度。"""
    cfg = RADAR_CONFIG["backtest"]
    horizons = cfg["forward_days"]
    oos_start = pd.Timestamp(cfg["oos_start"])
    ood = cfg["ood_periods"]

    rows = []
    for _, ev in signals.iterrows():
        dt = pd.Timestamp(ev["date"])
        row = {
            "date": str(dt.date()), "type": ev["type"],
            "score": round(float(ev["score"]), 1),
            "segment": "样本外" if dt >= oos_start else "样本内",
            "ood": next((p["label"] for p in ood
                         if pd.Timestamp(p["start"]) <= dt <= pd.Timestamp(p["end"])), None),
        }
        for h in horizons:
            st = _forward_stats(px, dt, h)
            row[f"ret{h}"] = None if st is None else round(st["ret"], 4)
            row[f"mdd{h}"] = None if st is None else round(st["mdd"], 4)
        rows.append(row)
    events = pd.DataFrame(rows)

    def summarize(sub: pd.DataFrame) -> dict:
        out: dict[str, dict] = {}
        for t in ("top", "bottom"):
            s = sub[sub.type == t] if not sub.empty else sub
            rec: dict[str, object] = {"样本数": len(s)}
            for h in horizons:
                col = s[f"ret{h}"].dropna() if len(s) else pd.Series(dtype=float)
                if len(col):
                    win = float((col < 0).mean()) if t == "top" else float((col > 0).mean())
                    rec[f"{h}日均值"] = round(float(col.mean()), 4)
                    rec[f"{h}日中位数"] = round(float(col.median()), 4)
                    rec[f"{h}日胜率"] = round(win, 3)
                    rec[f"{h}日平均最大回撤"] = round(float(s[f"mdd{h}"].dropna().mean()), 4)
                else:
                    rec[f"{h}日均值"] = None
            out[t] = rec
        return out

    summary = {
        "全样本": summarize(events),
        "样本内(<2021)": summarize(events[events.segment == "样本内"]),
        "样本外(>=2021)": summarize(events[events.segment == "样本外"]),
    }
    for p in ood:
        summary[p["label"]] = summarize(events[events.ood == p["label"]])

    contribution: dict[str, dict] = {}
    if len(events) and not detail.empty:
        det = detail.copy()
        det["date"] = pd.to_datetime(det["date"]).dt.strftime("%Y-%m-%d")
        for t in ("top", "bottom"):
            days = set(events.loc[events.type == t, "date"])
            d = det[det.date.isin(days) & det.oriented.notna()].copy()
            if len(d):
                d["part"] = d.weight * d.oriented
                tot = d.groupby("date")["part"].transform("sum")
                share = d["part"] / tot.replace(0, np.nan)
                contribution[t] = (share.groupby(d.key).mean().sort_values(ascending=False)
                                   .round(4).to_dict())

    return {
        "params": {
            "forward_days": horizons,
            "cooldown_days": cfg["cooldown_days"],
            "top_threshold": RADAR_CONFIG["composite"]["top_threshold"],
            "bottom_threshold": RADAR_CONFIG["composite"]["bottom_threshold"],
            "oos_start": cfg["oos_start"],
            "benchmark": cfg["benchmark"],
        },
        "summary": summary,
        "contribution": contribution,
        "events": rows,
    }
