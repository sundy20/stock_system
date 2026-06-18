#!/usr/bin/env python3
"""
下载净利润同比增长率（YOYNI）+ 股票名称
- 数据表 financial 包含 code, name, pub_date, stat_date, net_profit_yoy, revenue_yoy
- 当前仅 net_profit_yoy 有值，revenue_yoy 保留为空
"""
import baostock as bs
import sqlite3
import pandas as pd
from datetime import datetime
import time

DB_PATH = 'stocks_2y.db'
REQUEST_DELAY = 0.2
REFRESH_EVERY = 200

CURRENT_YEAR = datetime.now().year
YEARS = [CURRENT_YEAR - 1, CURRENT_YEAR]   # [2025, 2026]

def get_mainboard_codes():
    rs = bs.query_stock_basic(code_name="")
    stocks = []
    while rs.next():
        stocks.append(rs.get_row_data())
    df = pd.DataFrame(stocks, columns=rs.fields)
    mask = (df['type'] == '1') & \
           (df['code'].str.match(r'^(sh\.60|sz\.00)')) & \
           (~df['code_name'].str.contains('ST'))
    # 返回代码和名称
    return list(zip(df[mask]['code'], df[mask]['code_name']))

def init_db(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS financial (
                                                             code TEXT,
                                                             name TEXT,
                                                             pub_date TEXT,
                                                             stat_date TEXT,
                                                             net_profit_yoy REAL,
                                                             revenue_yoy REAL,
                                                             PRIMARY KEY (code, stat_date)
        )''')
    conn.commit()

def download_financials(code, name, conn):
    """下载单只股票的净利润增长率，并写入名称"""
    profit_data = {}
    pub_dates = {}
    for year in YEARS:
        rs = bs.query_growth_data(code, year=year, quarter=4)
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
                    pub_dates[sd] = row[pub_date_col]
    if not profit_data:
        return False

    inserted = 0
    for stat_date, net_yoy in profit_data.items():
        conn.execute(
            'INSERT OR REPLACE INTO financial (code, name, pub_date, stat_date, net_profit_yoy, revenue_yoy) '
            'VALUES (?,?,?,?,?,?)',
            (code, name, pub_dates.get(stat_date, ''), stat_date, net_yoy, None)
        )
        inserted += 1
    conn.commit()
    return inserted > 0

print("登录 baostock ...")
bs.login()
codes = get_mainboard_codes()
print(f"需要下载 {len(codes)} 只股票的净利润增长率数据（含名称）")

conn = sqlite3.connect(DB_PATH)
init_db(conn)

success = 0
for i, (code, name) in enumerate(codes):
    if i > 0 and i % REFRESH_EVERY == 0:
        bs.logout()
        time.sleep(1)
        bs.login()
        print(f"  已处理 {i}/{len(codes)}，刷新登录")
    if download_financials(code, name, conn):
        success += 1
        if success % 50 == 0:
            print(f"  已完成 {success} 只...")
    time.sleep(REQUEST_DELAY)

conn.close()
bs.logout()
print(f"财务数据下载完成，成功 {success}/{len(codes)} 只")