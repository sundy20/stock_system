#!/usr/bin/env python3
"""
baostock 财务数据下载——智能增量更新，全字段，超时保护，断网重连
- 默认增量模式：仅补充缺失的季度数据（最近两年，以net_profit_yoy是否存在为准）
- 全量模式：python3 download_financials.py --full  强制全量重新下载
- 单线程运行（THREAD_NUM=1），请求间隔 0.5~1.5 秒随机，避免高频被封
- 保存字段：净利润同比、营收同比(留空)、净资产同比、总资产同比、EPS同比、扣非净利润同比
            + 盈利能力：net_profit(净利润)、roe_avg(净资产收益率)、gp_margin(销售毛利率)
            + 业绩快报：express_gryoy(营收同比)、express_opyoy(营业利润同比)
- 失败股票输出到 failed_financial.txt，附带具体原因
"""
import baostock as bs
import sqlite3
import pandas as pd
from datetime import datetime
import time, sys, socket, random
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError

DB_PATH = 'stocks_2y.db'
THREAD_NUM = 1                # 单线程，防止高频被封
REQUEST_MIN_DELAY = 0.5       # 每次查询前最小随机等待（秒）
REQUEST_MAX_DELAY = 1.5       # 最大随机等待（秒）
MAX_RETRY = 2                 # 最大重试次数
SINGLE_TASK_TIMEOUT = 180     # 单任务超时
SOCKET_TIMEOUT = 180
CURRENT_YEAR = datetime.now().year
QUARTERS = [1, 2, 3, 4]


def reconnect_baostock():
    """断网重连：登出并重新登录，随机等待 5~10 秒"""
    try:
        bs.logout()
    except:
        pass
    wait = random.uniform(5, 10)
    print(f"  ⚠ 网络异常，{wait:.1f} 秒后重连...", file=sys.stderr)
    time.sleep(wait)
    lg = bs.login()
    if lg.error_code != '0':
        raise RuntimeError(f"重连失败: {lg.error_msg}")
    socket.setdefaulttimeout(SOCKET_TIMEOUT)


def init_worker():
    """线程初始化：设置 Socket 超时并登录；失败则抛出异常"""
    socket.setdefaulttimeout(SOCKET_TIMEOUT)
    lg = bs.login()
    if lg.error_code != '0':
        raise RuntimeError(f"线程登录失败: {lg.error_msg}")


def get_mainboard_codes(conn):
    """从本地 stock_basic 表获取股票列表（排除沪深300）"""
    df = pd.read_sql("SELECT code, name FROM stock_basic WHERE code != 'sh.000300'", conn)
    return list(zip(df['code'], df['name']))


def init_db(conn):
    """创建 financial 表（若不存在）"""
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute('''CREATE TABLE IF NOT EXISTS financial (
                                                             code TEXT, name TEXT,
                                                             pub_date TEXT, stat_date TEXT,
                                                             net_profit_yoy REAL, revenue_yoy REAL,
                                                             yoy_equity REAL, yoy_asset REAL, yoy_eps REAL, yoy_pni REAL,
                                                             net_profit REAL, roe_avg REAL, gp_margin REAL,
                                                             express_gryoy REAL, express_opyoy REAL,
                                                             express_pub_date TEXT, express_stat_date TEXT,
                                                             PRIMARY KEY (code, stat_date)
        )''')
    conn.commit()


def get_missing_quarters(conn, code):
    """返回该股票最近两年缺失的季度列表 [(year, quarter, stat_date)]，同时检查新增字段是否缺失"""
    existing = {}
    rows = conn.execute("""
                        SELECT stat_date, net_profit_yoy, net_profit FROM financial
                        WHERE code=? AND stat_date >= ?
                        """, (code, f"{CURRENT_YEAR-2}-01-01")).fetchall()
    for sd, net_yoy, net_p in rows:
        existing[sd] = (net_yoy is not None, net_p is not None)  # (有成长数据, 有利润数据)

    missing = []
    for year in [CURRENT_YEAR - 1, CURRENT_YEAR]:
        for quarter in QUARTERS:
            if quarter == 1:
                sd = f"{year}-03-31"
            elif quarter == 2:
                sd = f"{year}-06-30"
            elif quarter == 3:
                sd = f"{year}-09-30"
            else:
                sd = f"{year}-12-31"
            if sd not in existing:
                missing.append((year, quarter, sd))
            else:
                has_growth, has_profit = existing[sd]
                if not has_growth or not has_profit:  # 任一缺失则重新拉取
                    missing.append((year, quarter, sd))
    return missing


def download_single_financial(args):
    """
    下载单只股票指定季度的财务数据（成长能力 + 盈利能力 + 业绩快报）
    返回 (DataFrame_or_None, fail_code, error_msg)
    """
    code, name, year, quarter = args
    time.sleep(random.uniform(REQUEST_MIN_DELAY, REQUEST_MAX_DELAY))

    for retry in range(MAX_RETRY + 1):
        try:
            # 1. 成长能力
            growth_data = {}
            rs = bs.query_growth_data(code, year=year, quarter=quarter)
            if rs.error_code == '0':
                rows = []
                while rs.next():
                    rows.append(rs.get_row_data())
                if rows:
                    fields = [f.lower() for f in rs.fields]
                    df = pd.DataFrame(rows, columns=rs.fields)
                    col_map = {
                        'yoyni': 'net_profit_yoy',
                        'yoyequity': 'yoy_equity',
                        'yoyasset': 'yoy_asset',
                        'yoyepsbasic': 'yoy_eps',
                        'yoypni': 'yoy_pni'
                    }
                    for orig, target in col_map.items():
                        if orig in fields:
                            growth_data[target] = pd.to_numeric(df[rs.fields[fields.index(orig)]], errors='coerce').iloc[0]
                    pub_date_col = next((f for f in rs.fields if 'pubdate' in f.lower()), None)
                    stat_date_col = next((f for f in rs.fields if 'statdate' in f.lower()), None)
                    if stat_date_col:
                        sd = str(pd.to_datetime(df[stat_date_col].iloc[0]).date())
                        growth_data['pub_date'] = str(pd.to_datetime(df[pub_date_col].iloc[0]).date()) if pub_date_col else ''
                        growth_data['stat_date'] = sd

            # 2. 盈利能力
            profit_data = {}
            rs_profit = bs.query_profit_data(code, year=year, quarter=quarter)
            if rs_profit.error_code == '0':
                rows = []
                while rs_profit.next():
                    rows.append(rs_profit.get_row_data())
                if rows:
                    fields = [f.lower() for f in rs_profit.fields]
                    df = pd.DataFrame(rows, columns=rs_profit.fields)
                    if 'netProfit' in rs_profit.fields:
                        profit_data['net_profit'] = pd.to_numeric(df['netProfit'].iloc[0], errors='coerce')
                    if 'roeAvg' in rs_profit.fields:
                        profit_data['roe_avg'] = pd.to_numeric(df['roeAvg'].iloc[0], errors='coerce')
                    if 'gpMargin' in rs_profit.fields:
                        profit_data['gp_margin'] = pd.to_numeric(df['gpMargin'].iloc[0], errors='coerce')

            # 3. 业绩快报（取最新一期）
            express_data = {}
            rs_express = bs.query_performance_express_report(code, start_date=f"{year}-01-01", end_date=f"{year+1}-12-31")
            if rs_express.error_code == '0':
                rows = []
                while rs_express.next():
                    rows.append(rs_express.get_row_data())
                if rows:
                    df = pd.DataFrame(rows, columns=rs_express.fields)
                    # 筛选出对应季度最后一天的快报
                    target_end = f"{year}-{quarter*3:02d}-31" if quarter < 4 else f"{year}-12-31"
                    mask = df['performanceExpStatDate'] == target_end
                    if mask.any():
                        latest = df[mask].iloc[-1]
                        if 'performanceExpressGRYOY' in rs_express.fields:
                            express_data['express_gryoy'] = pd.to_numeric(latest['performanceExpressGRYOY'], errors='coerce')
                        if 'performanceExpressOPYOY' in rs_express.fields:
                            express_data['express_opyoy'] = pd.to_numeric(latest['performanceExpressOPYOY'], errors='coerce')
                        express_data['express_pub_date'] = latest.get('performanceExpPubDate', '')
                        express_data['express_stat_date'] = latest.get('performanceExpStatDate', '')

            # 合并记录
            record = {
                'code': code, 'name': name,
                'pub_date': growth_data.get('pub_date', ''),
                'stat_date': growth_data.get('stat_date', f"{year}-{quarter*3:02d}-31"),
                'net_profit_yoy': growth_data.get('net_profit_yoy'),
                'revenue_yoy': None,
                'yoy_equity': growth_data.get('yoy_equity'),
                'yoy_asset': growth_data.get('yoy_asset'),
                'yoy_eps': growth_data.get('yoy_eps'),
                'yoy_pni': growth_data.get('yoy_pni'),
                **profit_data,
                **express_data
            }
            # 如果既没有成长数据，也没有利润数据，则返回None
            if not growth_data and not profit_data:
                return None, code, "无有效财务数据"
            return pd.DataFrame([record]), None, None

        except (BrokenPipeError, ConnectionError, OSError, socket.timeout) as e:
            print(f"  ⚠ {code} {name} 网络异常 ({e})，准备重连...", file=sys.stderr)
            reconnect_baostock()
            continue
        except Exception as e:
            return None, code, f"{type(e).__name__}: {e}"
    return None, code, "多次重试后仍失败"


def batch_write_safe(conn, df_list):
    """临时表 + INSERT OR REPLACE 批量写入"""
    if not df_list:
        return
    all_df = pd.concat(df_list, ignore_index=True)
    all_df.to_sql('financial_temp', conn, if_exists='replace', index=False, method='multi')
    conn.execute('''
        INSERT OR REPLACE INTO financial (code, name, pub_date, stat_date,
                                          net_profit_yoy, revenue_yoy,
                                          yoy_equity, yoy_asset, yoy_eps, yoy_pni,
                                          net_profit, roe_avg, gp_margin,
                                          express_gryoy, express_opyoy,
                                          express_pub_date, express_stat_date)
        SELECT code, name, pub_date, stat_date,
               net_profit_yoy, revenue_yoy,
               yoy_equity, yoy_asset, yoy_eps, yoy_pni,
               net_profit, roe_avg, gp_margin,
               express_gryoy, express_opyoy,
               express_pub_date, express_stat_date FROM financial_temp
    ''')
    conn.execute("DROP TABLE IF EXISTS financial_temp")
    conn.commit()


if __name__ == '__main__':
    full_mode = '--full' in sys.argv
    mode_str = '全量重新下载' if full_mode else '智能增量更新'
    print(f"登录 baostock ... （{mode_str}）")
    lg = bs.login()
    if lg.error_code != '0':
        print(f"主线程登录失败: {lg.error_msg}")
        sys.exit(1)
    print("login success!")

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    codes = get_mainboard_codes(conn)
    total = len(codes)

    OVERALL_TIMEOUT = 3600 if full_mode else 1200

    tasks = []
    skipped = 0
    for code, name in codes:
        if full_mode:
            for y in [CURRENT_YEAR - 1, CURRENT_YEAR]:
                for q in QUARTERS:
                    tasks.append((code, name, y, q))
        else:
            missing = get_missing_quarters(conn, code)
            for year, quarter, sd in missing:
                tasks.append((code, name, year, quarter))
            if not missing:
                skipped += 1

    print(f"共 {total} 只股票，{len(tasks)} 个季度任务，已跳过 {skipped} 只完整股票")
    if not tasks:
        print("全部财务数据已是最新，无需下载。")
        bs.logout()
        conn.close()
        sys.exit(0)

    print(f"{THREAD_NUM} 线程，整体超时 {OVERALL_TIMEOUT} 秒，请求间隔 {REQUEST_MIN_DELAY}~{REQUEST_MAX_DELAY} 秒")
    bs.logout()

    success, failed_stocks, batch_buffer = 0, {}, []
    with ThreadPoolExecutor(max_workers=THREAD_NUM, initializer=init_worker) as executor:
        future_to_code = {executor.submit(download_single_financial, t): t for t in tasks}
        try:
            for i, future in enumerate(as_completed(future_to_code, timeout=OVERALL_TIMEOUT)):
                task = future_to_code[future]
                code, name, _, _ = task
                try:
                    result, fail_code, err_msg = future.result(timeout=SINGLE_TASK_TIMEOUT)
                except Exception as e:
                    err_msg = f"Future 异常: {type(e).__name__}: {e}"
                    failed_stocks[code] = err_msg
                    print(f"  ⚠ {code} {name} {err_msg}", file=sys.stderr)
                    continue

                if result is not None and not result.empty:
                    batch_buffer.append(result)
                    success += 1
                    if len(batch_buffer) >= 50:
                        batch_write_safe(conn, batch_buffer)
                        batch_buffer = []
                        print(f"  已完成 {success}/{len(tasks)} 个季度")
                else:
                    if code not in failed_stocks:
                        failed_stocks[code] = err_msg
                    print(f"  ✗ {code} {name} 失败: {err_msg}")

                if (i + 1) % 200 == 0:
                    print(f"  已处理 {i + 1}/{len(tasks)}")

        except FuturesTimeoutError:
            print(f"\n⚠ 整体超时（{OVERALL_TIMEOUT}秒），剩余任务跳过")
            for future in future_to_code:
                if not future.done():
                    task = future_to_code[future]
                    failed_stocks[task[0]] = "整体超时未完成"

    if batch_buffer:
        batch_write_safe(conn, batch_buffer)
    conn.commit()
    conn.close()

    print(f"\n成功 {success}/{len(tasks)} 个季度")
    if failed_stocks:
        print(f"失败股票 {len(failed_stocks)} 只，详情写入 failed_financial.txt")
        with open('failed_financial.txt', 'w') as f:
            for code, reason in failed_stocks.items():
                f.write(f"{code},{reason}\n")
        print("前10条失败示例:")
        for code, reason in list(failed_stocks.items())[:10]:
            print(f"  {code}: {reason}")