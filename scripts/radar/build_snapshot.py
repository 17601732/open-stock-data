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

import json
import shutil
from typing import TYPE_CHECKING

import pandas as pd

from scripts.radar import store
from scripts.radar.config import RADAR_CONFIG
from scripts.radar.indicators import weekly_history

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
    for key, meta in weights.items():
        row = det[(det.key == key)]
        if row.empty or pd.isna(row.iloc[0]["pct"]):
            st = status.get(key, {})
            missing.append({"key": key, "name": meta["name"], "group": meta["group"],
                            "reason": st.get("reason", "当日无有效值")})
            continue
        r = row.iloc[0]
        indicators.append({
            "key": key, "name": meta["name"], "group": meta["group"],
            "weight": meta["weight"], "direction": meta["direction"],
            "value": None if pd.isna(r["raw"]) else round(float(r["raw"]), 4),
            "percentile": round(float(r["pct"]), 1),
            "oriented": round(float(r["oriented"]), 1),
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

    latest_close = px.iloc[-1] if len(px) else None
    score = float(latest["score"])
    zone = ("top" if score >= RADAR_CONFIG["composite"]["top_threshold"]
            else "bottom" if score <= RADAR_CONFIG["composite"]["bottom_threshold"] else "neutral")

    notes = [
        "宽度/TRIN/换手/涨跌停基于沪深300+中证500当前成分股固定样本池(约800只), 非全市场, 存在生存者偏差",
        "两融余额为沪深两市合计, 分母用样本池流通市值, 绝对水平偏高但历史分位口径一致",
        "涨跌停家数为样本池内|涨跌幅|>=9.5%的近似计数, 非全市场精确口径(未区分主板/创业板/ST)",
        "期权PCR仅覆盖实际可得区间(起点见 meta.source_coverage.option_pcr), 缺失日期不做前向填充",
        "估值类为乐咕乐股口径, 沪深300 PE/PB 为月度频率, 已按日对齐(近端阶梯状)",
        "个股使用不复权价格, 除权日附近MA60/52周高低点存在轻微断层",
        "分位为滚动十年窗口(不足十年用扩展窗口, 至少250个观测), 严格无未来函数",
        "回测为事件研究, 非交易策略回测; 2021年起为样本外",
    ]

    snapshot = {
        "meta": {
            "generated_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"),
            "data_cutoff": latest_date,
            "latest_index_date": str(px.index.max())[:10] if len(px) else None,
            "source_coverage": status,
            "missing_dates": _missing_trading_dates(score_dates, px)[-60:],
            "missing_indicators": missing,
            "anomalies": anomalies or [],
            "notes": notes,
            "snapshot_url": SNAPSHOT_URL,
            "schema_version": 1,
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
