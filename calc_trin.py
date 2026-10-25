import pandas as pd
import glob

# 自动寻找项目中生成的 parquet 数据文件
parquet_files = glob.glob("**/*.parquet", recursive=True)
if not parquet_files:
    raise FileNotFoundError("未找到数据文件，请检查回填步骤是否成功。")

print(f"找到数据文件: {parquet_files}")
df = pd.read_parquet(parquet_files[0])
df['date'] = pd.to_datetime(df['date'])

# 兼容不同数据源的字段名，判断涨跌
if 'pct_chg' in df.columns:
    df['change'] = df['pct_chg']
else:
    df['change'] = df['close'] - df['open']

def calc_daily_trin(group):
    up = group[group["change"] > 0]
    down = group[group["change"] < 0]
    
    if len(down) == 0 or down["amount"].sum() == 0:
        return pd.Series({"trin_raw": None})
        
    # TRIN 计算公式
    trin = (len(up) / len(down)) / (up["amount"].sum() / down["amount"].sum())
    return pd.Series({"trin_raw": trin})

# 按日期分组计算并平滑
result = df.groupby("date").apply(calc_daily_trin).reset_index()
result["trin_smooth_21"] = result["trin_raw"].rolling(window=21, min_periods=1).mean()

# 保存结果
result.to_csv("trin_result.csv", index=False)
print("计算完成！")
print(result.tail(10))
