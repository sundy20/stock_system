#!/usr/bin/env python3
"""
baostock 财务数据下载——全字段，超时保护
- 数据源为正式季报/年报，不含业绩预告
- 支持并发下载、自动重试、临时表无冲突写入
- 获取最近2年所有季度数据，pub_date+10天生效（消除未来函数）
- 保存字段：净利润增长率、营收增长率、净资产增长率、总资产增长率、EPS增长率、扣非净利润增长率
"""
import baostock as bs
import sqlite3
import pandas as pd
from datetime import datetime
import time, sys
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError

# ===================== 配置 =====================
DB_PATH = 'stocks_2y.db'
THREAD_NUM = 2                # 并发线程数
REQUEST_DELAY = 0.2           # 单线程请求间隔（秒）
MAX_RETRY = 2                 # 最大重试次数
SINGLE_TASK_TIMEOUT = 120     # 单只股票最大处理时间（秒）
OVERALL_TIMEOUT = 600         # 整体最大等待时间（秒），防止全部线程死锁

CURRENT_YEAR = datetime.now().year
YEARS = [CURRENT_YEAR - 1, CURRENT_YEAR]   # 查询最近两年
QUARTERS = [1, 2, 3, 4]


def init_worker():
    """线程初始化：独立登录 baostock"""
    bs.login()


def get_mainboard_codes(conn):
    """从本地 stock_basic 表获取股票列表（排除沪深300）"""
    df = pd.read_sql("SELECT code, name FROM stock_basic WHERE code != 'sh.000300'", conn)
    return list(zip(df['code'], df['name']))


def init_db(conn):
    """创建 financial 表，开启 WAL 模式，包含所有扩展字段"""
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
    """
    下载单只股票的净利润同比增长率及多项成长指标
    返回 (DataFrame, 失败代码) 或 (None, 失败代码)
    """
    code, name = args
    for retry in range(MAX_RETRY + 1):
        try:
            profit_data = {}
            pub_dates = {}
            extra = {}       # 存储其他成长指标
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
                    # 字段名统一转为小写方便映射
                    fields = [f.lower() for f in rs.fields]
                    df = pd.DataFrame(rows, columns=rs.fields)
                    # 列名映射（原始字段 → 目标字段）
                    col_map = {
                        'yoyni': 'net_profit_yoy',
                        'yoyequity': 'yoy_equity',
                        'yoyasset': 'yoy_asset',
                        'yoyepsbasic': 'yoy_eps',
                        'yoypni': 'yoy_pni'
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
                            extra[sd] = {
                                'yoy_equity': row.get('yoy_equity'),
                                'yoy_asset': row.get('yoy_asset'),
                                'yoy_eps': row.get('yoy_eps'),
                                'yoy_pni': row.get('yoy_pni')
                            }
            time.sleep(REQUEST_DELAY)
            if not profit_data:
                return None, code

            records = []
            for stat_date, net_yoy in profit_data.items():
                ex = extra.get(stat_date, {})
                records.append({
                    'code': code, 'name': name,
                    'pub_date': pub_dates.get(stat_date, ''),
                    'stat_date': stat_date,
                    'net_profit_yoy': net_yoy,
                    'revenue_yoy': None,   # baostock 成长数据不含营收增长率，预留
                    'yoy_equity': ex.get('yoy_equity'),
                    'yoy_asset': ex.get('yoy_asset'),
                    'yoy_eps': ex.get('yoy_eps'),
                    'yoy_pni': ex.get('yoy_pni')
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
    print(f"下载财务数据，共 {total} 只股票，{THREAD_NUM} 线程并发，全季度数据")
    bs.logout()   # 主线程登出，子线程各自登录

    success = 0
    failed = []
    batch_buffer = []
    BATCH_SIZE = 50

    with ThreadPoolExecutor(max_workers=THREAD_NUM, initializer=init_worker) as executor:
        # 保存 future -> (code, name) 映射，用于超时信息输出
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