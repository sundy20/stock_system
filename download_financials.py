#!/usr/bin/env python3
"""
baostock 财务数据下载——最终稳定版（主线程直接执行，彻底避免卡死）
- 默认增量模式：仅补充缺失的季度数据（最近两年）
- 全量模式：python3 download_financials.py --full  强制全量重新下载
- 单线程直接顺序执行，请求间隔 0.2~1.0 秒随机，避免高频被封
- 断网重连等待 10~20 秒
- 保存字段：成长能力 + 盈利能力 + 业绩快报
- 失败股票输出到 failed_financial.txt，附带具体错误码和错误信息
"""
import baostock as bs
import sqlite3
import pandas as pd
from datetime import datetime
import time, sys, socket, random

DB_PATH = 'stocks_2y.db'
REQUEST_MIN_DELAY = 0.2       # 每次查询前最小随机等待（秒）
REQUEST_MAX_DELAY = 1.0       # 最大随机等待（秒）
MAX_RETRY = 2                 # 最大重试次数
SOCKET_TIMEOUT = 180          # 底层 Socket 超时（秒）
CURRENT_YEAR = datetime.now().year
QUARTERS = [1, 2, 3, 4]      # 四个季度

# 定义所有需要写入的列，确保临时表包含这些列
ALL_COLUMNS = [
    'code', 'name', 'pub_date', 'stat_date',
    'net_profit_yoy', 'revenue_yoy',
    'yoy_equity', 'yoy_asset', 'yoy_eps', 'yoy_pni',
    'net_profit', 'roe_avg', 'gp_margin',
    'express_gryoy', 'express_opyoy',
    'express_pub_date', 'express_stat_date'
]


def reconnect_baostock():
    """断网重连：登出并重新登录，随机等待 10~20 秒"""
    try:
        bs.logout()
    except:
        pass
    wait = random.uniform(10, 20)
    print(f"  ⚠ 网络异常，{wait:.1f} 秒后重连...", file=sys.stderr)
    time.sleep(wait)
    lg = bs.login()
    if lg.error_code != '0':
        raise RuntimeError(f"重连失败: {lg.error_msg}")
    socket.setdefaulttimeout(SOCKET_TIMEOUT)


def get_mainboard_codes(conn):
    """从本地 stock_basic 表获取股票列表（排除沪深300）"""
    df = pd.read_sql("SELECT code, name FROM stock_basic WHERE code != 'sh.000300'", conn)
    return list(zip(df['code'], df['name']))


def init_db(conn):
    """创建 financial 表（若不存在），包含全部字段"""
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
    """
    返回该股票最近两年缺失的季度列表 [(year, quarter, stat_date)]
    判断标准：stat_date 不存在，或 net_profit_yoy 为 NULL
    """
    existing = set()
    rows = conn.execute("""
                        SELECT stat_date FROM financial
                        WHERE code=? AND stat_date >= ? AND net_profit_yoy IS NOT NULL
                        """, (code, f"{CURRENT_YEAR-2}-01-01")).fetchall()
    for (sd,) in rows:
        existing.add(sd)

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
            # ★ 跳过未来季度（财报不可能已发布）
            if pd.Timestamp(sd) > pd.Timestamp.now():
                continue
            if sd not in existing:
                missing.append((year, quarter, sd))
    return missing


def download_single(code, name, year, quarter):
    """
    下载单只股票指定季度的财务数据（成长能力 + 盈利能力 + 业绩快报）
    只要成长能力接口有数据就算成功
    返回 (DataFrame_or_None, error_msg)
    """
    time.sleep(random.uniform(REQUEST_MIN_DELAY, REQUEST_MAX_DELAY))

    for retry in range(MAX_RETRY + 1):
        try:
            growth_data = {}
            profit_data = {}
            express_data = {}

            # 1. 成长能力（必选）
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
                            growth_data[target] = pd.to_numeric(
                                df[rs.fields[fields.index(orig)]], errors='coerce').iloc[0]
                    pub_date_col = next((f for f in rs.fields if 'pubdate' in f.lower()), None)
                    stat_date_col = next((f for f in rs.fields if 'statdate' in f.lower()), None)
                    if stat_date_col:
                        sd = str(pd.to_datetime(df[stat_date_col].iloc[0]).date())
                        growth_data['pub_date'] = str(pd.to_datetime(df[pub_date_col].iloc[0]).date()) if pub_date_col else ''
                        growth_data['stat_date'] = sd
            else:
                if retry == MAX_RETRY:
                    return None, f"成长能力接口错误: error_code={rs.error_code}, msg={rs.error_msg}"

            if not growth_data:
                if retry < MAX_RETRY:
                    continue
                return None, f"成长能力无数据 (error_code={rs.error_code})"

            # 2. 盈利能力（可选）
            try:
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
            except:
                pass

            # 3. 业绩快报（可选）
            try:
                rs_express = bs.query_performance_express_report(code, start_date=f"{year}-01-01", end_date=f"{year+1}-12-31")
                if rs_express.error_code == '0':
                    rows = []
                    while rs_express.next():
                        rows.append(rs_express.get_row_data())
                    if rows:
                        df = pd.DataFrame(rows, columns=rs_express.fields)
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
            except:
                pass

            # ★ 构造完整记录，显式包含所有列，缺失的用 None
            record = {
                'code': code,
                'name': name,
                'pub_date': growth_data.get('pub_date', ''),
                'stat_date': growth_data.get('stat_date', f"{year}-{quarter*3:02d}-31"),
                'net_profit_yoy': growth_data.get('net_profit_yoy'),
                'revenue_yoy': None,
                'yoy_equity': growth_data.get('yoy_equity'),
                'yoy_asset': growth_data.get('yoy_asset'),
                'yoy_eps': growth_data.get('yoy_eps'),
                'yoy_pni': growth_data.get('yoy_pni'),
                'net_profit': profit_data.get('net_profit'),
                'roe_avg': profit_data.get('roe_avg'),
                'gp_margin': profit_data.get('gp_margin'),
                'express_gryoy': express_data.get('express_gryoy'),
                'express_opyoy': express_data.get('express_opyoy'),
                'express_pub_date': express_data.get('express_pub_date', ''),
                'express_stat_date': express_data.get('express_stat_date', '')
            }
            return pd.DataFrame([record]), None

        except (BrokenPipeError, ConnectionError, OSError, socket.timeout) as e:
            print(f"  ⚠ {code} {name} 网络异常 ({e})，准备重连...", file=sys.stderr)
            reconnect_baostock()
            continue
        except Exception as e:
            return None, f"{type(e).__name__}: {e}"
    return None, "多次重试后仍失败"


def batch_write_safe(conn, df_list):
    """临时表 + INSERT OR REPLACE 批量写入"""
    if not df_list:
        return
    # 拼接所有 DataFrame
    all_df = pd.concat(df_list, ignore_index=True)
    # ★ 确保所有列都存在，缺失的填充 None
    for col in ALL_COLUMNS:
        if col not in all_df.columns:
            all_df[col] = None
    # 只保留需要的列并按顺序排列
    all_df = all_df[ALL_COLUMNS]
    # 写入临时表
    all_df.to_sql('financial_temp', conn, if_exists='replace', index=False)
    # 执行 INSERT OR REPLACE
    conn.execute('''
        INSERT OR REPLACE INTO financial (
            code, name, pub_date, stat_date,
            net_profit_yoy, revenue_yoy,
            yoy_equity, yoy_asset, yoy_eps, yoy_pni,
            net_profit, roe_avg, gp_margin,
            express_gryoy, express_opyoy,
            express_pub_date, express_stat_date
        )
        SELECT
            code, name, pub_date, stat_date,
            net_profit_yoy, revenue_yoy,
            yoy_equity, yoy_asset, yoy_eps, yoy_pni,
            net_profit, roe_avg, gp_margin,
            express_gryoy, express_opyoy,
            express_pub_date, express_stat_date
        FROM financial_temp
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

    # 构建任务列表
    tasks = []
    skipped = 0
    for code, name in codes:
        if full_mode:
            for y in [CURRENT_YEAR - 1, CURRENT_YEAR]:
                for q in QUARTERS:
                    if q == 1:
                        q_end = f"{y}-03-31"
                    elif q == 2:
                        q_end = f"{y}-06-30"
                    elif q == 3:
                        q_end = f"{y}-09-30"
                    else:
                        q_end = f"{y}-12-31"
                    if pd.Timestamp(q_end) > pd.Timestamp.now():
                        continue
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

    print(f"主线程直接执行，请求间隔 {REQUEST_MIN_DELAY}~{REQUEST_MAX_DELAY} 秒")
    print(f"预计耗时 {len(tasks) * (REQUEST_MIN_DELAY + REQUEST_MAX_DELAY) / 2 / 60:.1f} 分钟")

    # 主循环顺序处理
    success = 0
    failed_stocks = {}
    batch_buffer = []
    start_time = time.time()

    for idx, (code, name, year, quarter) in enumerate(tasks):
        df, err_msg = download_single(code, name, year, quarter)

        if df is not None:
            batch_buffer.append(df)
            success += 1
            if len(batch_buffer) >= 50:
                batch_write_safe(conn, batch_buffer)
                batch_buffer = []
                print(f"  已完成 {success}/{len(tasks)} 个季度")
        else:
            failed_stocks[code] = err_msg
            print(f"  ✗ {code} {name} 失败: {err_msg}")

        if (idx + 1) % 200 == 0:
            elapsed = time.time() - start_time
            print(f"  已处理 {idx+1}/{len(tasks)}，已用时 {elapsed/60:.1f} 分钟")

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

    bs.logout()
    print("下载结束。")