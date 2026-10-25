# ruff: noqa
import akshare as ak
import pandas as pd
from datetime import datetime, timedelta

def main():
    end_date = datetime.now().strftime('%Y%m%d')
    start_date = (datetime.now() - timedelta(days=180)).strftime('%Y%m%d')
    
    try:
        # 获取上证指数历史数据
        df_sh = ak.stock_zh_index_daily_em(symbol="sh000001")
        # 获取深证成指历史数据
        df_sz = ak.stock_zh_index_daily_em(symbol="sz399001")
        
        # 合并数据并筛选近半年
        df = pd.concat([df_sh, df_sz])
        df['date'] = pd.to_datetime(df['date'])
        df = df[df['date'] >= pd.to_datetime(start_date)]
        
        # 计算涨跌和成交额
        df['change'] = df['close'] - df['open']
        df['amount'] = df['amount']
        
        # 按日期分组计算每日的近似 TRIN
        result = df.groupby('date').apply(lambda x: (len(x[x['change'] > 0]) / len(x[x['change'] < 0])) / (x[x['change'] > 0]['amount'].sum() / x[x['change'] < 0]['amount'].sum()) if len(x[x['change'] < 0]) > 0 else 1.0).reset_index()
        result.columns = ['date', 'trin_raw']
        
        # 计算 21 天平滑
        result['trin_smooth_21'] = result['trin_raw'].rolling(window=21, min_periods=1).mean()
        
        result.to_csv("trin_result.csv", index=False)
        print("数据生成成功")
        print(result.tail())
        
    except Exception as e:
        print(f"发生错误: {e}")

if __name__ == "__main__":
    main()
