"""板块「放量长上影线」风险提醒: 形态识别 + 板块聚合 + 有效性统计。

形态(全部参数见 config.RADAR_CONFIG["pattern"]):
- 长上影线: 上影线/当日振幅 ≥ shadow_ratio(默认 0.5) 且 上影线 ≥ 实体 x shadow_body_mult(默认 2.0)
- 放量: 当日成交量 ≥ 前 volume_window(20)日均量 x volume_mult(默认 2.0)
- 位置条件(可选, 默认关闭): 距 position_window(60)日高点 ≤ position_high_pct%

板块聚合: 同一行业同日命中 ≥ sector_min_count(3) 只, 或占板块可交易只数 ≥ sector_min_share(10%)
→ 生成该板块风险提醒(附命中个股、占比、平均上影比例、平均放量倍数)。

有效性验证: 板块信号触发后 5/10/20 日板块等权收益、期间最大回撤、相对沪深300超额,
并与「全部板块日(随机同日)」「仅放量」「仅长上影」三组对照。
价格使用不复权收盘; 除权日打标(ex_rights)的个股当日收益置空, 避免除权跳空污染板块等权收益。
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from scripts.radar import store
from scripts.radar.config import RADAR_CONFIG

logger = logging.getLogger(__name__)


def _prepare(stock: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """逐股计算形态所需字段与三类条件。"""
    w = int(cfg["volume_window"])
    pw = int(cfg["position_window"])
    df = stock.copy()
    # TDX 原始日期是 YYYYMMDD 字符串, 统一成 YYYY-MM-DD 才能与指数/板块数据对齐
    df["date"] = pd.to_datetime(df["date"].astype(str), format="%Y%m%d").dt.strftime("%Y-%m-%d")
    df = df.sort_values(["code", "date"], kind="mergesort")
    g = df.groupby("code", sort=False)
    # 均量用前 20 日(不含当日), 否则当日放量会抬高自身基准
    df["vol_ma"] = g["volume"].transform(
        lambda s: s.shift(1).rolling(w, min_periods=max(5, w // 2)).mean())
    df["high_n"] = g["high"].transform(
        lambda s: s.rolling(pw, min_periods=pw // 2).max())
    rng = (df["high"] - df["low"]).replace(0, np.nan)
    df["amp"] = rng / df["prev_close"].replace(0, np.nan)
    df["upper"] = df["high"] - df[["open", "close"]].max(axis=1)
    df["body"] = (df["close"] - df["open"]).abs()
    df["upper_ratio"] = df["upper"] / rng
    df["vol_mult"] = df["volume"] / df["vol_ma"].replace(0, np.nan)
    df["ret"] = df["close"] / df["prev_close"].replace(0, np.nan) - 1.0
    df.loc[df["ex_rights"].fillna(False), "ret"] = np.nan

    valid = df["amp"] >= float(cfg["min_amplitude"])
    shadow = valid & (df["upper_ratio"] >= float(cfg["shadow_ratio"])) & (
        df["upper"] >= float(cfg["shadow_body_mult"]) * df["body"])
    surge = df["vol_mult"] >= float(cfg["volume_mult"])
    df["shadow_only"] = shadow
    df["vol_only"] = surge
    df["signal"] = shadow & surge
    if cfg.get("position_high_pct"):
        pos = (df["high_n"] - df["close"]) / df["high_n"].replace(0, np.nan) * 100.0
        df["signal"] &= pos <= float(cfg["position_high_pct"])
    return df


def _aggregate(flags: pd.DataFrame, boards: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """板块x日期聚合: 命中数、占比、平均上影比例与放量倍数。"""
    m = flags.merge(boards[["code", "board"]], on="code", how="inner", suffixes=("", "_b"))
    b = boards.drop_duplicates("code")[["code", "name"]] if "name" in boards.columns else None
    if b is not None:
        m = m.merge(b, on="code", how="left")
    m["upper_hit"] = np.where(m["signal"], m["upper_ratio"], np.nan)
    m["vol_hit"] = np.where(m["signal"], m["vol_mult"], np.nan)
    m["pct_hit"] = np.where(m["signal"], m["ret"] * 100.0, np.nan)
    agg = m.groupby(["board", "date"]).agg(
        eligible=("code", "size"),
        hits=("signal", "sum"),
        vol_hits=("vol_only", "sum"),
        shadow_hits=("shadow_only", "sum"),
        avg_upper_ratio=("upper_hit", "mean"),
        avg_vol_mult=("vol_hit", "mean"),
        avg_pct_chg=("pct_hit", "mean"),
    ).reset_index()
    agg["hits"] = agg["hits"].astype(int)
    agg["share"] = agg["hits"] / agg["eligible"].replace(0, np.nan)
    agg["trigger"] = (agg["hits"] >= int(cfg["sector_min_count"])) | (
        agg["share"] >= float(cfg["sector_min_share"]))
    return m, agg


def _fwd(r: np.ndarray, i: int, h: int) -> tuple[float, float] | None:
    """从第 i 日收盘起算的前 h 日等权收益与期间最大回撤。"""
    seg = r[i + 1: i + 1 + h]
    seg = seg[np.isfinite(seg)]
    if len(seg) < max(2, int(h * 0.6)):
        return None
    nav = np.cumprod(1.0 + seg)
    mdd = float((nav / np.maximum.accumulate(nav) - 1.0).min())
    return float(nav[-1] - 1.0), mdd


def _evaluate(m: pd.DataFrame, agg: pd.DataFrame, bench: pd.Series, cfg: dict) -> dict:
    """事件统计: 信号组 vs 仅放量 / 仅长上影 / 随机同日(全部板块日)。"""
    horizons = [int(h) for h in cfg["forward_days"]]
    board_ret = m.groupby(["date", "board"])["ret"].mean().unstack("board").sort_index()
    dates = list(board_ret.index)
    pos = {d: i for i, d in enumerate(dates)}
    bs = bench.reindex(dates).to_numpy(dtype=float)

    def bench_fwd(i: int, h: int) -> float:
        seg = bs[i + 1: i + 1 + h]
        seg = seg[np.isfinite(seg)]
        if len(seg) < 1:
            return np.nan
        return float(seg[-1] / bs[i] - 1.0) if np.isfinite(bs[i]) else np.nan

    groups = {
        "signal": agg[agg["trigger"]],
        "vol_only": agg[(agg["vol_hits"] >= int(cfg["sector_min_count"])) |
                        (agg["vol_hits"] / agg["eligible"] >= float(cfg["sector_min_share"]))],
        "shadow_only": agg[(agg["shadow_hits"] >= int(cfg["sector_min_count"])) |
                           (agg["shadow_hits"] / agg["eligible"] >= float(cfg["sector_min_share"]))],
        "baseline": agg,
    }
    out: dict[str, dict] = {}
    for gname, gdf in groups.items():
        rows = []
        for _, e in gdf.iterrows():
            i = pos.get(e["date"])
            if i is None or e["board"] not in board_ret.columns:
                continue
            r = board_ret[e["board"]].to_numpy(dtype=float)
            row = {"date": e["date"], "board": e["board"]}
            for h in horizons:
                f = _fwd(r, i, h)
                row[f"ret{h}"] = np.nan if f is None else f[0]
                row[f"mdd{h}"] = np.nan if f is None else f[1]
                row[f"ex{h}"] = row[f"ret{h}"] - bench_fwd(i, h)
            rows.append(row)
        if not rows:
            continue
        d = pd.DataFrame(rows)
        rec: dict[str, dict] = {"samples": len(d)}
        for h in horizons:
            col, mdd, ex = d[f"ret{h}"].dropna(), d[f"mdd{h}"].dropna(), d[f"ex{h}"].dropna()
            rec[f"{h}d"] = {
                "n": len(col),
                "mean": None if col.empty else round(float(col.mean()) * 100, 2),
                "median": None if col.empty else round(float(col.median()) * 100, 2),
                "win": None if col.empty else round(float((col < 0).mean()) * 100, 1),
                "mdd": None if mdd.empty else round(float(mdd.mean()) * 100, 2),
                "excess_mean": None if ex.empty else round(float(ex.mean()) * 100, 2),
                "excess_win": None if ex.empty else round(float((ex < 0).mean()) * 100, 1),
            }
        out[gname] = rec
    return out


def build_pattern_module(include_history: bool = True) -> dict:
    """执行形态识别与板块聚合, 返回快照用字典(含最新信号与统计)。"""
    cfg = RADAR_CONFIG["pattern"]
    stock = store.read_frame("tdx_stock.parquet")
    boards = store.read_frame("board_members.parquet")
    idx = store.read_frame("index_daily.parquet")
    if stock.empty or boards.empty or idx.empty:
        logger.warning("缺少 TDX 个股/板块/指数数据, 跳过板块形态模块")
        store.update_status("pattern", status="missing", reason="缺少 tdx_stock / board_members / index_daily")
        return {"available": False, "reason": "缺少 TDX 个股或板块成分数据"}

    flags = _prepare(stock, cfg)
    m, agg = _aggregate(flags, boards, cfg)
    latest = str(flags["date"].max())[:10]
    triggered = agg[agg["trigger"]].copy()

    # 最新交易日信号(附命中个股明细)
    detail_day = m[(m["date"] == latest) & m["signal"]]
    names = detail_day.drop_duplicates("code")[["code", "name"]] if "name" in detail_day.columns else None
    signals = []
    for _, e in triggered[triggered["date"] == latest].sort_values("hits", ascending=False).iterrows():
        hits = detail_day[detail_day["board"] == e["board"]].sort_values("vol_mult", ascending=False)
        items = []
        for _, r in hits.iterrows():
            items.append({
                "code": r["code"],
                "name": (names.set_index("code")["name"].get(r["code"]) if names is not None else None),
                "close": round(float(r["close"]), 2),
                "pct_chg": None if pd.isna(r["ret"]) else round(float(r["ret"]) * 100, 2),
                "upper_ratio": round(float(r["upper_ratio"]), 3),
                "vol_mult": round(float(r["vol_mult"]), 2),
            })
        signals.append({
            "board": e["board"], "date": e["date"], "hits": int(e["hits"]),
            "eligible": int(e["eligible"]), "share": round(float(e["share"]) * 100, 1),
            "avg_upper_ratio": round(float(e["avg_upper_ratio"]), 3),
            "avg_vol_mult": round(float(e["avg_vol_mult"]), 2),
            "stocks": items,
        })

    bench = idx[idx.code == "000300"].copy()
    bench["date"] = pd.to_datetime(bench["date"]).dt.strftime("%Y-%m-%d")
    bench = bench.drop_duplicates("date").set_index("date")["close"].astype(float).sort_index()
    stats = _evaluate(m, agg, bench, cfg)

    hist = None
    if include_history:
        hist = triggered.copy()
        hist["avg_upper_ratio"] = hist["avg_upper_ratio"].round(3)
        hist["avg_vol_mult"] = hist["avg_vol_mult"].round(2)
        hist["share"] = hist["share"].round(4)
        store.write_frame("pattern_signals.parquet", hist)

    # 最新日全部「长上影」候选(尚未叠加放量条件), 供 App 内改倍数阈值后本地重算
    cand = m[(m["date"] == latest) & m["shadow_only"]].copy()
    cand_rows = []
    for _, r in cand.sort_values("vol_mult", ascending=False).iterrows():
        cand_rows.append({
            "code": r["code"],
            "board": r["board"],
            "name": r.get("name") if "name" in cand.columns else None,
            "upper_ratio": round(float(r["upper_ratio"]), 3),
            "vol_mult": round(float(r["vol_mult"]), 2) if pd.notna(r["vol_mult"]) else None,
            "pct_chg": None if pd.isna(r["ret"]) else round(float(r["ret"]) * 100, 2),
            "close": round(float(r["close"]), 2),
        })
    eligible_map = {
        str(b): int(n) for b, n in
        agg[agg["date"] == latest].set_index("board")["eligible"].items()
    }

    recent = triggered[triggered["date"] >= str(pd.Timestamp(latest) - pd.Timedelta(days=15))][
        ["date", "board", "hits", "eligible", "share", "avg_upper_ratio", "avg_vol_mult"]
    ].sort_values(["date", "hits"], ascending=[False, False]).round(4).to_dict("records")

    store.update_status("pattern", status="ok", params=cfg, latest=latest,
                        signals_today=len(signals), history_events=len(triggered),
                        stock_rows=len(flags), board_count=int(boards["board"].nunique()))
    return {
        "available": True,
        "date": latest,
        "params": cfg,
        "signals": signals,
        "candidates": cand_rows,
        "eligible_by_board": eligible_map,
        "recent": recent,
        "stats": stats,
        "history_events": len(triggered),
        "universe": {
            "boards": int(boards["board"].nunique()),
            "board_codes": int(boards["code"].nunique()),
            "stock_codes": int(flags["code"].nunique()),
            "range": f"{flags['date'].min()} ~ {latest}",
            "board_source": str(boards["source"].iloc[0]) if "source" in boards.columns else "unknown",
            "note": "行业板块(申万一级), 题材/概念成分无干净历史映射, 未提供",
        },
    }
