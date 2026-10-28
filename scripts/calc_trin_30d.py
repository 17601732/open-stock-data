# ruff: noqa
import csv
import os
import struct
from collections import defaultdict

def parse_tdx_day(file_path):
    records = []
    try:
        with open(file_path, 'rb') as f:
            content = f.read()
            for i in range(0, len(content), 32):
                if i + 32 > len(content):
                    break
                row = struct.unpack('<IIIIIfII', content[i:i+32])
                date_str = str(row[0])
                # 严格判断：必须是8位数字，且在 20000101 到 20301231 之间
                if len(date_str) != 8 or not date_str.isdigit() or not (20000000 < int(date_str) < 20300000):
                    continue
                
                close = row[4] / 100.0
                volume = row[6]
                
                records.append({
                    "date": date_str,
                    "close": close,
                    "volume": volume
                })
    except Exception as e:
        pass
    return records

def main():
    print("开始搜索并解析通达信本地数据...")
    
    base_dir = "tdx_data"
    if not os.path.exists(base_dir):
        print(f"❌ 错误: 找不到 {base_dir} 目录！")
        return

    # 核心：自动遍历 tdx_data 下的所有文件夹，把所有 .day 文件找出来
    day_files = []
    for root, dirs, files in os.walk(base_dir):
        for file in files:
            if file.lower().endswith('.day'):
                day_files.append(os.path.join(root, file))

    print(f"✅ 共搜索到 {len(day_files)} 个 .day 文件。")
    if len(day_files) == 0:
        print("⚠️ 没找到任何 .day 文件，请检查压缩包内容！")
        return
        
    # 打印前 3 个文件的路径，方便我们核对
    print("前 3 个文件的路径示例：")
    for p in day_files[:3]:
        print(f"  - {p}")

    daily_data = defaultdict(lambda: {"up_count": 0, "down_count": 0, "up_vol": 0.0, "down_vol": 0.0})
    
    file_count = 0
    for file_path in day_files:
        filename = os.path.basename(file_path)
        
        # 过滤北交所 (通常代码前缀为 bj 或 8/4 开头)
        if filename.startswith(("bj", "sh8", "sz8", "sz4")):
            continue
            
        stock_data = parse_tdx_day(file_path)
        
        # 过滤新股/次新股 (如果历史数据少于 60 天，直接忽略)
        if len(stock_data) < 60:
            continue
            
        for i in range(1, len(stock_data)):
            today = stock_data[i]
            yesterday = stock_data[i-1]
            
            date = today["date"]
            close = today["close"]
            prev_close = yesterday["close"]
            volume = today["volume"]
            
            if close > prev_close:
                daily_data[date]["up_count"] += 1
                daily_data[date]["up_vol"] += volume
            elif close < prev_close:
                daily_data[date]["down_count"] += 1
                daily_data[date]["down_vol"] += volume
                
        file_count += 1

    print(f"✅ 成功解析了 {file_count} 只有效股票的数据。")

    if file_count == 0:
        print("⚠️ 股票数据为空！可能是解压的文件名或格式不对。")
        return

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
            
    print(f"✅ 成功生成 {out_file}，包含近 {len(results)} 个交易日的数据。")
    for r in results[-5:]:
        print(r)

if __name__ == "__main__":
    main()
