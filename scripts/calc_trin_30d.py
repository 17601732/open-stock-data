# ruff: noqa
import csv
import os
import time
import urllib.request
import json
import math
from datetime import datetime, timedelta
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

def get_latest_trading_date():
    """智能推算最新的交易日（如果盘中执行，则取前一交易日）"""
    now = datetime.now()
    # 假设用北京时间做判断，GitHub服务器是UTC，这里加8小时
    now_bj = now + timedelta(hours=8)
    
    # 如果当前时间不到 15:30，算作没收盘，往前推一天
    if now_bj.hour < 15 or (now_bj.hour == 15 and now_bj.minute < 30):
        target = now_bj - timedelta(days=1)
    else:
        target = now_bj
        
    # 如果是周末，退回到周五
    while target.weekday() >= 5: # 5是周六，6是周日
        target -= timedelta(days=1)
        
    return target.strftime("%Y%m%d")

def fetch_netease_data(date_str):
    """通过网易历史接口获取指定日期的全市场数据"""
    print(f"开始抓取 {date_str} 的全市场数据...")
    up_count, down_count = 0, 0
    up_vol, down_vol = 0.0, 0.0
    
    # 网易接口分页参数
    page = 0
    while True:
        url = f"http://quotes.money.163.com/hs/service/diyrank.php?host=http%3A%2F%2Fquotes.money.163.com%2Fhs%2Fservice%2Fdiyrank.php&page={page}&query=STYPE%3AEQA&fields=NO%2CSYMBOL%2CNAME%2CPRICE%2CPERCENT%2CVOLUME&sort=SYMBOL&order=asc&count=100&type=query&date={date_str}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        
        try:
            res = urllib.request.urlopen(req).read().decode("gbk")
            data = json.loads(res)
            stocks = data.get("list", [])
            
            if not stocks:
                if page == 0:
                    print("❌ 未获取到任何数据，可能这天休市或接口异常。")
                    return None
                break
                
            for item in stocks:
                name = item.get("NAME", "")
                pct = item.get("PERCENT", 0)
                vol = item.get("VOLUME", 0)
                
                # 过滤ST、退市、N/C新股
                if "ST" in name.upper() or "退" in name or name.startswith(("N", "C")):
                    continue
                    
                if pct > 0:
                    up_count += 1
                    up_vol += vol
                elif pct < 0:
                    down_count += 1
                    down_vol += vol
                    
            page += 1
            time.sleep(0.3) # 防封延迟
            
        except Exception as e:
            print(f"抓取第 {page} 页失败: {e}")
            break
            
    if down_count == 0 or down_vol == 0:
        print("数据异常（可能全市场停牌或休市），取消写入。")
        return None
        
    trin = (up_count / down_count) / (up_vol / down_vol)
    return up_count, down_count, up_vol, down_vol, trin

def update_csv_and_plot(date_str, up_count, down_count, up_vol, down_vol, trin_raw):
    csv_file = "trin_2020_now.csv"
    rows = []
    
    # 将 YYYYMMDD 转换为 YYYY-MM-DD
    formatted_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
    
    # 1. 读取现有数据
    if os.path.exists(csv_file):
        with open(csv_file, "r", newline="") as f:
            reader = csv.reader(f)
            header = next(reader)
            rows = list(reader)

    # 2. 检查该日期是否已存在，存在则覆盖，不存在则追加
    date_exists = False
    for row in rows:
        if row[0] == formatted_date:
            row[1] = str(up_count)
            row[2] = str(down_count)
            row[3] = str(int(up_vol))
            row[4] = str(int(down_vol))
            row[5] = str(round(trin_raw, 4))
            date_exists = True
            print(f"已覆盖历史数据: {formatted_date}")
            break
            
    if not date_exists:
        rows.append([
            formatted_date, str(up_count), str(down_count), 
            str(int(up_vol)), str(int(down_vol)), str(round(trin_raw, 4)), "0"
        ])
        print(f"已追加新数据: {formatted_date}")

    # 3. 按日期排序（防止插入历史日期后顺序错乱）
    rows.sort(key=lambda x: x[0])

    # 4. 重新计算 MA30
    trin_history = []
    for row in rows:
        trin_val = float(row[5])
        trin_history.append(trin_val)
        ma30 = sum(trin_history[-30:]) / min(30, len(trin_history))
        row[6] = str(round(ma30, 4))

    # 5. 写回 CSV
    with open(csv_file, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "up_count", "down_count", "up_vol", "down_vol", "trin_raw", "trin_ma30"])
        writer.writerows(rows)

    print(f"✅ CSV 已更新。{formatted_date} TRIN: {trin_raw:.4f}")

    # 6. 重新绘制图表
    print("🎨 正在重绘折线图...")
    dates = [datetime.strptime(r[0], "%Y-%m-%d") for r in rows]
    trin_raw_list = [float(r[5]) for r in rows]
    trin_ma30_list = [float(r[6]) for r in rows]

    plt.figure(figsize=(15, 7))
    plt.plot(dates, trin_raw_list, label='TRIN Raw', color='#1f77b4', alpha=0.3, linewidth=1)
    plt.plot(dates, trin_ma30_list, label='TRIN MA30', color='#d62728', linewidth=1.5)
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
    print("✅ 图表已更新。")

if __name__ == "__main__":
    # 从环境变量获取日期，如果是通过 schedule 触发的，这里是空字符串
    target_date = os.environ.get("TARGET_DATE", "").strip()
    
    if not target_date:
        # 没输入日期，智能推算最新交易日
        target_date = get_latest_trading_date()
        print(f"未指定日期，系统自动选择最新交易日: {target_date}")
    else:
        # 校验格式
        if len(target_date) != 8 or not target_date.isdigit():
            print("❌ 日期格式错误，请使用 YYYYMMDD 格式，如 20260921")
            exit(1)
        print(f"收到指定日期指令: {target_date}")
        
    result = fetch_netease_data(target_date)
    if result:
        u_c, d_c, u_v, d_v, t_raw = result
        update_csv_and_plot(target_date, u_c, d_c, u_v, d_v, t_raw)
