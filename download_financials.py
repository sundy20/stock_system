#!/usr/bin/env python3
"""
baostock 财务数据下载——最终版，双线程，增量更新，防封禁
- 主线程登录验证，失败直接退出
- 工作线程独立登录，失败报错
- 每只股票每次查询前随机等待 0.5~1.5 秒，避免高频被封
- 网络异常自动重连，重连等待 5~10 秒
- 增量模式只补充缺失的季度（最近两年，以 net_profit_yoy 是否存在为准）
- 全量模式：python3 download_financials.py --full
- 失败记录写入 failed_financial.txt，附具体原因
"""
import baostock as bs
import sqlite3
import pandas as pd
from datetime import datetime
import time, sys, socket, random
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError

# ===================== 配置 =====================
DB_PATH = 'stocks_2y.db'
THREAD_NUM = 2                # 双线程，日常安全
REQUEST_MIN_DELAY = 0.5       # 每次查询前最小随机等待（秒）
REQUEST_MAX_DELAY = 1.5       # 最大随机等待（秒）
MAX_RETRY = 2                 # 单只股票最大重试次数
SINGLE_TASK_TIMEOUT = 180     # 单任务超时（秒）
SOCKET_TIMEOUT = 180          # 底层 Socket 超时
CURRENT_YEAR = datetime.now().year
QUARTERS = [1, 2, 3, 4]      # 季度


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
    """线程初始化：设置 Socket 超时并登录；失败直接抛出异常"""
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
                                                             PRIMARY KEY (code, stat_date)
        )''')
    conn.commit()


def get_missing_quarters(conn, code):
    """返回该股票最近两年缺失的季度列表 [(year, quarter, stat_date)]"""
    existing = {}
    rows = conn.execute("""
                        SELECT stat_date, net_profit_yoy FROM financial
                        WHERE code=? AND stat_date >= ?
                        """, (code, f"{CURRENT_YEAR-2}-01-01")).fetchall()
    for sd, net in rows:
        existing[sd] = net is not None

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
            if sd not in existing or not existing[sd]:
                missing.append((year, quarter, sd))
    return missing


def download_single_financial(args):
    """
    下载单只股票指定季度的财务数据，返回 (DataFrame_or_None, fail_code, error_msg)
    每次查询前随机等待 REQUEST_MIN_DELAY~REQUEST_MAX_DELAY 秒
    """
    code, name, year, quarter = args
    # ★ 每次查询前随机延迟，避免高频请求
    time.sleep(random.uniform(REQUEST_MIN_DELAY, REQUEST_MAX_DELAY))

    for retry in range(MAX_RETRY + 1):
        try:
            rs = bs.query_growth_data(code, year=year, quarter=quarter)
            if rs.error_code != '0':
                return None, code, f"查询失败: {rs.error_msg}"
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
            if not rows:
                return None, code, "无数据返回"

            fields = [f.lower() for f in rs.fields]
            df = pd.DataFrame(rows, columns=rs.fields)
            # 字段映射
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
            if not stat_date_col:
                return None, code, "缺少 stat_date 字段"

            sd = str(pd.to_datetime(df[stat_date_col].iloc[0]).date())
            record = {
                'code': code, 'name': name,
                'pub_date': str(pd.to_datetime(df[pub_date_col].iloc[0]).date()) if pub_date_col else '',
                'stat_date': sd,
                'net_profit_yoy': df['net_profit_yoy'].iloc[0] if 'net_profit_yoy' in df.columns else None,
                'revenue_yoy': None,
                'yoy_equity': df['yoy_equity'].iloc[0] if 'yoy_equity' in df.columns else None,
                'yoy_asset': df['yoy_asset'].iloc[0] if 'yoy_asset' in df.columns else None,
                'yoy_eps': df['yoy_eps'].iloc[0] if 'yoy_eps' in df.columns else None,
                'yoy_pni': df['yoy_pni'].iloc[0] if 'yoy_pni' in df.columns else None,
            }
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
                                          yoy_equity, yoy_asset, yoy_eps, yoy_pni)
        SELECT code, name, pub_date, stat_date,
               net_profit_yoy, revenue_yoy,
               yoy_equity, yoy_asset, yoy_eps, yoy_pni FROM financial_temp
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

    # 动态设置整体超时（单任务较慢，适当放宽）
    OVERALL_TIMEOUT = 3600 if full_mode else 1200

    # 构建任务列表
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
    bs.logout()  # 主线程登出

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