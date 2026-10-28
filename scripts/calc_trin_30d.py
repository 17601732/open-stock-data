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
                
                # 格式化日期为 YYYY-MM-DD
                formatted_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
                
                close = row[4] / 100.0
                volume = row[6]
                
                records.append({
                    "date": formatted_date,
                    "close": close,
                    "volume": volume
                })
    except Exception:
        pass
    return records

def main():
    print("开始解析全历史A股数据...")
    
    base_dir = "tdx_data"
    if not os.path.exists(base_dir):
        print(f"❌ 错误: 找不到 {base_dir} 目录！")
        return

    day_files = []
    for root, dirs, files in os.walk(base_dir):
        for file in files:
            if file.lower().endswith('.day'):
                day_files.append(os.path.join(root, file))

    daily_data = defaultdict(lambda: {"up_count": 0, "down_count": 0, "up_vol": 0.0, "down_vol": 0.0})
    valid_stock_count = 0
    
    for file_path in day_files:
        filename = os.path.basename(file_path).lower()
        match = re.search(r'(\d{6})\.day$', filename)
        if not match:
            continue
        code = match.group(1)
        
        # 过滤规则：仅限沪深A股
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

    # 获取全部日期并排序（去掉了限制 60 天的切片）
    sorted_dates = sorted(list(daily_data.keys()))
    
    results = []
    trin_history = [] # 用于计算 30 日平滑均线
    
    for date in sorted_dates:
        d = daily_data[date]
        up_c = d["up_count"]
        down_c = d["down_count"]
        up_v = d["up_vol"]
        down_v = d["down_vol"]
        
        if down_c > 0 and down_v > 0 and up_v > 0:
            trin = (up_c / down_c) / (up_v / down_v)
        else:
            trin = 1.0 # 无数据时给中性值
            
        trin_history.append(trin)
        
        # 计算 30 日平滑均线 (MA30)
        if len(trin_history) >= 30:
            ma30 = sum(trin_history[-30:]) / 30.0
        else:
            ma30 = sum(trin_history) / len(trin_history)
            
        results.append({
            "date": date,
            "up_count": up_c,
            "down_count": down_c,
            "up_vol": up_v,
            "down_vol": down_v,
            "trin_raw": round(trin, 4),
            "trin_ma30": round(ma30, 4)
        })

    # 保存文件，改名为全历史数据
    out_file = "trin_history.csv"
    with open(out_file, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "up_count", "down_count", "up_vol", "down_vol", "trin_raw", "trin_ma30"])
        for row in results:
            writer.writerow([row["date"], row["up_count"], row["down_count"], row["up_vol"], row["down_vol"], row["trin_raw"], row["trin_ma30"]])
            
    print(f"🎉 成功生成 {out_file}，包含全部 {len(results)} 个交易日的历史数据。")

if __name__ == "__main__":
    main()
