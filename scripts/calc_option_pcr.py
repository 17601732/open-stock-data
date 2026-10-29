import akshare as ak
import pandas as pd
import datetime
import os
import time


def main():
    output_file = 'option_pcr_50etf.csv'
    columns = [
        'date', 'pcr_vol', 'pcr_oi',
        'put_vol', 'call_vol', 'put_oi', 'call_oi'
    ]

    existing_dates = set()
    if os.path.exists(output_file):
        try:
            old_df = pd.read_csv(output_file)
            if not old_df.empty:
                existing_dates = set(old_df['date'].tolist())
        except Exception as e:
            print(f"Error reading existing file: {e}")

    end_date = datetime.datetime.now()

    if len(existing_dates) > 0:
        start_date = end_date - datetime.timedelta(days=30)
    else:
        start_date = end_date - datetime.timedelta(days=365 * 2)

    current_date = start_date
    new_records = []

    print("Starting data fetch...")
    while current_date <= end_date:
        if current_date.weekday() < 5:
            date_str = current_date.strftime("%Y%m%d")
            fmt_date = current_date.strftime("%Y-%m-%d")

            if fmt_date not in existing_dates:
                print(f"Fetching {fmt_date}...")
                try:
                    df = ak.option_sse_daily(trade_date=date_str)
                    if not df.empty:
                        df = df[df['合约名称'].str.contains('50ETF')]

                        call_df = df[df['合约名称'].str.contains('购')]
                        put_df = df[df['合约名称'].str.contains('沽')]

                        vol_col = '成交量' if '成交量' in df.columns else '成交量(手)'
                        oi_col = '持仓量' if '持仓量' in df.columns else '持仓量(手)'

                        if vol_col in df.columns and oi_col in df.columns:
                            c_vol = call_df[vol_col].sum()
                            p_vol = put_df[vol_col].sum()
                            c_oi = call_df[oi_col].sum()
                            p_oi = put_df[oi_col].sum()

                            if c_vol > 0 and c_oi > 0:
                                new_records.append({
                                    'date': fmt_date,
                                    'pcr_vol': round(p_vol / c_vol, 4),
                                    'pcr_oi': round(p_oi / c_oi, 4),
                                    'put_vol': int(p_vol),
                                    'call_vol': int(c_vol),
                                    'put_oi': int(p_oi),
                                    'call_oi': int(c_oi)
                                })
                except Exception as e:
                    print(f"Skip {fmt_date}: {e}")
                time.sleep(0.5)
        current_date += datetime.timedelta(days=1)

    if new_records:
        new_df = pd.DataFrame(new_records)
        if os.path.exists(output_file):
            old_df = pd.read_csv(output_file)
            final_df = pd.concat([old_df, new_df]).drop_duplicates(
                subset=['date']
            ).sort_values('date')
        else:
            final_df = new_df.sort_values('date')

        final_df.to_csv(output_file, index=False)
        print(f"Data updated! Total {len(final_df)} records.")
    else:
        print("No new data fetched.")
        if not os.path.exists(output_file):
            pd.DataFrame(columns=columns).to_csv(output_file, index=False)


if __name__ == "__main__":
    main()
