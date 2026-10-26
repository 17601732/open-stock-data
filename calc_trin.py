# ruff: noqa
import urllib.request
import pandas as pd
from datetime import datetime

def main():
    print("1. 开始下载全市场底层数据库 (约 10-20MB)...")
    # 直接获取原项目每天更新的全市场个股历史数据
    url = "https://github.com/irachex/open-stock-data/releases/latest/download/cn_bars.parquet"
    
    try:
        urllib.request.urlretrieve(url, "cn_bars.parquet")
        print("下载成功！正在读取数据...")
    except Exception as e:
        print(f"下载失败，请检查网络: {e}")
        return

    # 读取 Parquet 文件
    df = pd.read_parquet("cn_bars.parquet")
    
    # 统一日期格式，并截取最近 6 个月的数据
    df['date'] = pd.to_datetime(df['date'])
    # 动态计算半年前的日期
    start_date = pd.Timestamp.now() - pd.Timedelta(days=180)
    df = df[df['date'] >= start_date]
    
    print(f"2. 数据加载完毕，共 {len(df)} 条个股日线记录。开始计算 TRIN...")
    
    # 计算单日全市场 TRIN
    def calc_daily_trin(group):
        # 判定涨跌：如果你有 pct_chg 字段可用 pct_chg > 0，否则用 close > open
        if 'pct_chg' in group.columns:
            up = group[group['pct_chg'] > 0]
            down = group[group['pct_chg'] < 0]
        else:
            up = group[group['close'] > group['open']]
            down = group[group['close'] < group['open']]
        
        # 防除零保护
        if len(down) == 0 or down['amount'].sum() == 0 or up['amount'].sum() == 0:
            return 1.0
            
        # 真实全市场 TRIN 公式 (使用成交额 amount 计算，更贴近现代市场)
        trin = (len(up) / len(down)) / (up['amount'].sum() / down['amount'].sum())
        return trin

    # 按天分组并应用公式
    result = df.groupby('date').apply(calc_daily_trin).reset_index()
    result.columns = ['date', 'trin_raw']
    
    # 计算 21 天平滑均线
    result['trin_smooth_21'] = result['trin_raw'].rolling(window=21, min_periods=1).mean()
    
    # 格式化日期
    result['date'] = result['date'].dt.strftime('%Y-%m-%d')
    
    # 保存结果
    result.to_csv("trin_result.csv", index=False)
    print("3. 计算完成！已保存至 trin_result.csv")
    print(result.tail(10))

if __name__ == "__main__":
    main()
