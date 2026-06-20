#!/usr/bin/env python3
"""
tushare 日线数据下载（前复权）—— 全字段版，2018年起
- Token 从环境变量读取
- 全量覆盖 2018-01-01 至今，使用临时表 + INSERT OR REPLACE 避免主键冲突
- 限速 50 次/分钟，稳定运行
- 保存字段：open, high, low, close, volume, amount, pct_chg, turn, pre_close
- 股票信息表 stock_basic 同时保存名称和行业（申万）
- 自动过滤退市/长期停牌股票（最新交易日距今超过60个自然日）
"""
import os, time, sqlite3, pandas as pd
from datetime import datetime, timedelta
import tushare as ts

# ===================== 配置 =====================
TOKEN = os.getenv('TUSHARE_TOKEN')
if not TOKEN:
    raise RuntimeError("请先执行: export TUSHARE_TOKEN='你的token'")
ts.set_token(TOKEN)
pro = ts.pro_api()

DB_PATH = 'stocks_2y.db'
START_DATE = '20180101'                     # 数据起始日期（2018年1月1日）
END_DATE = datetime.now().strftime('%Y%m%d') # 至今日
CALL_PER_MIN = 50                           # 每分钟最大调用次数
SLEEP_SEC = 60 / CALL_PER_MIN               # 每次请求最小间隔（秒）
INACTIVE_DAYS = 60                          # 最近交易日距今超过此天数即视为退市/长期停牌，自动过滤


def init_db(conn):
    """初始化数据库：WAL模式，创建表和索引（若不存在则建表）"""
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA cache_size = -20000;")
    # 日线表，包含所有扩展字段
    conn.execute('''CREATE TABLE IF NOT EXISTS daily (
                                                         code TEXT, date TEXT, name TEXT,
                                                         open REAL, high REAL, low REAL, close REAL,
                                                         volume REAL, amount REAL, pct_chg REAL, turn REAL, pre_close REAL,
                                                         PRIMARY KEY (code, date))''')
    # 股票基本信息表（含行业）
    conn.execute('''CREATE TABLE IF NOT EXISTS stock_basic (
                                                               code TEXT PRIMARY KEY, name TEXT, industry TEXT)''')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_daily_code_date ON daily(code, date);")
    conn.commit()


def get_stock_list_smart(conn):
    """
    获取股票列表，并自动过滤退市/长期停牌股票
    1. 尝试从 tushare 获取最新列表（含行业），成功则更新缓存并返回
    2. 失败则使用本地 stock_basic 表
    过滤规则：
        - 仅沪深主板（60xxxx/00xxxx），且非 ST
        - 最新交易日距今超过 INACTIVE_DAYS 天的股票将被剔除
    """
    try:
        print("  正在从 tushare 获取最新股票列表（含行业）...")
        # 获取股票基本信息，包含行业
        stocks = pro.stock_basic(exchange='', list_status='L', fields='ts_code,name,industry')
        # 保留主板且非ST
        mask = (stocks['ts_code'].str.match(r'^(60|00)')) & (~stocks['name'].str.contains('ST'))
        df = stocks[mask].copy()
        df['code'] = df['ts_code'].apply(
            lambda x: 'sh.' + x[:6] if x.endswith('.SH') else 'sz.' + x[:6])
        df = df[['code', 'name', 'industry']]
        # 更新本地缓存
        df.to_sql('stock_basic', conn, if_exists='replace', index=False)
        conn.commit()
        print(f"  ✓ 成功获取 {len(df)} 只股票，缓存已更新")
    except Exception as e:
        print(f"  ⚠ 实时获取失败 ({e})，回退到本地缓存 ...")
        df = pd.read_sql("SELECT code, name, industry FROM stock_basic", conn)
        if df.empty:
            raise RuntimeError("本地缓存为空，且实时获取失败，请稍后重试或检查网络。")
        print(f"  ✓ 使用本地缓存，共 {len(df)} 只股票")

    # ---------- 过滤退市/长期停牌股票 ----------
    cutoff_date = (datetime.now() - timedelta(days=INACTIVE_DAYS)).strftime('%Y-%m-%d')
    # 从 daily 表获取每只股票的最新交易日
    last_dates = pd.read_sql("""
                             SELECT code, MAX(date) as last_date FROM daily
                             WHERE code IN (SELECT code FROM stock_basic)
                             GROUP BY code
                             """, conn)
    active_codes = last_dates[last_dates['last_date'] >= cutoff_date]['code'].tolist()
    # 输出被过滤的股票
    removed = df[~df['code'].isin(active_codes)]
    if not removed.empty:
        print(f"  ⚠ 过滤掉 {len(removed)} 只退市/长期停牌股票: {removed['code'].tolist()}")
    # 只保留活跃股票
    df = df[df['code'].isin(active_codes)]
    print(f"  ✓ 最终保留 {len(df)} 只活跃股票")
    return list(zip(df['code'], df['name']))


def download_one(code):
    """下载单只股票的前复权日线，返回 DataFrame 或 None"""
    ts_code = code[3:] + '.SH' if code.startswith('sh.') else code[3:] + '.SZ'
    if code == 'sh.000300':  # 沪深300指数特殊处理
        ts_code = '000300.SH'
    try:
        df = pro.daily(ts_code=ts_code, start_date=START_DATE, end_date=END_DATE, adj='qfq')
        if df.empty:
            return None
        # 列名映射及单位转换
        df = df.rename(columns={'trade_date': 'date', 'vol': 'volume'})
        df['date'] = pd.to_datetime(df['date'], format='%Y%m%d').dt.strftime('%Y-%m-%d')
        df['volume'] = (df['volume'] * 100).astype(int)      # 手 → 股
        df['amount'] = (df['amount'] * 1000).astype(float)   # 千元 → 元
        df['pct_chg'] = df['pct_chg'].astype(float)
        df['turn'] = 0.0                                     # pro.daily 不含换手率，填 0 占位
        # 保留所需字段
        df = df[['date', 'open', 'high', 'low', 'close', 'volume', 'amount', 'pct_chg', 'turn', 'pre_close']]
        df['code'] = code
        return df
    except Exception:
        return None


def safe_batch_write(conn, df_list):
    """通过临时表实现 INSERT OR REPLACE，避免主键冲突和变量过多"""
    if not df_list:
        return
    all_df = pd.concat(df_list, ignore_index=True)
    # 写入临时表
    all_df.to_sql('daily_temp', conn, if_exists='replace', index=False)
    # 将临时表内容插入或替换到主表
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

    # 获取股票列表（已自动过滤退市股）
    codes = get_stock_list_smart(conn)
    codes.append(('sh.000300', '沪深300'))  # 添加基准指数

    print(f"共 {len(codes)} 只标的，限速 {CALL_PER_MIN} 次/分钟")

    success, failed, batch = 0, [], []
    last_time = time.time()
    for code, name in codes:
        # 限速控制：保证两次请求间隔不小于 SLEEP_SEC
        elapsed = time.time() - last_time
        if elapsed < SLEEP_SEC:
            time.sleep(SLEEP_SEC - elapsed)
        last_time = time.time()

        df = download_one(code)
        if df is not None and not df.empty:
            df['name'] = name
            batch.append(df)
            success += 1
            if len(batch) >= 50:  # 每50只写入一次，减少数据库压力
                safe_batch_write(conn, batch)
                batch = []
                print(f"  已完成 {success}/{len(codes)} 只，已写入数据库")
        else:
            failed.append(code)

        if (success + len(failed)) % 200 == 0:
            print(f"  已处理 {success+len(failed)}/{len(codes)}")

    if batch:  # 处理剩余不足50只的部分
        safe_batch_write(conn, batch)

    conn.close()
    print(f"下载完成，成功 {success}/{len(codes)} 只")
    if failed:
        print(f"失败 {len(failed)} 只（示例: {failed[:10]}），下次运行自动补全")
        with open('failed_daily.txt', 'w') as f:
            f.write('\n'.join(failed))