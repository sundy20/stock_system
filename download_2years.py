#!/usr/bin/env python3
"""下载全部沪深主板非ST股票最近2年日线数据（含名称）"""
import baostock as bs
import sqlite3
import pandas as pd
from datetime import datetime, timedelta
import time

DB_PATH = 'stocks_2y.db'
END_DATE = datetime.now().strftime('%Y-%m-%d')
START_DATE = (datetime.now() - timedelta(days=730)).strftime('%Y-%m-%d')
REQUEST_DELAY = 0.2
REFRESH_EVERY = 200

def get_mainboard_codes():
    rs = bs.query_stock_basic(code_name="")
    stocks = []
    while rs.next():
        stocks.append(rs.get_row_data())
    df = pd.DataFrame(stocks, columns=rs.fields)
    mask = (df['type'] == '1') & \
           (df['code'].str.match(r'^(sh\.60|sz\.00)')) & \
           (~df['code_name'].str.contains('ST'))
    return list(zip(df[mask]['code'], df[mask]['code_name']))

def init_db(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS daily (
                                                         code TEXT, date TEXT, name TEXT,
                                                         open REAL, high REAL, low REAL,
                                                         close REAL, volume REAL,
                                                         PRIMARY KEY (code, date))''')
    conn.commit()

def download_stock(code, name, conn):
    rs = bs.query_history_k_data_plus(
        code, "date,open,high,low,close,volume",
        start_date=START_DATE, end_date=END_DATE,
        frequency="d", adjustflag="2"
    )
    if rs.error_code != '0':
        return False
    rows = []
    while rs.next():
        rows.append(rs.get_row_data())
    if not rows:
        return False
    df = pd.DataFrame(rows, columns=rs.fields)
    df.dropna(subset=['date', 'close'], inplace=True)
    for col in ['open', 'high', 'low', 'close', 'volume']:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    for _, row in df.iterrows():
        conn.execute(
            'INSERT OR REPLACE INTO daily (code,date,name,open,high,low,close,volume) '
            'VALUES (?,?,?,?,?,?,?,?)',
            (code, row['date'], name, row['open'], row['high'],
             row['low'], row['close'], row['volume'])
        )
    conn.commit()
    return True

print("登录 baostock ...")
bs.login()
codes = get_mainboard_codes()
print(f"需要下载 {len(codes)} 只股票，数据范围：{START_DATE} 至 {END_DATE}")

conn = sqlite3.connect(DB_PATH)
init_db(conn)

success = 0
for i, (code, name) in enumerate(codes):
    if i > 0 and i % REFRESH_EVERY == 0:
        bs.logout()
        time.sleep(1)
        bs.login()
        print(f"  已处理 {i}/{len(codes)}，刷新登录")
    if download_stock(code, name, conn):
        success += 1
        if success % 50 == 0:
            print(f"  已完成 {success} 只...")
    time.sleep(REQUEST_DELAY)

conn.close()
bs.logout()
print(f"下载完成，成功 {success}/{len(codes)} 只，数据库保存至 {DB_PATH}")