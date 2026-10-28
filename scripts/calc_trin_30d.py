# ruff: noqa
import csv
import os
import re
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
                if len(date_str) != 8 or not date_str.isdigit():
                    continue
                
                close = row[4] / 100.0
                volume = row[6]
                
                records.append({
                    "date": date_str,
                    "close": close,
                    "volume": volume
                })
    except Exception:
        pass
    return records

def main():
    print("开始智能解析A股数据...")
    
    base_dir = "tdx_data"
    if not os.path.exists(base_dir):
        print(f"❌ 错误: 找不到 {base_dir} 目录！")
        return

    day_files = []
    for root, dirs, files in os.walk(base_dir):
        for file in files:
            if file.lower().endswith('.day'):
                day_files.append(os.path.join(root, file))

    print(f"📂 共搜索到 {len(day_files)} 个 .day 文件。")
    if len(day_files) > 0:
        print(f"🔍 随便看几个文件的真实名字: {[os.path.basename(f) for f in day_files[:5]]}")

    daily_data = defaultdict(lambda: {"up_count": 0, "down_count": 0, "up_vol": 0.0, "down_vol": 0.0})
    
    valid_stock_count = 0
    for file_path in day_files:
        filename = os.path.basename(file_path).lower()
        
        # 智能提取 6 位数字代码 (兼容 sh600000.day 或 600000.day)
        match = re.search(r'(\d{6})\.day$', filename)
        if not match:
            continue
        code = match.group(1)
        
        # 核心白名单：只保留真正的沪深 A 股代码
        is_sh_a = code.startswith(("600", "601", "603", "605", "688"))
        is_sz_a = code.startswith(("000", "001", "002", "003", "300", "301"))
        
        if not (is_sh_a or is_sz_a):
            continue
            
        stock_data = parse_tdx_day(file_path)
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
                
        valid_stock_count += 1

    print(f"✅ 过滤后，真正参与计算的A股数量：{valid_stock_count} 只。")

    if valid_stock_count == 0:
        print("⚠️ 没有任何股票进入计算池，请检查上面的文件名示例！")
        return

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

    out_file = "trin_30d.csv"
    with open(out_file, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "up_count", "down_count", "up_vol", "down_vol", "trin_raw"])
        for row in results:
            writer.writerow([row["date"], row["up_count"], row["down_count"], row["up_vol"], row["down_vol"], row["trin_raw"]])
            
    print(f"🎉 成功生成 {out_file}，包含近 {len(results)} 个交易日的数据。")

if __name__ == "__main__":
    main()
