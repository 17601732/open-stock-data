# ruff: noqa
import csv
import json
import urllib.request
from datetime import datetime


def fetch_sina_data(symbol):
    url = f"https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_=/CN_MarketDataService.getKLineData?symbol={symbol}&scale=240&ma=no&datalen=180"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    response = urllib.request.urlopen(req)
    text = response.read().decode("utf-8")
    # 新浪返回的是 jsonp 格式，需要截取 JSON 部分
    start_idx = text.find("(")
    end_idx = text.rfind(")")
    json_str = text[start_idx + 1 : end_idx]
    return json.loads(json_str)


def main():
    print("Fetching historical data from Sina Finance...")
    
    try:
        # 获取上证指数和深证成指近 180 天数据
        sh_data = fetch_sina_data("sh000001")
        sz_data = fetch_sina_data("sz399001")
    except Exception as e:
        print(f"Fetch failed: {e}")
        return

    # 按日期合并数据
    daily_stats = {}
    
    for item in sh_data + sz_data:
        date = item["day"]
        open_p = float(item["open"])
        close_p = float(item["close"])
        vol = float(item["volume"])
        
        if date not in daily_stats:
            daily_stats[date] = {"up_count": 0, "down_count": 0, "up_vol": 0.0, "down_vol": 0.0}
            
        if close_p > open_p:
            daily_stats[date]["up_count"] += 1
            daily_stats[date]["up_vol"] += vol
        elif close_p < open_p:
            daily_stats[date]["down_count"] += 1
            daily_stats[date]["down_vol"] += vol
        else:
            daily_stats[date]["up_count"] += 1
            daily_stats[date]["up_vol"] += vol / 2
            daily_stats[date]["down_count"] += 1
            daily_stats[date]["down_vol"] += vol / 2

    dates = sorted(daily_stats.keys())
    results = []
    
    # 计算每日原始 TRIN
    for d in dates:
        s = daily_stats[d]
        if s["down_count"] > 0 and s["down_vol"] > 0 and s["up_vol"] > 0:
            trin = (s["up_count"] / s["down_count"]) / (s["up_vol"] / s["down_vol"])
        else:
            trin = 1.0
        results.append([d, trin])

    # 计算 21 天平滑
    smooth_results = []
    for i in range(len(results)):
        window = results[max(0, i - 20) : i + 1]
        avg_trin = sum(r[1] for r in window) / len(window)
        smooth_results.append([results[i][0], round(results[i][1], 4), round(avg_trin, 4)])

    # 保存
    with open("trin_result.csv", "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "trin_raw", "trin_smooth_21"])
        for row in smooth_results:
            writer.writerow(row)
            
    print("Success! Check trin_result.csv")


if __name__ == "__main__":
    main()
