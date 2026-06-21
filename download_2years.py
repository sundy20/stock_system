#!/usr/bin/env python3
"""
下载全部沪深主板非ST股票+沪深300指数日线数据（起始于2024-01-01）
- 2线程并发，请求间隔0.2秒，重试2次
- 全量覆盖，INSERT OR REPLACE 确保前复权数据最新
- 失败自动补：中断后无需重试，下周运行时会自动补充
"""
import baostock as bs
import sqlite3
import pandas as pd
from datetime import datetime
import time
import random
from concurrent.futures import ThreadPoolExecutor, as_completed

DB_PATH = 'stocks_2y.db'
THREAD_NUM = 2                     # 线程数
REQUEST_DELAY = 0.2                # 请求间隔（秒）
MAX_RETRY = 2                      # 最大重试次数
BENCH_CODE = 'sh.000300'           # 沪深300基准代码

START_DATE = '2024-01-01'
END_DATE = datetime.now().strftime('%Y-%m-%d')

def init_worker():
    """线程初始化：独立登录"""
    bs.login()

def get_mainboard_codes():
    """获取沪深主板非ST股票列表，并加入沪深300指数"""
    rs = bs.query_stock_basic(code_name="")
    stocks = []
    while rs.next():
        stocks.append(rs.get_row_data())
    df = pd.DataFrame(stocks, columns=rs.fields)
    mask = (df['type'] == '1') & \
           (df['code'].str.match(r'^(sh\.60|sz\.00)')) & \
           (~df['code_name'].str.contains('ST'))
    code_list = list(zip(df[mask]['code'], df[mask]['code_name']))
    code_list.append((BENCH_CODE, '沪深300'))
    return code_list

def init_db(conn):
    """初始化数据库，开启WAL模式及索引"""
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA cache_size = -20000;")
    conn.execute('''CREATE TABLE IF NOT EXISTS daily (
                                                         code TEXT, date TEXT, name TEXT,
                                                         open REAL, high REAL, low REAL,
                                                         close REAL, volume REAL,
                                                         PRIMARY KEY (code, date))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS stock_basic (
                                                               code TEXT PRIMARY KEY,
                                                               name TEXT
                    )''')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_daily_code_date ON daily(code, date);")
    conn.commit()

def download_single_stock(args):
    """下载单只股票日线，带重试机制"""
    code, name = args
    for retry in range(MAX_RETRY + 1):
        try:
            # 重试前随机等待，避免多线程同时冲击
            time.sleep(random.uniform(0.2, 0.5))
            rs = bs.query_history_k_data_plus(
                code, "date,open,high,low,close,volume",
                start_date=START_DATE, end_date=END_DATE,
                frequency="d", adjustflag="2"      # 前复权
            )
            if rs.error_code != '0':
                time.sleep(1 * (retry + 1))
                continue
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
            if not rows:
                return None, code
            df = pd.DataFrame(rows, columns=rs.fields)
            df.dropna(subset=['date', 'close'], inplace=True)
            if df.empty:
                return None, code
            # 转换数值列
            for col in ['open', 'high', 'low', 'close', 'volume']:
                df[col] = pd.to_numeric(df[col], errors='coerce')
            df['code'] = code
            df['name'] = name
            time.sleep(REQUEST_DELAY)
            return df, None
        except Exception as e:
            time.sleep(1 * (retry + 1))
            continue
    return None, code

def batch_write_safe(conn, df_list):
    """通过临时表无冲突批量写入"""
    if not df_list:
        return
    all_df = pd.concat(df_list, ignore_index=True)
    all_df.to_sql('daily_temp', conn, if_exists='replace', index=False, method='multi')
    conn.execute('''
        INSERT OR REPLACE INTO daily (code, date, name, open, high, low, close, volume)
        SELECT code, date, name, open, high, low, close, volume FROM daily_temp
    ''')
    conn.execute("DROP TABLE IF EXISTS daily_temp")
    conn.commit()

if __name__ == '__main__':
    print(f"全量下载数据：{START_DATE} 至 {END_DATE}（{THREAD_NUM}线程，间隔{REQUEST_DELAY}秒）")
    bs.login()
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    codes = get_mainboard_codes()
    # 更新股票基础信息表
    basic_df = pd.DataFrame(codes, columns=['code', 'name'])
    basic_df.to_sql('stock_basic', conn, if_exists='replace', index=False)
    conn.commit()
    print(f"共 {len(codes)} 只标的，{THREAD_NUM} 线程并发下载...")
    bs.logout()                           # 主线程登出，子线程各自登录

    tasks = [(code, name) for code, name in codes]
    success = 0
    failed = []
    batch_buffer = []
    BATCH_SIZE = 50

    with ThreadPoolExecutor(max_workers=THREAD_NUM, initializer=init_worker) as executor:
        futures = {executor.submit(download_single_stock, task): task for task in tasks}
        for i, future in enumerate(as_completed(futures)):
            result, fail_code = future.result()
            if result is not None and not result.empty:
                batch_buffer.append(result)
                success += 1
                if len(batch_buffer) >= BATCH_SIZE:
                    batch_write_safe(conn, batch_buffer)
                    batch_buffer = []
                    print(f"  已完成 {success}/{len(codes)} 只，已写入数据库")
            else:
                failed.append(fail_code)
            if (i+1) % 200 == 0:
                print(f"  已处理 {i+1}/{len(codes)}")

    if batch_buffer:
        batch_write_safe(conn, batch_buffer)

    conn.close()
    print(f"\n下载完成，成功 {success}/{len(codes)} 只")
    if failed:
        print(f"失败 {len(failed)} 只，将在下次运行时自动补充")
    print(f"数据库保存至 {DB_PATH}")