import pandas as pd
import numpy as np
from pathlib import Path

# 读取原仓库 cn_bars（未复权，TRIN无需复权）
p = Path("data/cn_bars.parquet")
if not p.exists():
    raise FileNotFoundError("请先确保 data-cn-bars 包含 cn_bars.parquet")

df = pd.read_parquet(p)

# 排除北交所（8开头）及非A股（如科创板688已属沪市，保留；仅排8开头BJ）
df = df[~df["code"].astype(str).str.startswith("8")]
df["date"] = pd.to_datetime(df["date"])

# 按代码排序，计算前收
df = df.sort_values(["code", "date"])
df["prev_close"] = df.groupby("code")["close"].shift(1)

# 涨跌判定（原仓库逻辑）
df["is_up"] = df["close"] > df["prev_close"]
df["is_down"] = df["close"] < df["prev_close"]

# 每日广度聚合
daily = df.groupby("date").agg(
    up_count=("is_up", "sum"),
    down_count=("is_down", "sum"),
    up_volume=("volume", lambda x: x[df.loc[x.index, "is_up"]].sum()),
    down_volume=("volume", lambda x: x[df.loc[x.index, "is_down"]].sum()),
).reset_index()

# TRIN = (上涨家数/下跌家数) / (上涨量/下跌量)
daily["adv_ratio"] = daily["up_count"] / daily["down_count"].replace(0, np.nan)
daily["vol_ratio"] = daily["up_volume"] / daily["down_volume"].replace(0, np.nan)
daily["trin"] = daily["adv_ratio"] / daily["vol_ratio"]

# 30日滚动均值（新增）
daily["trin_30d"] = daily["trin"].rolling(30, min_periods=1).mean()

# 输出
out = daily[["date", "up_count", "down_count", "adv_ratio", "vol_ratio", "trin", "trin_30d"]]
out.to_csv("trin_30d.csv", index=False)

print(out.tail(10).to_string())
