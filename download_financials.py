#!/usr/bin/env python3
"""
baostock 财务数据下载——全字段，超时保护（增加全局Socket超时，防止线程死锁）
"""
import baostock as bs
import sqlite3
import pandas as pd
from datetime import datetime
import time, sys, socket
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError

DB_PATH = 'stocks_2y.db'
THREAD_NUM = 2
REQUEST_DELAY = 0.2
MAX_RETRY = 2
SINGLE_TASK_TIMEOUT = 120
OVERALL_TIMEOUT = 600
SOCKET_TIMEOUT = 120          # 全局网络超时（秒），防止线程无限阻塞

CURRENT_YEAR = datetime.now().year
YEARS = [CURRENT_YEAR - 1, CURRENT_YEAR]
QUARTERS = [1, 2, 3, 4]


def init_worker():
    """线程初始化：设置Socket超时并登录"""
    socket.setdefaulttimeout(SOCKET_TIMEOUT)   # ★ 关键：避免单个请求永久阻塞
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
                                                             yoy_equity REAL, yoy_asset REAL, yoy_eps REAL, yoy_pni REAL,
                                                             PRIMARY KEY (code, stat_date)
        )''')
    conn.commit()


def download_single_financial(args):
    code, name = args
    for retry in range(MAX_RETRY + 1):
        try:
            profit_data, pub_dates, extra = {}, {}, {}
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
                    fields = [f.lower() for f in rs.fields]
                    df = pd.DataFrame(rows, columns=rs.fields)
                    col_map = {
                        'yoyni': 'net_profit_yoy', 'yoyequity': 'yoy_equity',
                        'yoyasset': 'yoy_asset', 'yoyepsbasic': 'yoy_eps', 'yoypni': 'yoy_pni'
                    }
                    for orig, target in col_map.items():
                        if orig in fields:
                            df[target] = pd.to_numeric(df[rs.fields[fields.index(orig)]], errors='coerce')
                    pub_date_col = next((f for f in rs.fields if 'pubdate' in f.lower()), None)
                    stat_date_col = next((f for f in rs.fields if 'statdate' in f.lower()), None)
                    for _, row in df.iterrows():
                        if pd.notna(row.get('net_profit_yoy')):
                            sd = str(pd.to_datetime(row[stat_date_col]).date())
                            profit_data[sd] = row['net_profit_yoy']
                            if pub_date_col:
                                pub_dates[sd] = str(pd.to_datetime(row[pub_date_col]).date())
                            extra[sd] = {k: row.get(k) for k in ['yoy_equity','yoy_asset','yoy_eps','yoy_pni']}
            time.sleep(REQUEST_DELAY)
            if not profit_data:
                return None, code
            records = []
            for sd, net_yoy in profit_data.items():
                ex = extra.get(sd, {})
                records.append({
                    'code': code, 'name': name,
                    'pub_date': pub_dates.get(sd, ''),
                    'stat_date': sd,
                    'net_profit_yoy': net_yoy,
                    'revenue_yoy': None,
                    **ex
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
        INSERT OR REPLACE INTO financial (code, name, pub_date, stat_date,
                                          net_profit_yoy, revenue_yoy,
                                          yoy_equity, yoy_asset, yoy_eps, yoy_pni)
        SELECT code, name, pub_date, stat_date,
               net_profit_yoy, revenue_yoy,
               yoy_equity, yoy_asset, yoy_eps, yoy_pni FROM financial_temp
    ''')
    conn.execute("DROP TABLE IF EXISTS financial_temp")
    conn.commit()


if __name__ == '__main__':
    print("登录 baostock ...")
    bs.login()
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    codes = get_mainboard_codes(conn)
    total = len(codes)
    print(f"下载财务数据，共 {total} 只股票，{THREAD_NUM} 线程")
    bs.logout()

    success, failed, batch_buffer = 0, [], []
    with ThreadPoolExecutor(max_workers=THREAD_NUM, initializer=init_worker) as executor:
        future_to_code = {executor.submit(download_single_financial, t): t for t in codes}
        try:
            for i, future in enumerate(as_completed(future_to_code, timeout=OVERALL_TIMEOUT)):
                code, name = future_to_code[future]
                try:
                    result, fail_code = future.result(timeout=SINGLE_TASK_TIMEOUT)
                except Exception as e:
                    print(f"  ⚠ {code} {name} 超时/异常: {e}", file=sys.stderr)
                    failed.append(code)
                    continue
                if result is not None and not result.empty:
                    batch_buffer.append(result)
                    success += 1
                    if len(batch_buffer) >= 50:
                        batch_write_safe(conn, batch_buffer)
                        batch_buffer = []
                        print(f"  已完成 {success}/{total} 只")
                else:
                    failed.append(fail_code)
                if (i+1) % 200 == 0:
                    print(f"  已处理 {i+1}/{total}")

        except FuturesTimeoutError:
            print(f"\n⚠ 整体超时（{OVERALL_TIMEOUT}秒），剩余任务跳过")
            for future in future_to_code:
                if not future.done():
                    failed.append(future_to_code[future][0])

    if batch_buffer:
        batch_write_safe(conn, batch_buffer)
    conn.commit()
    conn.close()
    print(f"\n财务下载结束，成功 {success}/{total} 只；失败 {len(failed)} 只")
    if failed:
        print(f"示例: {failed[:10]}")