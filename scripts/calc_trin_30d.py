# ruff: noqa
import csv
import os
import struct
from collections import defaultdict

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
                # 解析 32 字节：日期, 开盘, 最高, 最低, 收盘, 成交额, 成交量, 保留
                row = struct.unpack('<IIIIIfII', content[i:i+32])
                date_str = str(row[0])
                if len(date_str) != 8:
                    continue
                
                # 通达信价格是乘以 100 后存储的整数
                close = row[4] / 100.0
                volume = row[6] # 成交量（股）
                
                records.append({
                    "date": date_str,
                    "close": close,
                    "volume": volume
                })
    except Exception as e:
        pass
    return records

def main():
    print("开始解析通达信本地数据...")
    
    # 通达信解压后的路径
    base_dir = "tdx_data"
    folders = [
        os.path.join(base_dir, "vipdoc", "sh", "lday"),
        os.path.join(base_dir, "vipdoc", "sz", "lday")
    ]
    
    # 按日期聚合
    daily_data = defaultdict(lambda: {"up_count": 0, "down_count": 0, "up_vol": 0.0, "down_vol": 0.0})
    
    file_count = 0
    for folder in folders:
        if not os.path.exists(folder):
            print(f"警告: 找不到目录 {folder}")
            continue
            
        for filename in os.listdir(folder):
            if not filename.endswith(".day"):
                continue
                
            file_path = os.path.join(folder, filename)
            stock_data = parse_tdx_day(file_path)
            
            # 过滤北交所 (通常代码前缀为 bj 或 8/4 开头，此处仅为防万一)
            if filename.startswith(("bj", "sh8", "sz8", "sz4")):
                continue
                
            # 过滤新股/次新股 (如果历史数据少于 60 天，直接忽略)
            if len(stock_data) < 60:
                continue
                
            # 遍历这只股票的每天数据
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
            if file_count % 1000 == 0:
                print(f"已解析 {file_count} 只股票...")

    print(f"解析完成！共处理 {file_count} 只股票。")

    # 取最近 60 个交易日
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
            "up_vol": up_v,
            "down_vol": down_v,
            "trin_raw": round(trin, 4)
        })

    # 写入 CSV
    out_file = "trin_30d.csv"
    with open(out_file, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "up_count", "down_count", "up_vol", "down_vol", "trin_raw"])
        for row in results:
            writer.writerow([row["date"], row["up_count"], row["down_count"], row["up_vol"], row["down_vol"], row["trin_raw"]])
            
    print(f"成功生成 {out_file}，包含近 {len(results)} 个交易日的数据。")
    # 打印最后 5 天看看效果
    for r in results[-5:]:
        print(r)

if __name__ == "__main__":
    main()
