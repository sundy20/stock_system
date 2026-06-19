#!/usr/bin/env python3
"""
tushare 财务数据下载：单季度净利润同比增长率 + 营收同比增长率
- Token 从环境变量 TUSHARE_TOKEN 读取
- 股票列表优先从本地缓存读取
"""
import os, time, sqlite3, pandas as pd
from datetime import datetime
import tushare as ts

TOKEN = os.getenv('TUSHARE_TOKEN')
if not TOKEN: raise RuntimeError("请先执行: export TUSHARE_TOKEN='你的token'")
ts.set_token(TOKEN)
pro = ts.pro_api()

DB_PATH = 'stocks_2y.db'
CALL_PER_MIN = 50
SLEEP_SEC = 60 / CALL_PER_MIN

def init_db(conn):
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute('''CREATE TABLE IF NOT EXISTS financial (
                                                             code TEXT, name TEXT,
                                                             pub_date TEXT, stat_date TEXT,
                                                             net_profit_yoy REAL, revenue_yoy REAL,
                                                             PRIMARY KEY (code, stat_date))''')
    conn.commit()

def get_stock_codes(conn):
    """从本地 stock_basic 表获取股票列表（排除沪深300）"""
    df = pd.read_sql("SELECT code, name FROM stock_basic WHERE code != 'sh.000300'", conn)
    if df.empty:
        raise RuntimeError("stock_basic 表为空，请先运行日线下载脚本生成缓存")
    return list(zip(df['code'], df['name']))

if __name__ == '__main__':
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    codes = get_stock_codes(conn)
    print(f"下载财务数据，共 {len(codes)} 只股票，限速 {CALL_PER_MIN} 次/分钟")

    success, last_time = 0, time.time()
    for code, name in codes:
        ts_code = code[3:] + '.SH' if code.startswith('sh.') else code[3:] + '.SZ'
        try:
            df = pro.fina_indicator(ts_code=ts_code, period='20240101', end_date='20251231',
                                    fields='end_date,ann_date,q_profit_yoy,q_gr_yoy')
            if df.empty: continue
            df = df.rename(columns={
                'end_date': 'stat_date', 'ann_date': 'pub_date',
                'q_profit_yoy': 'net_profit_yoy', 'q_gr_yoy': 'revenue_yoy'
            })
            df['stat_date'] = pd.to_datetime(df['stat_date']).dt.strftime('%Y-%m-%d')
            df['pub_date'] = pd.to_datetime(df['pub_date']).dt.strftime('%Y-%m-%d')
            df['code'], df['name'] = code, name
            for _, row in df.iterrows():
                conn.execute('''INSERT OR REPLACE INTO financial
                                (code, name, pub_date, stat_date, net_profit_yoy, revenue_yoy)
                                VALUES (?,?,?,?,?,?)''',
                             (row['code'], row['name'], row['pub_date'], row['stat_date'],
                              row['net_profit_yoy'], row['revenue_yoy']))
            conn.commit()
            success += 1
            if success % 50 == 0: print(f"  已完成 {success}/{len(codes)} 只")
        except Exception: pass

        elapsed = time.time() - last_time
        if elapsed < SLEEP_SEC: time.sleep(SLEEP_SEC - elapsed)
        last_time = time.time()

    conn.close()
    print(f"财务数据下载完成，成功 {success}/{len(codes)} 只")