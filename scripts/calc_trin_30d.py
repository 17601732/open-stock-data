# ruff: noqa
import csv
import os
import time
import urllib.request
import ast
from datetime import datetime
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

def fetch_today_data():
    print("开始抓取今日全市场快照...")
    up_count, down_count = 0, 0
    up_vol, down_vol = 0.0, 0.0

    # 遍历新浪接口拉取全市场
    for page in range(1, 80):
        url = f"http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData?page={page}&num=100&sort=symbol&asc=1&node=hs_a&_s_r_a=page"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            res = urllib.request.urlopen(req).read().decode("gbk")
            if not res or res == "null":
                break
            data = ast.literal_eval(res)
            if not data:
                break
            
            for item in data:
                try:
                    name = item["name"]
                    # 剔除ST、退市、N/C开头新股
                    if "ST" in name.upper() or "退" in name or name.startswith(("N", "C")):
                        continue
                    
                    pct = float(item["changepercent"])
                    vol = float(item["volume"])  # 单位：股
                    
                    if pct > 0:
                        up_count += 1
                        up_vol += vol
                    elif pct < 0:
                        down_count += 1
                        down_vol += vol
                except:
                    continue
        except Exception as e:
            print(f"抓取第 {page} 页失败: {e}")
            break
        time.sleep(0.5)

    if down_count > 0 and down_vol > 0 and up_vol > 0:
        trin = (up_count / down_count) / (up_vol / down_vol)
    else:
        trin = 0.0

    return up_count, down_count, up_vol, down_vol, trin

def update_csv_and_plot(date_str, up_count, down_count, up_vol, down_vol, trin_raw):
    csv_file = "trin_2020_now.csv"
    rows = []
    
    # 1. 读取现有的历史数据
    if os.path.exists(csv_file):
        with open(csv_file, "r", newline="") as f:
            reader = csv.reader(f)
            header = next(reader)
            rows = list(reader)

    # 2. 检查今天是否已经存在（如果存在则更新，不存在则追加）
    today_exists = False
    for row in rows:
        if row[0] == date_str:
            # 更新今天的数据
            row[1] = str(up_count)
            row[2] = str(down_count)
            row[3] = str(int(up_vol))
            row[4] = str(int(down_vol))
            row[5] = str(round(trin_raw, 4))
            today_exists = True
            break
            
    if not today_exists:
        rows.append([
            date_str, str(up_count), str(down_count), 
            str(int(up_vol)), str(int(down_vol)), str(round(trin_raw, 4)), "0"
        ])

    # 3. 重新计算 MA30 平滑线
    trin_history = []
    for row in rows:
        trin_val = float(row[5])
        trin_history.append(trin_val)
        ma30 = sum(trin_history[-30:]) / min(30, len(trin_history))
        row[6] = str(round(ma30, 4))

    # 4. 写回 CSV
    with open(csv_file, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "up_count", "down_count", "up_vol", "down_vol", "trin_raw", "trin_ma30"])
        writer.writerows(rows)

    print(f"✅ CSV 已更新。今日 TRIN: {trin_raw:.4f}")

    # 5. 重新绘制图表
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
    # 为了测试，如果今天是周末或节假日休市，直接退出
    now = datetime.now()
    if now.weekday() >= 5:
        print("今天是周末，A股休市，跳过计算。")
        exit(0)
        
    date_str = now.strftime("%Y-%m-%d")
    u_c, d_c, u_v, d_v, t_raw = fetch_today_data()
    
    if u_c > 0 and d_c > 0:
        update_csv_and_plot(date_str, u_c, d_c, u_v, d_v, t_raw)
    else:
        print("未获取到有效数据（可能休市或接口错误），跳过写入。")
