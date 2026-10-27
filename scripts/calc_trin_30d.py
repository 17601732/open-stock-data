# ruff: noqa
import csv
import os
import struct
from collections import defaultdict
from datetime import datetime

def parse_tdx_day(file_path):
    """解析通达信单只股票的日线文件 (.day)"""
    records = []
    try:
        with open(file_path, 'rb') as f:
            content = f.read()
            # 每天的数据占 32 个字节
            for i in range(0, len(content), 32):
                if i + 32 > len(content):
                    break
                # < 代表小端，I 代表无符号整数(4字节)，f 代表浮点数(4字节)
                # 格式: 日期, 开盘, 最高, 最低, 收盘, 成交额, 成交量, 保留
                row = struct.unpack('<IIIIIfII', content[i:i+32])
                date_str = str(row[0])
                # 如果日期不是 8 位数，说明数据异常或结束了
                if len(date_str) != 8:
                    continue
                
                # 通达信的价格是乘以 100 后的整数，需要除以 100 还原
                close = row[4] / 100.0
                # 成交量 (股)
                volume = row[6]
                
                records.append({
                    "date": date_str,
                    "close": close,
                    "volume": volume
                })
    except Exception as e:
        print(f"读取文件 {file_path} 出错: {e}")
    return records

def main():
    print("开始解析通达信本地数据...")
    
    # 通达信解压后的路径通常长这样：vipdoc/sh/lday/*.day 和 vipdoc/sz/lday/*.day
    # 请根据你 zip 解压后的实际目录结构调整，这里假设解压在 tdx_data 目录下
    base_dir = "tdx_data"
    folders = [
        os.path.join(base_dir, "vipdoc", "sh", "lday"),
        os.path.join(base_dir, "vipdoc", "sz", "lday")
    ]
    
    # 用于按日期聚合: daily_data['20231026'] = {'up': 0, 'down': 0, 'up_vol': 0, 'down_vol': 0}
    daily_data = defaultdict(lambda: {"up_count": 0, "down_count": 0, "up_vol": 0.0, "down_vol": 0.0})
    
    file_count = 0
    for folder in folders:
        if not os.path.exists(folder):
            print(f"警告: 找不到目录 {folder}")
            continue
            
        for filename in os.listdir(folder):
            # 只要日线文件 .day
            if not filename.endswith(".day"):
                continue
                
            file_path = os.path.join(folder, filename)
            # 解析出单只股票的所有历史数据
            stock_data = parse_tdx_day(file_path)
            
            # 过滤北交所 (代码 8 开头或 4 开头，通达信通常不包含在 sh/sz 目录，但以防万一)
            if filename.startswith(("sh8", "sz8", "sz4")):
                continue
                
            # 过滤新股 (如果上市天数少于 30 天，说明是次新股/新股)
            if len(stock_data) < 30:
                continue
                
            # 遍历这只股票的每一天
            for i in range(1, len(stock_data)):
                today = stock_data[i]
                yesterday = stock_data[i-1]
                
                date = today["date"]
                close = today["close"]
                prev_close = yesterday["close"]
                volume = today["volume"]
                
                # 判断涨跌
                if close > prev_close:
                    daily_data[date]["up_count"] += 1
                    daily_data[date]["up_vol"] += volume
                elif close < prev_close:
                    daily_data[date]["down_count"] += 1
                    daily_data[date]["down_vol"] += volume
                    
            file_count += 1
            if file_count % 500 == 0:
                print(f"已解析 {file_count} 只股票...")

    print(f"解析完成！共处理 {file_count} 只股票。")

    # 接下来计算 TRIN 并写入 CSV
    # 我们只取最近 60 个交易日的数据（足够算 30 日均值了）
    sorted_dates = sorted(list(daily_data.keys()))[-60:]
    
    results = []
    for date in sorted_dates:
        d = daily_data[date]
        up_c = d["up_count"]
        down_c = d["down_count"]
        up_v = d["up_vol"]
        down_v = d["down_vol"]
        
        if down_c > 0 and down_v > 0 and up_v > 0:
            trin = (up_c / down_c) / (up_v / down_v)
        else:
            trin = 0.0
            
        results.append({
            "date": date,
            "up_count": up_c,
            "down_count": down_c,
            "trin_raw": round(trin, 4)
        })

    # 写入 CSV
    out_file = "trin_30d.csv"
    with open(out_file, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "up_count", "down_count", "trin_raw"])
        for row in results:
            writer.writerow([row["date"], row["up_count"], row["down_count"], row["trin_raw"]])
            
    print(f"成功生成 {out_file}，包含近 {len(results)} 个交易日的数据。")
    # 打印最后 5 天看看效果
    for r in results[-5:]:
        print(r)

if __name__ == "__main__":
    main()
