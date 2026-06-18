#!/usr/bin/env python3
"""
下载全季度净利润同比增长率 + 发布日期 + 股票名称
最终优化版：季度级增量 + 财报季智能判断 + 空窗期跳过，非财报季秒级完成
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
CURRENT_MONTH = datetime.now().month

# A股财报披露窗口期对应关系：(报告期, 披露月份范围)
REPORT_DISCLOSURE_WINDOW = [
    ((CURRENT_YEAR-1, 4), (1, 4)),    # 上年年报：1-4月披露
    ((CURRENT_YEAR, 1), (4, 4)),       # 一季报：4月披露
    ((CURRENT_YEAR, 2), (7, 8)),       # 中报：7-8月披露
    ((CURRENT_YEAR, 3), (10, 10)),     # 三季报：10月披露
]

def get_need_download_quarters(conn):
    """
    智能计算需要下载的季度列表
    规则：1. 只取披露期内的报告期  2. 只取比本地最新报告期新的
    """
    # 获取本地最新报告期
    try:
        cursor = conn.execute("SELECT MAX(stat_date) FROM financial")
        res = cursor.fetchone()[0]
        latest_local = pd.Timestamp(res) if res else pd.Timestamp('2000-01-01')
    except:
        latest_local = pd.Timestamp('2000-01-01')

    # 筛选当前月份处于披露窗口的报告期
    candidate_quarters = []
    for (year, q), (start_month, end_month) in REPORT_DISCLOSURE_WINDOW:
        if start_month <= CURRENT_MONTH <= end_month:
            # 报告期截止日
            stat_month = {1:'03-31', 2:'06-30', 3:'09-30', 4:'12-31'}[q]
            stat_date = pd.Timestamp(f"{year}-{stat_month}")
            if stat_date > latest_local:
                candidate_quarters.append((year, q))

    # 兜底：如果本地为空，回退到默认2年全季度
    if latest_local.year == 2000:
        candidate_quarters = []
        for y in [CURRENT_YEAR-1, CURRENT_YEAR]:
            for q in [1,2,3,4]:
                candidate_quarters.append((y, q))

    return candidate_quarters, latest_local

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

def download_single_financial(args):
    code, name, quarters, existing_dates = args
    for retry in range(MAX_RETRY + 1):
        try:
            profit_data = {}
            pub_dates = {}
            for year, quarter in quarters:
                # 粗略预判，本地已有则跳过
                stat_month = {1:'03-31', 2:'06-30', 3:'09-30', 4:'12-31'}[quarter]
                stat_date_str = f"{year}-{stat_month}"
                if stat_date_str in existing_dates:
                    continue

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

# ==================== 主流程 ====================
if __name__ == '__main__':
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    codes = get_mainboard_codes()
    quarters, latest_local = get_need_download_quarters(conn)
    is_full_update = latest_local.year == 2000

    # 没有需要下载的季度，直接退出
    if not quarters and not is_full_update:
        print("当前处于财报空窗期，且本地数据已是最新，无需更新财务数据")
        conn.close()
        exit()

    mode_str = "全量下载" if is_full_update else "增量更新"
    print(f"财务数据{mode_str}，共 {len(codes)} 只股票，{THREAD_NUM} 线程并发")
    print(f"待下载季度：{quarters}")

    # 预加载已存在的报告期
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
        tasks = [(code, name, quarters, all_existing.get(code, set())) for code, name in codes]
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