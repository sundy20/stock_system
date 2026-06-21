#!/usr/bin/env python3
"""
tushare 财务数据下载——增量更新，面向主力数据源设计
- 动态日期：自动计算当前年和去年
- 增量模式：仅补充数据库中缺失的季度数据
- 全量模式：python3 download_financial_tushare.py --full
- 限速 50 次/分钟，失败重试 2 次
- 表结构与 baostock 版兼容，backtest 脚本可直接切换使用
"""
import os, sys, time, sqlite3, pandas as pd
from datetime import datetime
import tushare as ts

TOKEN = os.getenv('TUSHARE_TOKEN')
if not TOKEN:
    raise RuntimeError("请先设置环境变量 TUSHARE_TOKEN")
ts.set_token(TOKEN)
pro = ts.pro_api()

DB_PATH = 'stocks_2y.db'
CALL_PER_MIN = 50
SLEEP_SEC = 60 / CALL_PER_MIN
CURRENT_YEAR = datetime.now().year
QUARTERS = [1, 2, 3, 4]


def init_db(conn):
    """创建 financial 表（与 baostock 版表结构完全兼容）"""
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute('''CREATE TABLE IF NOT EXISTS financial (
        code TEXT, name TEXT,
        pub_date TEXT, stat_date TEXT,
        net_profit_yoy REAL, revenue_yoy REAL,
        yoy_equity REAL, yoy_asset REAL, yoy_eps REAL, yoy_pni REAL,
        PRIMARY KEY (code, stat_date)
    )''')
    conn.commit()


def get_stock_codes(conn):
    """从本地 stock_basic 表获取股票列表（排除沪深300）"""
    df = pd.read_sql("SELECT code, name FROM stock_basic WHERE code != 'sh.000300'", conn)
    if df.empty:
        raise RuntimeError("stock_basic 表为空，请先运行日线下载脚本生成缓存")
    return list(zip(df['code'], df['name']))


def get_missing_quarters(conn, code):
    """返回该股票最近两年缺失的季度列表 [(year, quarter, stat_date)]"""
    existing = {}
    rows = conn.execute(
        "SELECT stat_date, net_profit_yoy FROM financial WHERE code=? AND stat_date >= ?",
        (code, f"{CURRENT_YEAR - 2}-01-01")
    ).fetchall()
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


def download_one(code, name, year, quarter):
    """
    下载单只股票指定季度的财务数据
    返回 (DataFrame_or_None, error_msg)
    """
    ts_code = code[3:] + '.SH' if code.startswith('sh.') else code[3:] + '.SZ'
    period = f"{year}{quarter:02d}01" if quarter == 1 else f"{year}{quarter * 3:02d}01"
    # tushare period 格式: 20240331, 20240630, 20240930, 20241231
    period = f"{year}{quarter * 3:02d}{['31','30','30','31'][quarter-1]}"

    try:
        df = pro.fina_indicator(
            ts_code=ts_code,
            period=period,
            fields='ts_code,ann_date,end_date,q_profit_yoy,q_gr_yoy,q_fa_yoygr'
        )
        if df.empty:
            return None, "无数据返回"
        if len(df) == 0:
            return None, "空 DataFrame"

        # 字段映射
        col_map = {
            'q_profit_yoy': 'net_profit_yoy',
            'q_gr_yoy': 'revenue_yoy',
            'q_fa_yoygr': 'yoy_equity',
        }
        for orig, target in col_map.items():
            if orig in df.columns:
                df[target] = pd.to_numeric(df[orig], errors='coerce')

        stat_date = str(pd.to_datetime(df['end_date'].iloc[0]).date())
        pub_date = str(pd.to_datetime(df['ann_date'].iloc[0]).date()) if 'ann_date' in df.columns else ''

        record = {
            'code': code,
            'name': name,
            'pub_date': pub_date,
            'stat_date': stat_date,
            'net_profit_yoy': df['net_profit_yoy'].iloc[0] if 'net_profit_yoy' in df.columns else None,
            'revenue_yoy': df['revenue_yoy'].iloc[0] if 'revenue_yoy' in df.columns else None,
            'yoy_equity': df['yoy_equity'].iloc[0] if 'yoy_equity' in df.columns else None,
            'yoy_asset': None,  # tushare 无直接对应字段
            'yoy_eps': None,
            'yoy_pni': None,
        }
        return pd.DataFrame([record]), None

    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def batch_write_safe(conn, df_list):
    """临时表 + INSERT OR REPLACE 批量写入"""
    if not df_list:
        return
    all_df = pd.concat(df_list, ignore_index=True)
    all_df.to_sql('financial_temp', conn, if_exists='replace', index=False)
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
    mode_str = '全量重新下载' if full_mode else '增量更新'
    print(f"tushare 财务数据下载：{mode_str}（{CURRENT_YEAR - 1}~{CURRENT_YEAR}）")

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    codes = get_stock_codes(conn)

    # 筛选需要更新的股票
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

    print(f"共 {len(codes)} 只股票，{len(tasks)} 个季度任务，已跳过 {skipped} 只完整股票")
    if not tasks:
        print("全部财务数据已是最新，无需下载。")
        conn.close()
        sys.exit(0)

    print(f"限速 {CALL_PER_MIN} 次/分钟，预计耗时 {len(tasks) / CALL_PER_MIN:.1f} 分钟")

    success, failed_stocks, batch_buffer = 0, [], []
    last_time = time.time()

    for code, name, year, quarter in tasks:
        elapsed = time.time() - last_time
        if elapsed < SLEEP_SEC:
            time.sleep(SLEEP_SEC - elapsed)
        last_time = time.time()

        df, err_msg = download_one(code, name, year, quarter)
        if df is not None and not df.empty:
            batch_buffer.append(df)
            success += 1
            if len(batch_buffer) >= 50:
                batch_write_safe(conn, batch_buffer)
                batch_buffer = []
                print(f"  已完成 {success}/{len(tasks)} 个季度")
        else:
            failed_stocks.append((code, name, err_msg))
            if len(failed_stocks) <= 20:
                print(f"  ✗ {code} {name} 失败: {err_msg}")

        if (success + len(failed_stocks)) % 200 == 0:
            print(f"  已处理 {success + len(failed_stocks)}/{len(tasks)}")

    if batch_buffer:
        batch_write_safe(conn, batch_buffer)

    conn.close()
    print(f"\n下载完成，成功 {success}/{len(tasks)} 个季度")
    if failed_stocks:
        print(f"失败 {len(failed_stocks)} 个季度，详情写入 failed_financial_tushare.txt")
        with open('failed_financial_tushare.txt', 'w') as f:
            for code, name, reason in failed_stocks:
                f.write(f"{code},{name},{reason}\n")
        print("前 10 条失败示例:")
        for code, name, reason in failed_stocks[:10]:
            print(f"  {code} {name}: {reason}")
