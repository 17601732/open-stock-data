# ruff: noqa
import csv
import os
import time
import urllib.request
import re
from datetime import datetime

def get_all_cn_codes():
    print("正在获取全市场股票代码...")
    url = "http://stock.gtimg.cn/data/index.php?appn=rank&t=rankash/chr&p=1&o=0&l=6000&v=list_data"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    res = urllib.request.urlopen(req).read().decode("gbk")
    
    match = re.search(r'"(.*?)"', res)
    if not match:
        return []
    
    all_codes = match.group(1).split(",")
    # 剔除北交所 (bj) 和 沪深B股等特殊代码
    valid_codes = [c for c in all_codes if c.startswith("sh") or c.startswith("sz")]
    # 剔除科创板688等（如果你不需要的话，但通常全市场TRIN包含科创）
    return valid_codes

def main():
    codes = get_all_cn_codes()
    print(f"共获取到 {len(codes)} 只沪深股票代码。")
    
    up_count, down_count = 0, 0
    up_vol, down_vol = 0.0, 0.0

    # 每次请求 80 只
    batch_size = 80
    for i in range(0, len(codes), batch_size):
        batch = codes[i:i+batch_size]
        query = ",".join(batch)
        url = f"https://qt.gtimg.cn/q={query}"
        
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            res = urllib.request.urlopen(req).read().decode("gbk")
            
            # 腾讯返回的每一行用 ; 隔开
            for line in res.strip().split(";"):
                if not line.strip(): continue
                parts = line.split("~")
                if len(parts) < 40: continue
                
                name = parts[1]
                # 过滤 ST 股、退市股
                if "ST" in name.upper() or "退" in name:
                    continue
                # 过滤 N (首日上市) 和 C (上市次日到第五日，不设涨跌幅)
                if name.startswith(("N", "C", "U", "W")):
                    continue
                
                pct = float(parts[32]) if parts[32] else 0.0
                # 注意：腾讯接口的第 36 个字段是成交额(万)，这里为了精准，用成交量(手)
                vol = float(parts[6]) if parts[6] else 0.0
                
                if pct > 0:
                    up_count += 1
                    up_vol += vol
                elif pct < 0:
                    down_count += 1
                    down_vol += vol
        except Exception as e:
            print(f"批次 {i//batch_size + 1} 请求失败: {e}")
            
        # 休息 3 秒防封
        time.sleep(3)
        if (i // batch_size) % 10 == 0:
            print(f"已处理 {i}/{len(codes)} 只股票...")

    if down_count > 0 and down_vol > 0:
        trin = (up_count / down_count) / (up_vol / down_vol)
    else:
        trin = 0.0

    date_str = datetime.now().strftime("%Y-%m-%d")
    
    # 写入 CSV
    file_exists = os.path.isfile("trin_30d.csv")
    with open("trin_30d.csv", "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["date", "up_count", "down_count", "up_vol", "down_vol", "trin_raw"])
        writer.writerow([
            date_str, up_count, down_count, 
            int(up_vol), int(down_vol), round(trin, 4)
        ])
    
    print(f"完成！上涨家数: {up_count}, 下跌家数: {down_count}")
    print(f"TRIN: {trin:.4f}")

if __name__ == "__main__":
    main()
