import pandas as pd
import numpy as np
from pathlib import Path

# 读取原仓库 cn_bars.parquet
df = pd.read_parquet("cn_bars.parquet")
df["date"] = pd.to_datetime(df["date"])

# 按日期判断涨跌（收盘价 > 前收为涨）
df = df.sort_values(["code", "date"])
df["prev_close"] = df.groupby("code")["close"].shift(1)
df["is_up"] = df["close"] > df["prev_close"]
df["is_down"] = df["close"] < df["prev_close"]

# 按日聚合
daily = df.groupby("date").agg(
    up_count=("is_up", "sum"),
    down_count=("is_down", "sum"),
    up_volume=("volume", lambda x: x[df.loc[x.index, "is_up"]].sum()),
    down_volume=("volume", lambda x: x[df.loc[x.index, "is_down"]].sum()),
)

# 计算 TRIN
daily["adv_ratio"] = daily["up_count"] / daily["down_count"].replace(0, np.nan)
daily["vol_ratio"] = daily["up_volume"] / daily["down_volume"].replace(0, np.nan)
daily["trin"] = daily["adv_ratio"] / daily["vol_ratio"]

# 30日 TRIN（滚动均值）
daily["trin_30d"] = daily["trin"].rolling(30).mean()

# 输出
out = daily[["up_count", "down_count", "adv_ratio", "vol_ratio", "trin", "trin_30d"]]
out.to_csv("trin_30d.csv")
out.tail(10).to_string()
