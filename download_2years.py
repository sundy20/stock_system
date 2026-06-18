#!/usr/bin/env python3
"""
下载全部沪深主板非ST股票+沪深300指数最近2年日线数据
最终版：增量更新、4线程安全并发、批量无冲突写入、失败重试、WAL性能优化
"""
import baostock as bs
import sqlite3
import pandas as pd
from datetime import datetime, timedelta
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

DB_PATH = 'stocks_2y.db'
THREAD_NUM = 4               # 并发线程数，安全阈值2-6
REQUEST_DELAY = 0.15         # 单线程请求间隔
MAX_RETRY = 2                # 单只股票最大重试次数
BENCH_CODE = 'sh.000300'     # 沪深300基准代码

def init_worker():
    """线程独立登录，会话隔离"""
    bs.login()

def get_mainboard_codes():
    """获取主板非ST股票列表 + 沪深300指数"""
    rs = bs.query_stock_basic(code_name="")
    stocks = []
    while rs.next():
        stocks.append(rs.get_row_data())
    df = pd.DataFrame(stocks, columns=rs.fields)
    mask = (df['type'] == '1') & \
           (df['code'].str.match(r'^(sh\.60|sz\.00)')) & \
           (~df['code_name'].str.contains('ST'))
    code_list = list(zip(df[mask]['code'], df[mask]['code_name']))
    # 加入沪深300指数
    code_list.append((BENCH_CODE, '沪深300'))
    return code_list

def init_db(conn):
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

def get_incremental_date(conn):
    try:
        cursor = conn.execute("SELECT MAX(date) FROM daily")
        max_date = cursor.fetchone()[0]
        if max_date:
            start = (datetime.strptime(max_date, '%Y-%m-%d') + timedelta(days=1)).strftime('%Y-%m-%d')
            return start
    except:
        pass
    return (datetime.now() - timedelta(days=730)).strftime('%Y-%m-%d')

def download_single_stock(args):
    code, name, start_date, end_date = args
    for retry in range(MAX_RETRY + 1):
        try:
            rs = bs.query_history_k_data_plus(
                code, "date,open,high,low,close,volume",
                start_date=start_date, end_date=end_date,
                frequency="d", adjustflag="2"
            )
            if rs.error_code != '0':
                time.sleep(0.5 * (retry + 1))
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
            for col in ['open', 'high', 'low', 'close', 'volume']:
                df[col] = pd.to_numeric(df[col], errors='coerce')
            df['code'] = code
            df['name'] = name
            time.sleep(REQUEST_DELAY)
            return df, None
        except Exception as e:
            time.sleep(0.5)
            continue
    return None, code

def batch_write_safe(conn, df_list):
    """
    安全批量写入：临时表过渡 + INSERT OR REPLACE
    彻底解决主键重复冲突，支持任意重复运行
    """
    if not df_list:
        return
    all_df = pd.concat(df_list, ignore_index=True)
    # 写入临时表
    all_df.to_sql('daily_temp', conn, if_exists='replace', index=False, method='multi')
    # 合并到正式表，冲突则覆盖
    conn.execute('''
        INSERT OR REPLACE INTO daily (code, date, name, open, high, low, close, volume)
        SELECT code, date, name, open, high, low, close, volume FROM daily_temp
    ''')
    conn.execute("DROP TABLE IF EXISTS daily_temp")
    conn.commit()

# ==================== 主流程 ====================
if __name__ == '__main__':
    print("登录 baostock ...")
    bs.login()

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    codes = get_mainboard_codes()
    basic_df = pd.DataFrame(codes, columns=['code', 'name'])
    basic_df.to_sql('stock_basic', conn, if_exists='replace', index=False)
    conn.commit()

    start_date = get_incremental_date(conn)
    end_date = datetime.now().strftime('%Y-%m-%d')

    if start_date >= end_date:
        print("数据已是最新，无需更新")
        conn.close()
        bs.logout()
        exit()

    print(f"下载范围：{start_date} 至 {end_date}，共 {len(codes)} 只标的，{THREAD_NUM} 线程并发")
    bs.logout()  # 主线程登出，子线程独立登录，避免会话冲突

    tasks = [(code, name, start_date, end_date) for code, name in codes]

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
        print(f"失败 {len(failed)} 只，部分失败代码：{failed[:10]}")
    print(f"数据库保存至 {DB_PATH}")