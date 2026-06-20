#!/usr/bin/env python3
"""
tushare 日线数据下载（前复权）—— 全字段版，2018年起
- 含上市日期、行业信息，自动过滤退市/长期停牌股
- 保存字段：open,high,low,close,volume,amount,pct_chg,turn,pre_close
"""
import os, time, sqlite3, pandas as pd
from datetime import datetime, timedelta
import tushare as ts

TOKEN = os.getenv('TUSHARE_TOKEN')
if not TOKEN:
    raise RuntimeError("请先设置 TUSHARE_TOKEN")
ts.set_token(TOKEN)
pro = ts.pro_api()

DB_PATH = 'stocks_2y.db'
START_DATE = '20180101'
END_DATE = datetime.now().strftime('%Y%m%d')
CALL_PER_MIN = 50
SLEEP_SEC = 60 / CALL_PER_MIN
INACTIVE_DAYS = 60          # 超60天无交易视为退市/停牌


def init_db(conn):
    """初始化数据库表结构（若不存在则创建）"""
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA cache_size = -20000;")
    conn.execute('''CREATE TABLE IF NOT EXISTS daily (
                                                         code TEXT, date TEXT, name TEXT,
                                                         open REAL, high REAL, low REAL, close REAL,
                                                         volume REAL, amount REAL, pct_chg REAL, turn REAL, pre_close REAL,
                                                         PRIMARY KEY (code, date))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS stock_basic (
                                                               code TEXT PRIMARY KEY, name TEXT, industry TEXT, list_date TEXT)''')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_daily_code_date ON daily(code, date);")
    conn.commit()


def get_stock_list_smart(conn):
    """
    获取活跃股票列表，含行业和上市日期，并过滤退市/停牌股
    要求：主板、非ST、最新交易日距今≤INACTIVE_DAYS
    """
    try:
        print("  从tushare获取最新股票列表...")
        stocks = pro.stock_basic(exchange='', list_status='L',
                                 fields='ts_code,name,industry,list_date')
        mask = (stocks['ts_code'].str.match(r'^(60|00)')) & (~stocks['name'].str.contains('ST'))
        df = stocks[mask].copy()
        df['code'] = df['ts_code'].apply(
            lambda x: 'sh.' + x[:6] if x.endswith('.SH') else 'sz.' + x[:6])
        df = df[['code', 'name', 'industry', 'list_date']]
        df.to_sql('stock_basic', conn, if_exists='replace', index=False)
        conn.commit()
        print(f"  ✓ 获取 {len(df)} 只股票")
    except Exception as e:
        print(f"  ⚠ 获取失败 ({e})，使用本地缓存")
        df = pd.read_sql("SELECT code, name, industry, list_date FROM stock_basic", conn)
        if df.empty:
            raise RuntimeError("本地缓存为空，请检查网络")

    # 过滤退市/长期停牌
    cutoff = (datetime.now() - timedelta(days=INACTIVE_DAYS)).strftime('%Y-%m-%d')
    last_dates = pd.read_sql("""
                             SELECT code, MAX(date) as last_date FROM daily
                             WHERE code IN (SELECT code FROM stock_basic) GROUP BY code
                             """, conn)
    active = last_dates[last_dates['last_date'] >= cutoff]['code'].tolist()
    removed = df[~df['code'].isin(active)]
    if not removed.empty:
        print(f"  ⚠ 过滤掉 {len(removed)} 只退市/停牌股: {removed['code'].tolist()}")
    df = df[df['code'].isin(active)]
    print(f"  ✓ 最终活跃股票 {len(df)} 只")
    return list(zip(df['code'], df['name']))


def download_one(code):
    """下载单只股票全部日线数据"""
    ts_code = code[3:] + '.SH' if code.startswith('sh.') else code[3:] + '.SZ'
    if code == 'sh.000300':
        ts_code = '000300.SH'
    try:
        df = pro.daily(ts_code=ts_code, start_date=START_DATE, end_date=END_DATE, adj='qfq')
        if df.empty:
            return None
        df = df.rename(columns={'trade_date': 'date', 'vol': 'volume'})
        df['date'] = pd.to_datetime(df['date'], format='%Y%m%d').dt.strftime('%Y-%m-%d')
        df['volume'] = (df['volume'] * 100).astype(int)          # 手->股
        df['amount'] = (df['amount'] * 1000).astype(float)       # 千元->元
        df['pct_chg'] = df['pct_chg'].astype(float)
        df['turn'] = 0.0
        df = df[['date', 'open', 'high', 'low', 'close', 'volume', 'amount', 'pct_chg', 'turn', 'pre_close']]
        df['code'] = code
        return df
    except Exception:
        return None


def safe_batch_write(conn, df_list):
    if not df_list:
        return
    all_df = pd.concat(df_list, ignore_index=True)
    all_df.to_sql('daily_temp', conn, if_exists='replace', index=False)
    conn.execute('''
        INSERT OR REPLACE INTO daily (code, date, name, open, high, low, close,
                                      volume, amount, pct_chg, turn, pre_close)
        SELECT code, date, name, open, high, low, close,
               volume, amount, pct_chg, turn, pre_close FROM daily_temp
    ''')
    conn.execute("DROP TABLE IF EXISTS daily_temp")
    conn.commit()


if __name__ == '__main__':
    print(f"tushare 日线下载：{START_DATE} 至 {END_DATE}")
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    codes = get_stock_list_smart(conn)
    codes.append(('sh.000300', '沪深300'))

    print(f"共 {len(codes)} 只标的，限速 {CALL_PER_MIN} 次/分钟")
    success, failed, batch = 0, [], []
    last_time = time.time()
    for code, name in codes:
        elapsed = time.time() - last_time
        if elapsed < SLEEP_SEC:
            time.sleep(SLEEP_SEC - elapsed)
        last_time = time.time()

        df = download_one(code)
        if df is not None and not df.empty:
            df['name'] = name
            batch.append(df)
            success += 1
            if len(batch) >= 50:
                safe_batch_write(conn, batch)
                batch = []
                print(f"  已完成 {success}/{len(codes)} 只")
        else:
            failed.append(code)

        if (success + len(failed)) % 200 == 0:
            print(f"  已处理 {success+len(failed)}/{len(codes)}")

    if batch:
        safe_batch_write(conn, batch)

    conn.close()
    print(f"下载完成，成功 {success}/{len(codes)} 只")
    if failed:
        print(f"失败 {len(failed)} 只，已写入 failed_daily.txt")
        with open('failed_daily.txt', 'w') as f:
            f.write('\n'.join(failed))