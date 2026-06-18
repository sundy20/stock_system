#!/usr/bin/env python3
"""
下载全季度净利润同比增长率 + 发布日期 + 股票名称
最终优化版：增量更新、6线程安全并发、存在跳过、无冲突批量写入
"""
import baostock as bs
import sqlite3
import pandas as pd
from datetime import datetime
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

DB_PATH = 'stocks_2y.db'
THREAD_NUM = 6
REQUEST_DELAY = 0.1
MAX_RETRY = 2

CURRENT_YEAR = datetime.now().year
YEARS = [CURRENT_YEAR - 1, CURRENT_YEAR]
QUARTERS = [1, 2, 3, 4]

def init_worker():
    bs.login()

def get_mainboard_codes():
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql("SELECT code, name FROM stock_basic WHERE code != 'sh.000300'", conn)
    conn.close()
    return list(zip(df['code'], df['name']))

def init_db(conn):
    conn.execute("PRAGMA journal_mode=WAL;")
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

def get_existing_stat_dates(conn, code):
    """获取某只股票已有的报告期，避免重复下载"""
    cursor = conn.execute("SELECT stat_date FROM financial WHERE code=?", (code,))
    return {row[0] for row in cursor.fetchall()}

def get_latest_stat_date(conn):
    """获取数据库中最新的报告期，用于增量判断"""
    try:
        cursor = conn.execute("SELECT MAX(stat_date) FROM financial")
        res = cursor.fetchone()[0]
        return res if res else '2000-01-01'
    except:
        return '2000-01-01'

def download_single_financial(args):
    code, name, existing_dates = args
    for retry in range(MAX_RETRY + 1):
        try:
            profit_data = {}
            pub_dates = {}
            for year in YEARS:
                for quarter in QUARTERS:
                    # 构造报告期日期，粗略判断是否已存在（精确匹配在入库前做）
                    stat_month = {1:'03-31', 2:'06-30', 3:'09-30', 4:'12-31'}[quarter]
                    stat_date_str = f"{year}-{stat_month}"
                    if stat_date_str in existing_dates:
                        continue  # 本地已有，直接跳过

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
    """安全批量写入，临时表过渡，主键冲突自动覆盖"""
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

# ==================== 主流程 ====================
if __name__ == '__main__':
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    codes = get_mainboard_codes()
    latest_date = get_latest_stat_date(conn)

    # 判断是否需要全量更新
    is_full_update = latest_date == '2000-01-01'
    mode_str = "全量下载" if is_full_update else "增量更新"
    print(f"财务数据{mode_str}，共 {len(codes)} 只股票，{THREAD_NUM} 线程并发，全季度数据")

    # 预加载所有已存在的报告期，传入线程减少数据库查询
    all_existing = {}
    if not is_full_update:
        cursor = conn.execute("SELECT code, stat_date FROM financial")
        for code, sd in cursor.fetchall():
            if code not in all_existing:
                all_existing[code] = set()
            all_existing[code].add(sd)

    conn.close()

    success = 0
    failed = []
    batch_buffer = []
    BATCH_SIZE = 50

    with ThreadPoolExecutor(max_workers=THREAD_NUM, initializer=init_worker) as executor:
        tasks = [(code, name, all_existing.get(code, set())) for code, name in codes]
        futures = {executor.submit(download_single_financial, task): task for task in tasks}
        for i, future in enumerate(as_completed(futures)):
            result, fail_code = future.result()
            if result is not None and not result.empty:
                batch_buffer.append(result)
                success += 1

                if len(batch_buffer) >= BATCH_SIZE:
                    conn = sqlite3.connect(DB_PATH)
                    batch_write_safe(conn, batch_buffer)
                    conn.close()
                    batch_buffer = []
                    print(f"  已完成 {success}/{len(codes)} 只")
            else:
                failed.append(fail_code)

            if (i+1) % 200 == 0:
                print(f"  已处理 {i+1}/{len(codes)}")

    if batch_buffer:
        conn = sqlite3.connect(DB_PATH)
        batch_write_safe(conn, batch_buffer)
        conn.close()

    print(f"\n财务数据下载完成，成功 {success}/{len(codes)} 只")
    if failed:
        print(f"失败 {len(failed)} 只，部分失败代码：{failed[:10]}")