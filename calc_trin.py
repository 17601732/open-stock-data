# ruff: noqa
import akshare as ak
import pandas as pd
import concurrent.futures
from datetime import datetime, timedelta

def fetch_stock_data(code):
    try:
        # 抓取单只股票近 120 个交易日的日线
        df = ak.stock_zh_a_hist(symbol=code, period="daily", start_date="20260601", end_date="20261025", adjust="")
        if df.empty:
            return None
        df = df[['日期', '涨跌幅', '成交额']]
        return df
    except:
        return None

def main():
    print("1. 获取全市场股票列表...")
    spot_df = ak.stock_zh_a_spot_em()
    # 按成交额降序排列，取前 1000 只最活跃的股票（覆盖 80% 以上成交量，完美代表市场）
    spot_df = spot_df.sort_values(by='成交额', ascending=False)
    codes = spot_df['代码'].head(1000).tolist()

    print("2. 并发下载近半年历史数据 (预计耗时 1 分钟)...")
    all_data = []
    
    # 开启 10 个线程并发下载
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        results = executor.map(fetch_stock_data, codes)
        for res in results:
            if res is not None:
                all_data.append(res)
    
    if not all_data:
        print("下载失败，未获取到数据")
        return
        
    print("3. 合并数据并计算真实 TRIN...")
    df_all = pd.concat(all_data, ignore_index=True)
    df_all['日期'] = pd.to_datetime(df_all['日期'])
    
    def calc_daily_trin(group):
        up = group[group['涨跌幅'] > 0]
        down = group[group['涨跌幅'] < 0]
        
        if len(down) == 0 or down['成交额'].sum() == 0 or up['成交额'].sum() == 0:
            return 1.0
            
        # 真实全市场 TRIN 公式
        trin = (len(up) / len(down)) / (up['成交额'].sum() / down['成交额'].sum())
        return trin

    result = df_all.groupby('日期').apply(calc_daily_trin).reset_index()
    result.columns = ['date', 'trin_raw']
    
    # 计算 21 天平滑
    result['trin_smooth_21'] = result['trin_raw'].rolling(window=21, min_periods=1).mean()
    
    result.to_csv("trin_result.csv", index=False)
    print("Success! 已生成 trin_result.csv")
    print(result.tail(10))

if __name__ == "__main__":
    main()
