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
import shutil
from typing import TYPE_CHECKING

import pandas as pd

from scripts.radar import store
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
    """截止日涨跌家数一致性: 涨+跌+平必须等于有数据的样本数。"""
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


def build_snapshot(comp: pd.DataFrame, detail: pd.DataFrame, signals: pd.DataFrame,
                   backtest_result: dict, indicator_frame: pd.DataFrame,
                   include_history: bool = True,
                   anomalies: list[dict] | None = None) -> dict:
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

    idx = store.read_frame("index_daily.parquet")
    px = pd.Series(dtype=float)
    if not idx.empty:
        sub = idx[idx.code == "000300"].copy()
        sub["date"] = pd.to_datetime(sub["date"]).dt.strftime("%Y-%m-%d")
        px = sub.set_index("date")["close"].sort_index()

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
    signals_confirmed = cf.to_dict("records") if len(cf) else []

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

    latest_close = px.iloc[-1] if len(px) else None
    score = float(latest["score"])
    zone = ("top" if score >= RADAR_CONFIG["composite"]["top_threshold"]
            else "bottom" if score <= RADAR_CONFIG["composite"]["bottom_threshold"] else "neutral")

    notes = [
        "交易日历以沪深300指数实际交易日为准; meta.gap_info 中 holiday=节假日正常休市, missing=真实缺失",
        "两融余额由交易所 T+1/T+2 发布, 截止日当日可能无值, 属正常发布滞后(不作为缺口); 前值填充窗口按指标配置",
        "宽度/TRIN/换手/涨跌停基于沪深300+中证500当前成分股固定样本池(约800只), 非全市场, 存在生存者偏差",
        "两融余额为沪深两市合计, 分母用样本池流通市值, 绝对水平偏高但历史分位口径一致",
        "涨跌停家数为样本池内|涨跌幅|>=9.5%的近似计数, 非全市场精确口径(未区分主板/创业板/ST)",
        "期权PCR仅覆盖实际可得区间(起点见 meta.source_coverage.option_pcr), 缺失日期不做前向填充",
        "估值类为乐咕乐股口径, 沪深300 PE/PB 为月度频率, 已按日对齐(近端阶梯状)",
        "个股使用不复权价格, 除权日附近MA60/52周高低点存在轻微断层",
        "分位为滚动十年窗口(不足十年用扩展窗口, 至少250个观测), 严格无未来函数",
        "回测为事件研究, 非交易策略回测; 2021年起为样本外",
        "增减持明细按公告日聚合(公告日不等于增减持发生日); 无公告的交易日留空, 不做 0 填充, 聚合时「无公告」与「净额为零」可区分",
        "两融明细口径: 融资余额/融券余额/合计为存量(周/月取期末值), 融资买入额/融券卖出量为流量(周/月求和), 两融余额占比为比率(周/月取均值)",
        "TRIN 日频原始值等参考指标为展示项(权重 0), 不参与综合分; 综合分权重与阈值均未改动",
        "TRIN 复用仓库 trin_2020_now.csv(新浪当日全市场快照), 上游无历史回溯接口; 缺口见 meta.indicator_gaps",
    ]

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
            "snapshot_url": SNAPSHOT_URL,
            "schema_version": 2,
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
        "series": series.to_dict("records"),
        "signals": sig.to_dict("records") if len(sig) else [],
        "backtest": backtest_result,
        "indicator_history": weekly_history(indicator_frame, list(weights)) if include_history else {},
        "indicator_daily": indicator_daily,
        "score_ta": score_ta,
        "signals_confirmed": signals_confirmed,
    }
    return snapshot


def write_snapshot(snapshot: dict, android_assets: Path | None = None) -> Path:
    """写入 data/radar/snapshot.json, 可选复制到 Android assets。"""
    path = store.ensure_dir() / "snapshot.json"
    path.write_text(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    if android_assets:
        android_assets.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, android_assets / "snapshot.json")
    return path
