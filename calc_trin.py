import glob

import pandas as pd

# Find the generated parquet file
parquet_files = glob.glob("**/*.parquet", recursive=True)
if not parquet_files:
    raise FileNotFoundError("Data file not found. Please check the backfill step.")

print(f"Found data file: {parquet_files}")
df = pd.read_parquet(parquet_files[0])
df["date"] = pd.to_datetime(df["date"])

# Check column names for different data sources
if "pct_chg" in df.columns:
    df["change"] = df["pct_chg"]
else:
    df["change"] = df["close"] - df["open"]


def calc_daily_trin(group):
    up = group[group["change"] > 0]
    down = group[group["change"] < 0]

    if len(down) == 0 or down["amount"].sum() == 0:
        return pd.Series({"trin_raw": None})

    # TRIN formula
    trin = (len(up) / len(down)) / (up["amount"].sum() / down["amount"].sum())
    return pd.Series({"trin_raw": trin})


# Calculate TRIN grouped by date
result = df.groupby("date").apply(calc_daily_trin).reset_index()
result["trin_smooth_21"] = result["trin_raw"].rolling(window=21, min_periods=1).mean()

# Save results
result.to_csv("trin_result.csv", index=False)
print("Calculation complete!")
print(result.tail(10))
