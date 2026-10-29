import pandas as pd
from pytdx.exhq import TdxExHq_API
import time
import os

# 通达信扩展行情服务器列表（期权需要连扩展服务器）
EXH_SERVERS = [
    ('119.97.185.5', 7727),
    ('124.74.236.94', 7721),
    ('47.103.48.45', 7727),
    ('59.175.238.38', 7727)
]

def get_api():
    """获取并连接可用的扩展行情API"""
    api = TdxExHq_API()
    for ip, port in EXH_SERVERS:
        try:
            if api.connect(ip, port):
                print(f"成功连接到服务器 {ip}:{port}")
                return api
        except Exception:
            continue
    raise Exception("所有扩展行情服务器连接失败")

def main():
    api = get_api()
    
    # 市场代码：8 代表上交所期权，这里获取 50ETF (510050) 的期权列表
    # 注意：通达信扩展行情接口中，50ETF期权的标的代码通常是 510050
    print("正在获取 50ETF 期权合约列表...")
    contract_list = api.get_instrument_quote_list(8, 0, 10000)
    
    if not contract_list:
        print("未获取到合约列表")
        return

    # 筛选出标的为 510050 的期权
    # 通达信返回的字段中，通常 name 包含 '50ETF'，或者根据内部代码规则过滤
    options = [c for c in contract_list if '50ETF' in c.get('name', '')]
    print(f"共找到 {len(options)} 个 50ETF 期权合约")

    all_data = []
    
    # 遍历每个合约获取历史 K 线
    for idx, opt in enumerate(options):
        code = opt['code']
        name = opt['name']
        print(f"[{idx+1}/{len(options)}] 正在拉取: {name} ({code})")
        
        try:
            # 获取该合约的历史日线（最多拉取 1000 天）
            bars = api.get_instrument_bars(8, code, 9, 0, 1000) # 9 代表日线
            if not bars:
                continue
            
            for bar in bars:
                # 解析日期、成交量、持仓量
                all_data.append({
                    'date': bar['datetime'][:10], # 提取 YYYY-MM-DD
                    'name': name,
                    'code': code,
                    'volume': bar['vol'],
                    'open_interest': bar.get('amount', 0) # pytdx 有时将持仓量放在 amount 或专有字段，这里按实际字段微调
                })
        except Exception as e:
            print(f"拉取 {code} 失败: {e}")
            time.sleep(1) # 防止请求过快被踢

    api.disconnect()

    if not all_data:
        print("未获取到任何数据")
        return

    print("正在计算 PCR...")
    df = pd.DataFrame(all_data)
    
    # 区分认购（购）和认沽（沽）
    df['is_call'] = df['name'].apply(lambda x: 1 if '购' in str(x) else 0)
    df['is_put'] = df['name'].apply(lambda x: 1 if '沽' in str(x) else 0)

    # 按日期汇总
    daily_stats = df.groupby('date').agg({
        'volume': lambda x: x[df.loc[x.index, 'is_call'] == 1].sum(), # Call Vol
        'open_interest': lambda x: x[df.loc[x.index, 'is_call'] == 1].sum()  # Call OI
    }).rename(columns={'volume': 'call_vol', 'open_interest': 'call_oi'})

    put_stats = df.groupby('date').agg({
        'volume': lambda x: x[df.loc[x.index, 'is_put'] == 1].sum(), # Put Vol
        'open_interest': lambda x: x[df.loc[x.index, 'is_put'] == 1].sum()  # Put OI
    }).rename(columns={'volume': 'put_vol', 'open_interest': 'put_oi'})

    # 合并数据
    result = pd.concat([daily_stats, put_stats], axis=1).fillna(0)
    
    # 计算 PCR (Put / Call)
    result['pcr_volume'] = (result['put_vol'] / result['call_vol']).round(4)
    result['pcr_oi'] = (result['put_oi'] / result['call_oi']).round(4)
    
    result = result.reset_index()
    
    # 格式整理
    output_df = result[['date', 'pcr_volume', 'pcr_oi', 'put_vol', 'call_vol', 'put_oi', 'call_oi']]
    output_df.columns = ['date', 'pcr_vol', 'pcr_oi', 'put_vol', 'call_vol', 'put_oi', 'call_oi']
    
    # 保存为 CSV
    output_file = 'option_pcr_50etf.csv'
    output_df.to_csv(output_file, index=False)
    print(f"计算完成，已保存至 {output_file}")

if __name__ == "__main__":
    main()
