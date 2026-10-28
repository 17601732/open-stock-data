# ruff: noqa
import csv
import os
import re
import struct
import zipfile
from collections import defaultdict
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime

def parse_tdx_content(content):
    """解析单只通达信文件的内容，返回按时间排序的记录列表"""
    records = []
    for i in range(0, len(content), 32):
        if i + 32 > len(content):
            break
        row = struct.unpack('<IIIIIfII', content[i:i+32])
        date_str = str(row[0])
        if len(date_str) != 8 or not date_str.isdigit():
            continue
        
        formatted_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
        close = row[4] / 100.0
        volume = row[6]
        
        records.append({
            "date": formatted_date,
            "close": close,
            "volume": volume
        })
    return records

def main():
    zip_path = "hsjday.1.zip"
    if not os.path.exists(zip_path):
        print(f"❌ 找不到压缩包 {zip_path}")
        return

    print("=== 开始扫描压缩包 ===")
    # daily_data[日期] = {"up_count": 0, "down_count": 0, "up_vol": 0.0, "down_vol": 0.0}
    daily_data = defaultdict(lambda: {"up_count": 0, "down_count": 0, "up_vol": 0.0, "down_vol": 0.0})
    valid_stock_count = 0
    
    with zipfile.ZipFile(zip_path, 'r') as z:
        for file_info in z.infolist():
            filename = file_info.filename.lower()
            if not filename.endswith('.day'):
                continue
                
            match = re.search(r'(\d{6})\.day$', filename)
            if not match:
                continue
            code = match.group(1)
            
            # 白名单过滤 A 股
            if not (code.startswith(("600", "601", "603", "605", "688")) or code.startswith(("000", "001", "002", "003", "300", "301"))):
                continue
                
            content = z.read(file_info.filename)
            records = parse_tdx_content(content)
            
            if len(records) < 5:
                continue
                
            # 核心：正确的逐日比较逻辑
            for i in range(1, len(records)):
                today = records[i]
                yesterday = records[i-1]
                
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

    print(f"✅ 参与计算的A股数量：{valid_stock_count} 只。")

    # 获取所有日期并排序
    all_sorted_dates = sorted(list(daily_data.keys()))
    
    # 过滤 2020-01-01 之后的数据
    target_dates = [d for d in all_sorted_dates if d >= "2020-01-01"]
    print(f"📅 2020年以后共有 {len(target_dates)} 个交易日。")

    results = []
    trin_history = []
    
    for date in target_dates:
        d = daily_data[date]
        up_c = d["up_count"]
        down_c = d["down_count"]
        up_v = d["up_vol"]
        down_v = d["down_vol"]
        
        if down_c > 0 and down_v > 0 and up_v > 0:
            trin = (up_c / down_c) / (up_v / down_v)
        else:
            trin = 1.0
        trin_history.append(trin)
        
        ma30 = sum(trin_history[-30:]) / min(30, len(trin_history))
        
        results.append([date, up_c, down_c, up_v, down_v, round(trin, 4), round(ma30, 4)])

    # 生成 CSV
    out_file = "trin_2020_now.csv"
    with open(out_file, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "up_count", "down_count", "up_vol", "down_vol", "trin_raw", "trin_ma30"])
        writer.writerows(results)
    print(f"🎉 成功生成 {out_file}。")

    # 绘制折线图
    print("🎨 开始绘制图表...")
    dates = [datetime.strptime(r[0], "%Y-%m-%d") for r in results]
    trin_raw = [r[5] for r in results]
    trin_ma30 = [r[6] for r in results]

    plt.figure(figsize=(15, 7))
    plt.plot(dates, trin_raw, label='TRIN Raw', color='#1f77b4', alpha=0.3, linewidth=1)
    plt.plot(dates, trin_ma30, label='TRIN MA30', color='#d62728', linewidth=1.5)
    plt.axhline(y=1.0, color='black', linestyle='--', linewidth=1, label='Neutral (1.0)')

    plt.ylim(0.2, 2.5)
    plt.gca().xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
    plt.gca().xaxis.set_major_locator(mdates.MonthLocator(interval=6))
    plt.gcf().autofmt_xdate()

    plt.title("A-Share Arms Index (TRIN) 2020 - Present", fontsize=16)
    plt.xlabel("Date", fontsize=12)
    plt.ylabel("TRIN Value", fontsize=12)
    plt.legend(loc='upper right')
    plt.grid(True, alpha=0.3)
    plt.tight_layout()

    plt.savefig("trin_chart.png", dpi=150)
    print("🎉 成功生成图表 trin_chart.png。")

if __name__ == "__main__":
    main()
