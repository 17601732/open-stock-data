# ruff: noqa
import ast
import csv
import time
import urllib.request
from datetime import datetime


def main():
    up_count = 0
    down_count = 0
    up_amount = 0.0
    down_amount = 0.0

    print("Fetching today's market data from Sina...")
    
    # 遍历全市场 A 股 (新浪接口每页 100 条)
    for page in range(1, 60):
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
                    pct = float(item["changepercent"])
                    amt = float(item["amount"])
                    
                    if pct > 0:
                        up_count += 1
                        up_amount += amt
                    elif pct < 0:
                        down_count += 1
                        down_amount += amt
                except:
                    continue
        except Exception as e:
            print(f"Error on page {page}: {e}")
            break
            
        time.sleep(0.1) # 微小延迟防封

    if down_count > 0 and down_amount > 0 and up_amount > 0:
        trin = (up_count / down_count) / (up_amount / down_amount)
    else:
        trin = 0.0

    date_str = datetime.now().strftime("%Y-%m-%d")
    
    print(f"Up Count: {up_count}, Down Count: {down_count}")
    print(f"Up Amount: {up_amount/1e8:.2f} 亿, Down Amount: {down_amount/1e8:.2f} 亿")
    print(f"TRIN: {trin:.4f}")

    # 将所有的中间变量全部写入 CSV
    with open("trin_result.csv", "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "up_count", "down_count", "up_amount_yi", "down_amount_yi", "trin_raw"])
        writer.writerow([
            date_str, 
            up_count, 
            down_count, 
            round(up_amount / 1e8, 2), 
            round(down_amount / 1e8, 2), 
            round(trin, 4)
        ])


if __name__ == "__main__":
    main()
