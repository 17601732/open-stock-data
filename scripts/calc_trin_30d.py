# ruff: noqa
import numpy as np
import pandas as pd
from pathlib import Path

# 1. 读取数据
p = Path("data/cn_bars.parquet")
if not p.exists():
    raise FileNotFoundError("cn_bars.parquet not found")

df = pd.read_parquet(p)

# 2. 过滤数据 (只留沪深A股，剔除北交所 8开头和 4/9开头)
df = df[~df["code"].astype(str).str.startswith(("8", "4", "9"))]
df["date"] = pd.to_datetime(df["date"])
df = df.sort_values(["code", "date"])

# 3. 计算涨跌
df["prev_close"] = df.groupby("code")["close"].shift(1)
df["is_up"] = df["close"] > df["prev_close"]
df["is_down"] = df["close"] < df["prev_close"]

# 4. 按日聚合 (这里用 numpy where 或者 pandas 向量化最快)
def calc_daily(group):
    up_count = group["is_up"].sum()
    down_count = group["is_down"].sum()
    up_vol = group.loc[group["is_up"], "volume"].sum()
    down_vol = group.loc[group["is_down"], "volume"].sum()
    
    # 防除零
    adv_ratio = up_count / down_count if down_count > 0 else np.nan
    vol_ratio = up_vol / down_vol if down_vol > 0 else np.nan
    trin = adv_ratio / vol_ratio if vol_ratio > 0 else np.nan
    
    return pd.Series({
        "up_count": up_count, 
        "down_count": down_count, 
        "adv_ratio": adv_ratio, 
        "vol_ratio": vol_ratio, 
        "trin": trin
    })

daily = df.groupby("date").apply(calc_daily).reset_index()

# 5. 计算30日平滑并输出
daily["trin_30d"] = daily["trin"].rolling(30, min_periods=1).mean()

out = daily[["date", "up_count", "down_count", "adv_ratio", "vol_ratio", "trin", "trin_30d"]]
out.to_csv("trin_30d.csv", index=False)
print(out.tail(10).to_string())
