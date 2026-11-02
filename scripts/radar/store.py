"""雷达数据落盘: 本地 Parquet/CSV, 不引入数据库服务。

目录约定 data/radar/:
- index_daily.parquet   指数日线
- stock_daily.parquet   样本池个股日线(增量追加)
- margin_daily.parquet  两融余额
- bond_yield.parquet    10年期国债收益率
- valuation.parquet     沪深300/全A PE、PB
- option_pcr_etf.csv    50ETF/300ETF 期权 PCR
- holder_change.parquet 股东增减持明细
- fetch_status.json     各数据源状态与数据起点
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "radar"

INDEX_SCHEMA = ["code", "date", "open", "high", "low", "close", "volume", "amount"]
STOCK_SCHEMA = ["code", "date", "close", "pct_chg", "volume", "amount", "turnover"]


def ensure_dir() -> Path:
    """确保数据目录存在。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR


def read_frame(name: str) -> pd.DataFrame:
    """读取 data/radar 下的 parquet/csv, 不存在返回空表。"""
    path = DATA_DIR / name
    if not path.exists():
        return pd.DataFrame()
    if path.suffix == ".csv":
        df = pd.read_csv(path)
        if "code" in df.columns:
            df["code"] = df["code"].astype(str).str.zfill(6)
        return df
    return pd.read_parquet(path)


def write_frame(name: str, df: pd.DataFrame) -> Path:
    """写入 data/radar 下的文件, 自动建目录。"""
    ensure_dir()
    path = DATA_DIR / name
    if path.suffix == ".csv":
        df.to_csv(path, index=False)
    else:
        df.to_parquet(path, index=False)
    logger.info("写入 %s (%d 行)", path, len(df))
    return path


def upsert(name: str, df: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    """按 keys 去重合并(新值覆盖旧值), 返回合并结果并落盘。"""
    if df.empty:
        return read_frame(name)
    old = read_frame(name)
    merged = pd.concat([old, df], ignore_index=True) if not old.empty else df
    merged = merged.drop_duplicates(subset=keys, keep="last")
    sort_col = "date" if "date" in merged.columns else keys[0]
    merged = merged.sort_values(sort_col).reset_index(drop=True)
    write_frame(name, merged)
    return merged


def read_status() -> dict:
    """读取数据源状态与数据起点。"""
    path = DATA_DIR / "fetch_status.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_status(status: dict) -> Path:
    """写入数据源状态与数据起点。"""
    ensure_dir()
    path = DATA_DIR / "fetch_status.json"
    path.write_text(json.dumps(status, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def update_status(source: str, **fields) -> dict:
    """更新单个数据源状态并持久化; 成功时清除历史失败原因。"""
    status = read_status()
    entry = status.get(source, {})
    entry.update(fields)
    if fields.get("status") == "ok":
        entry.pop("reason", None)
    status[source] = entry
    write_status(status)
    return status
