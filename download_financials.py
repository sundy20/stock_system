#!/usr/bin/env python3
"""
baostock 财务数据下载：单季度净利润同比增长率
- 数据源为正式季报/年报，不含业绩预告
- 支持并发下载、自动重试、临时表无冲突写入
- 获取最近2年所有季度数据，发布后10天生效（消除未来函数）
"""
import baostock as bs
import sqlite3
import pandas as pd
from datetime import datetime
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

DB_PATH = 'stocks_2y.db'
THREAD_NUM = 2
REQUEST_DELAY = 0.2
MAX_RETRY = 2

CURRENT_YEAR = datetime.now().year
YEARS = [CURRENT_YEAR - 1, CURRENT_YEAR]
QUARTERS = [1, 2, 3, 4]


def init_worker():
    bs.login()


def get_mainboard_codes(conn):
    df = pd.read_sql("SELECT code, name FROM stock_basic WHERE code != 'sh.000300'", conn)
    return list(zip(df['code'], df['name']))


def init_db(conn):
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute('''CREATE TABLE IF NOT EXISTS financial (
                                                             code TEXT, name TEXT,
                                                             pub_date TEXT, stat_date TEXT,
                                                             net_profit_yoy REAL, revenue_yoy REAL,
                                                             PRIMARY KEY (code, stat_date)
        )''')
    conn.commit()


def download_single_financial(args):
    code, name = args
    for retry in range(MAX_RETRY + 1):
        try:
            profit_data = {}
            pub_dates = {}
            for year in YEARS:
                for quarter in QUARTERS:
                    rs = bs.query_growth_data(code, year=year, quarter=quarter)
                    if rs.error_code != '0':
                        continue
                    rows = []
                    while rs.next():
                        rows.append(rs.get_row_data())
                    if not rows:
                        continue
                    fields = rs.fields
                    df = pd.DataFrame(rows, columns=fields)
                    profit_col = next((col for col in fields if col.lower() == 'yoyni'), None)
                    if not profit_col:
                        continue
                    pub_date_col = next((col for col in fields if 'pubdate' in col.lower()), None)
                    stat_date_col = next((col for col in fields if 'statdate' in col.lower()), None)
                    df[profit_col] = pd.to_numeric(df[profit_col], errors='coerce')
                    for _, row in df.iterrows():
                        if pd.notna(row[profit_col]):
                            sd = str(pd.to_datetime(row[stat_date_col]).date())
                            profit_data[sd] = row[profit_col]
                            if pub_date_col:
                                pub_dates[sd] = str(pd.to_datetime(row[pub_date_col]).date())
            time.sleep(REQUEST_DELAY)
            if not profit_data:
                return None, code

            records = []
            for stat_date, net_yoy in profit_data.items():
                records.append({
                    'code': code, 'name': name,
                    'pub_date': pub_dates.get(stat_date, ''),
                    'stat_date': stat_date,
                    'net_profit_yoy': net_yoy,
                    'revenue_yoy': None
                })
            return pd.DataFrame(records), None
        except Exception as e:
            time.sleep(0.5)
            continue
    return None, code


def batch_write_safe(conn, df_list):
    if not df_list:
        return
    all_df = pd.concat(df_list, ignore_index=True)
    all_df.to_sql('financial_temp', conn, if_exists='replace', index=False, method='multi')
    conn.execute('''
        INSERT OR REPLACE INTO financial (code, name, pub_date, stat_date, net_profit_yoy, revenue_yoy)
        SELECT code, name, pub_date, stat_date, net_profit_yoy, revenue_yoy FROM financial_temp
    ''')
    conn.execute("DROP TABLE IF EXISTS financial_temp")
    conn.commit()


if __name__ == '__main__':
    print("登录 baostock ...")
    bs.login()
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    codes = get_mainboard_codes(conn)
    print(f"下载财务数据，共 {len(codes)} 只股票，{THREAD_NUM} 线程并发，全季度数据")
    bs.logout()

    success = 0
    failed = []
    batch_buffer = []
    BATCH_SIZE = 50

    with ThreadPoolExecutor(max_workers=THREAD_NUM, initializer=init_worker) as executor:
        futures = {executor.submit(download_single_financial, task): task for task in codes}
        for i, future in enumerate(as_completed(futures)):
            result, fail_code = future.result()
            if result is not None and not result.empty:
                batch_buffer.append(result)
                success += 1
                if len(batch_buffer) >= BATCH_SIZE:
                    batch_write_safe(conn, batch_buffer)
                    batch_buffer = []
                    print(f"  已完成 {success}/{len(codes)} 只")
            else:
                failed.append(fail_code)
            if (i+1) % 200 == 0:
                print(f"  已处理 {i+1}/{len(codes)}")

    if batch_buffer:
        batch_write_safe(conn, batch_buffer)

    conn.commit()
    conn.close()
    print(f"财务数据下载完成，成功 {success}/{len(codes)} 只")
    if failed:
        print(f"失败 {len(failed)} 只（示例: {failed[:10]}）")