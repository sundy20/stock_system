#!/usr/bin/env python3
"""
baostock 财务数据下载：单季度净利润同比增长率
- 数据源为正式季报/年报，不含业绩预告
- 支持并发下载、自动重试、临时表无冲突写入
- 获取最近2年所有季度数据，发布后10天生效（消除未来函数）
- 内置单任务超时与整体超时，防止个别股票阻塞所有线程
"""
import baostock as bs
import sqlite3
import pandas as pd
from datetime import datetime
import time
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError

DB_PATH = 'stocks_2y.db'
THREAD_NUM = 2              # 并发线程数
REQUEST_DELAY = 0.2         # 单线程请求间隔（秒）
MAX_RETRY = 2               # 最大重试次数
SINGLE_TASK_TIMEOUT = 120   # 单只股票最大处理时间（秒），超时将跳过
OVERALL_TIMEOUT = 600       # 整体最大等待时间（秒），通常 10 分钟足以；若全部线程卡死则强制终止

CURRENT_YEAR = datetime.now().year
YEARS = [CURRENT_YEAR - 1, CURRENT_YEAR]
QUARTERS = [1, 2, 3, 4]


def init_worker():
    """线程初始化：独立登录"""
    bs.login()


def get_mainboard_codes(conn):
    """从本地 stock_basic 表获取股票列表（排除沪深300）"""
    df = pd.read_sql("SELECT code, name FROM stock_basic WHERE code != 'sh.000300'", conn)
    return list(zip(df['code'], df['name']))


def init_db(conn):
    """创建 financial 表，开启 WAL 模式"""
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute('''CREATE TABLE IF NOT EXISTS financial (
                                                             code TEXT, name TEXT,
                                                             pub_date TEXT, stat_date TEXT,
                                                             net_profit_yoy REAL, revenue_yoy REAL,
                                                             PRIMARY KEY (code, stat_date)
        )''')
    conn.commit()


def download_single_financial(args):
    """下载单只股票的净利润同比增长率，返回 DataFrame 或 None"""
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
                    # 动态识别净利润增长率字段（通常为 YOYNI）
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
        except Exception:
            time.sleep(0.5)
            continue
    return None, code


def batch_write_safe(conn, df_list):
    """临时表 + INSERT OR REPLACE 安全批量写入"""
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
    total = len(codes)
    print(f"下载财务数据，共 {total} 只股票，{THREAD_NUM} 线程并发，全季度数据")
    bs.logout()

    success = 0
    failed = []
    batch_buffer = []
    BATCH_SIZE = 50

    with ThreadPoolExecutor(max_workers=THREAD_NUM, initializer=init_worker) as executor:
        future_to_code = {executor.submit(download_single_financial, task): task for task in codes}

        try:
            for i, future in enumerate(as_completed(future_to_code, timeout=OVERALL_TIMEOUT)):
                code, name = future_to_code[future]
                try:
                    result, fail_code = future.result(timeout=SINGLE_TASK_TIMEOUT)
                except Exception as e:
                    print(f"  ⚠ {code} {name} 处理异常/超时，跳过: {e}", file=sys.stderr)
                    failed.append(code)
                    continue

                if result is not None and not result.empty:
                    batch_buffer.append(result)
                    success += 1
                    if len(batch_buffer) >= BATCH_SIZE:
                        batch_write_safe(conn, batch_buffer)
                        batch_buffer = []
                        print(f"  已完成 {success}/{total} 只")
                else:
                    failed.append(fail_code)

                if (i + 1) % 200 == 0:
                    print(f"  已处理 {i + 1}/{total}")

        except FuturesTimeoutError:
            print(f"\n⚠ 整体超时（{OVERALL_TIMEOUT}秒），剩余任务将被跳过。")
            # 收集未完成的任务
            for future in future_to_code:
                if not future.done():
                    code, name = future_to_code[future]
                    print(f"  ⚠ 未完成: {code} {name}", file=sys.stderr)
                    failed.append(code)

    if batch_buffer:
        batch_write_safe(conn, batch_buffer)

    conn.commit()
    conn.close()
    print(f"\n财务数据下载结束，成功 {success}/{total} 只")
    if failed:
        print(f"失败 {len(failed)} 只（示例: {failed[:10]}）")