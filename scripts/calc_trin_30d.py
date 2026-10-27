# ruff: noqa
import csv
import os
import time
import urllib.request
from datetime import datetime


def is_valid_stock(name: str, code: str) -> bool:
    """过滤ST股与新股（名称含N、C）"""
    if "ST" in name.upper():
        return False
    if name.startswith(("N", "C")):
        return False
    return True


def fetch_and_calc():
    # 1. 获取沪深A股代码列表（用于分批）
    # 这里假设你已通过其他方式获取了代码列表，或者可以硬编码一个示例列表
    # 实际使用时，建议先用一个独立的步骤获取全市场代码
    print("加载股票代码列表...")
    # 示例：从本地文件读取，或通过接口获取
    # 此处用占位符代替，实际需替换为真实代码列表
    all_codes = []  # 需填充真实代码，如 ["sh600000", "sz000001", ...]

    up_count, down_count = 0, 0
    up_amount, down_amount = 0.0, 0.0

    # 2. 分批请求，每批80只，间隔3秒
    batch_size = 80
    for i in range(0, len(all_codes), batch_size):
        batch = all_codes[i:i + batch_size]
        query = ",".join(batch)
        url = f"https://qt.gtimg.cn/q={query}"
        
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            res = urllib.request.urlopen(req).read().decode("gbk")
            if not res or "v_pv_none" in res:
                continue

            # 3. 解析腾讯返回的~分隔文本
            for line in res.strip().split(";"):
                if not line.strip(): continue
                try:
                    parts = line.split("~")
                    if len(parts) < 10: continue
                    
                    code_with_prefix = parts[0].split("=")[0].replace("v_", "")
                    name = parts[1]
                    pct = float(parts[3])   # 涨跌幅
                    volume = float(parts[6]) # 成交量（手）
                    
                    # 过滤ST与新股
                    if not is_valid_stock(name, code_with_prefix):
                        continue

                    if pct > 0:
                        up_count += 1
                        up_amount += volume
                    elif pct < 0:
                        down_count += 1
                        down_amount += volume
                except (ValueError, IndexError):
                    continue
        except Exception as e:
            print(f"批次 {i // batch_size + 1} 请求失败: {e}")
        finally:
            time.sleep(3)  # 每批之间休息3秒，防止被封

    # 4. 计算TRIN
    if down_count > 0 and down_amount > 0:
        trin = (up_count / down_count) / (up_amount / down_amount)
    else:
        trin = 0.0

    date_str = datetime.now().strftime("%Y-%m-%d")
    file_exists = os.path.isfile("trin_30d.csv")
    with open("trin_30d.csv", "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["date", "up_count", "down_count", "up_amount", "down_amount", "trin_raw"])
        writer.writerow([date_str, up_count, down_count, round(up_amount, 2), round(down_amount, 2), round(trin, 4)])
    print(f"计算完成！日期: {date_str}, TRIN: {trin:.4f}")


if __name__ == "__main__":
    fetch_and_calc()
