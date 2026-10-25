# ruff: noqa
import csv
import json
import urllib.request


def main():
    # 获取上证指数最近 180 个交易日的真实行情
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh000001,day,,,180,qfq"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    response = urllib.request.urlopen(req)
    data = json.loads(response.read().decode("utf-8"))

    klines = data["data"]["sh000001"]["day"]
    results = []

    # 采用滚动 21 天的量价关系计算 TRIN
    for i in range(len(klines)):
        window = klines[max(0, i - 20) : i + 1]
        up_days = 0
        down_days = 0
        up_vol = 0.0
        down_vol = 0.0

        for w in window:
            open_price = float(w[1])
            close_price = float(w[2])
            volume = float(w[5])

            if close_price > open_price:
                up_days += 1
                up_vol += volume
            elif close_price < open_price:
                down_days += 1
                down_vol += volume

        # 核心修复：只要有任何一项为 0，直接返回 1.0，防止报错
        if down_days == 0 or down_vol == 0.0 or up_vol == 0.0:
            trin = 1.0
        else:
            trin = (up_days / down_days) / (up_vol / down_vol)

        results.append([klines[i][0], round(trin, 4)])

    # 保存为 CSV
    with open("trin_result.csv", "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "trin_smooth_21d"])
        for row in results:
            writer.writerow(row)
    print("Success!")


if __name__ == "__main__":
    main()
