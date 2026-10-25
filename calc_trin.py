import csv
import json
import urllib.request
from datetime import datetime


def fetch_data():
    url = (
        "http://82.push2.eastmoney.com/api/qt/clist/get?"
        "pn=1&pz=6000&po=1&np=1&ut=bd1d9ddb04089700cf9c27f6f7426281&"
        "fltt=2&invt=2&fid=f3&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23&fields=f3,f6"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    response = urllib.request.urlopen(req)
    data = json.loads(response.read().decode("utf-8"))

    items = data["data"]["diff"]
    up_count = 0
    down_count = 0
    up_amount = 0.0
    down_amount = 0.0

    for item in items:
        if "f3" not in item or "f6" not in item or item["f3"] == "-":
            continue
        pct = float(item["f3"])
        amt = float(item["f6"])
        if pct > 0:
            up_count += 1
            up_amount += amt
        elif pct < 0:
            down_count += 1
            down_amount += amt

    if down_count > 0 and down_amount > 0:
        trin = (up_count / down_count) / (up_amount / down_amount)
        return trin
    return 0.0


def main():
    trin = fetch_data()
    date_str = datetime.now().strftime("%Y-%m-%d")
    with open("trin_result.csv", "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "trin_raw"])
        writer.writerow([date_str, round(trin, 4)])


if __name__ == "__main__":
    main()
