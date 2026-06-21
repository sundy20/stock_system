#!/usr/bin/env python3
"""
baostock 日线数据下载（前复权）—— 最终稳定版（主线程直接执行，抑制警告）
- 默认增量模式：仅下载缺失数据（与数据库最新交易日的差距）
- 全量模式：python3 download_2years.py --full  强制从2018年起全量重新下载
- 自动过滤 ST / 退市 / 长期停牌（最新交易日距今>60天）股票
- 保存字段：open,high,low,close,volume,amount(元),pct_chg(%),turn(%),pre_close
- 单线程直接顺序执行，请求间隔 0.2~1.0 秒随机，避免服务器压力
- 使用临时表 + INSERT OR REPLACE 写入
- 股票列表自动更新到 stock_basic 表（含行业、上市日期）
- 失败股票输出到 failed_daily_bs.txt，附带具体异常信息
- 内置断网重连机制，网络异常时自动恢复
- 抑制 pandas FutureWarning，保持输出界面整洁
"""
import warnings
import os, sys, time, sqlite3, pandas as pd, random, socket
from datetime import datetime, timedelta
import baostock as bs

# 忽略 pandas 未来版本警告，保持输出界面整洁
warnings.filterwarnings('ignore', category=FutureWarning)

# ===================== 配置 =====================
DB_PATH = 'stocks_2y.db'
FULL_START_DATE = '2018-01-01'        # 全量下载的起始日期
REQUEST_MIN_DELAY = 0.2               # 每次请求前最小随机等待（秒）
REQUEST_MAX_DELAY = 1.0               # 最大随机等待（秒）
INACTIVE_DAYS = 60                    # 退市判定：最新交易日距今超过此天数即过滤
MAX_RETRY = 2                         # 单只股票最大重试次数
SOCKET_TIMEOUT = 60                   # 底层Socket超时（秒）

# 需要写入 daily 表的所有列
DAILY_COLUMNS = ['date', 'open', 'high', 'low', 'close', 'volume', 'amount', 'pct_chg', 'turn', 'pre_close']


def reconnect_baostock():
    """断网重连：登出并重新登录baostock，恢复Socket超时"""
    try:
        bs.logout()
    except:
        pass
    wait = random.uniform(2, 5)
    print(f"  ⚠ 网络异常，{wait:.1f}秒后重连...", file=sys.stderr)
    time.sleep(wait)
    lg = bs.login()
    if lg.error_code != '0':
        raise RuntimeError(f"重连失败: {lg.error_msg}")
    socket.setdefaulttimeout(SOCKET_TIMEOUT)


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


def get_stock_list_baostock(conn):
    """
    从baostock获取最新股票列表（含行业、上市日期），并过滤ST/退市/长期停牌
    返回 DataFrame（code, name, industry, list_date）
    """
    print("  从baostock获取股票列表...")
    rs = bs.query_stock_basic()
    if rs.error_code != '0':
        print(f"  ⚠ 获取股票列表失败: {rs.error_msg}")
        df = pd.read_sql("SELECT code, name, industry, list_date FROM stock_basic", conn)
        if df.empty:
            raise RuntimeError("本地缓存为空，且无法获取列表，请检查网络")
        return df

    data = []
    while rs.next():
        data.append(rs.get_row_data())
    df_all = pd.DataFrame(data, columns=rs.fields)
    # 筛选沪深主板（sh.60xxxx, sz.00xxxx），并排除ST
    mask = ((df_all['code'].str.startswith('sh.60')) | (df_all['code'].str.startswith('sz.00'))) & \
           (~df_all['code_name'].str.contains('ST'))
    df = df_all[mask].copy()
    df.rename(columns={'code_name': 'name', 'ipoDate': 'list_date'}, inplace=True)

    # 获取行业信息
    try:
        rs_ind = bs.query_stock_industry()
        ind_data = []
        while rs_ind.next():
            ind_data.append(rs_ind.get_row_data())
        df_ind = pd.DataFrame(ind_data, columns=rs_ind.fields)
        df_ind = df_ind.sort_values('updateDate').groupby('code').tail(1)
        ind_map = df_ind.set_index('code')['industry'].to_dict()
        df['industry'] = df['code'].map(ind_map).fillna('')
    except:
        df['industry'] = ''

    df = df[['code', 'name', 'industry', 'list_date']]
    df.to_sql('stock_basic', conn, if_exists='replace', index=False)
    conn.commit()
    print(f"  ✓ 获取 {len(df)} 只股票，缓存已更新")

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
    return df


def get_latest_date(conn, code):
    """查询某只股票在 daily 表中的最新日期，若无返回 None"""
    row = conn.execute("SELECT MAX(date) FROM daily WHERE code=?", (code,)).fetchone()
    return row[0] if row and row[0] else None


def get_market_latest_date(conn):
    """获取数据库中所有股票的最大日期，作为最近交易日"""
    row = conn.execute("SELECT MAX(date) FROM daily").fetchone()
    if row and row[0]:
        return row[0]
    return FULL_START_DATE


def download_one_baostock(code, name, start_date, end_date):
    """
    下载单只股票日线数据（主线程直接调用）
    返回 (DataFrame or None, error_msg)
    """
    # ★ 每次请求前随机等待，减轻服务器压力
    time.sleep(random.uniform(REQUEST_MIN_DELAY, REQUEST_MAX_DELAY))

    for retry in range(MAX_RETRY + 1):
        try:
            rs = bs.query_history_k_data_plus(
                code,
                "date,open,high,low,close,preclose,volume,amount,turn,tradestatus",
                start_date=start_date.replace('-', ''),
                end_date=end_date.replace('-', ''),
                frequency="d",
                adjustflag="2"  # 前复权
            )
            if rs.error_code != '0':
                return None, f"查询失败: {rs.error_msg}"
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
            if not rows:
                return None, "无数据返回"
            df = pd.DataFrame(rows, columns=rs.fields)
            # 过滤停牌日
            df = df[df['tradestatus'] == '1'].copy()
            if df.empty:
                return None, "全部停牌或无交易"
            # 类型转换
            for col in ['open', 'high', 'low', 'close', 'preclose', 'volume', 'amount', 'turn']:
                df[col] = pd.to_numeric(df[col], errors='coerce')
            # 计算涨跌幅
            df['pct_chg'] = ((df['close'] - df['preclose']) / df['preclose'] * 100).round(2)
            df['date'] = pd.to_datetime(df['date']).dt.strftime('%Y-%m-%d')
            df['volume'] = df['volume'].astype(int)
            df['amount'] = df['amount'].astype(float)
            df['turn'] = df['turn'].astype(float)
            df['pre_close'] = df['preclose']
            df = df[DAILY_COLUMNS]
            df['code'] = code
            return df, None

        except (BrokenPipeError, ConnectionError, OSError, socket.timeout) as e:
            print(f"  ⚠ {code} {name} 网络异常 ({e})，重连并重试...", file=sys.stderr)
            reconnect_baostock()
            continue
        except Exception as e:
            return None, f"{type(e).__name__}: {e}"
    return None, "多次重试后失败"


def safe_batch_write(conn, df_list):
    """临时表 + INSERT OR REPLACE 批量写入（自动补齐缺失列）"""
    if not df_list:
        return
    all_df = pd.concat(df_list, ignore_index=True)
    # 确保所有需要的列都存在，缺失的填充 None
    for col in DAILY_COLUMNS + ['code']:
        if col not in all_df.columns:
            all_df[col] = None
    # 写入临时表
    all_df.to_sql('daily_temp', conn, if_exists='replace', index=False)
    # 执行 INSERT OR REPLACE
    conn.execute('''
        INSERT OR REPLACE INTO daily (code, date, name, open, high, low, close,
                                      volume, amount, pct_chg, turn, pre_close)
        SELECT code, date, name, open, high, low, close,
               volume, amount, pct_chg, turn, pre_close FROM daily_temp
    ''')
    conn.execute("DROP TABLE IF EXISTS daily_temp")
    conn.commit()


if __name__ == '__main__':
    full_mode = '--full' in sys.argv
    start_msg = "全量重新下载" if full_mode else "智能增量更新"
    print(f"baostock 日线下载：{start_msg}模式")

    # ★ 主线程登录一次，全程不登出
    lg = bs.login()
    if lg.error_code != '0':
        print(f"主线程登录失败: {lg.error_msg}")
        sys.exit(1)
    print("login success!")

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    # 1. 获取活跃股票列表（含行业、上市日期）
    stock_info = get_stock_list_baostock(conn)
    stock_info = pd.concat([stock_info, pd.DataFrame([{
        'code': 'sh.000300', 'name': '沪深300', 'industry': '', 'list_date': ''
    }])], ignore_index=True)

    # 2. 获取市场最近交易日
    market_last_date = get_market_latest_date(conn)
    print(f"  市场最近交易日: {market_last_date}")

    # 3. 构建任务列表
    task_list = []
    skipped = 0
    today_str = datetime.now().strftime('%Y-%m-%d')
    for _, row in stock_info.iterrows():
        code = row['code']
        name = row['name']
        if full_mode:
            task_list.append((code, name, FULL_START_DATE, today_str))
        else:
            last_date = get_latest_date(conn, code)
            if last_date is None:
                task_list.append((code, name, FULL_START_DATE, today_str))
            elif last_date < market_last_date:
                next_date = (pd.to_datetime(last_date) + timedelta(days=1)).strftime('%Y-%m-%d')
                task_list.append((code, name, next_date, today_str))
            else:
                skipped += 1

    print(f"共需处理 {len(task_list)} 只股票，已跳过 {skipped} 只最新股票")
    if not task_list:
        print("全部数据已是最新，无需下载。")
        bs.logout()
        conn.close()
        sys.exit(0)

    print(f"主线程直接执行，请求间隔 {REQUEST_MIN_DELAY}~{REQUEST_MAX_DELAY} 秒")
    print(f"预计耗时 {len(task_list) * (REQUEST_MIN_DELAY + REQUEST_MAX_DELAY) / 2 / 60:.1f} 分钟")

    # ★ 主循环顺序处理（不用线程池）
    success = 0
    failed_dict = {}
    batch_buffer = []
    start_time = time.time()

    for idx, (code, name, start_d, end_d) in enumerate(task_list):
        df, err_msg = download_one_baostock(code, name, start_d, end_d)

        if df is not None:
            df['name'] = name
            batch_buffer.append(df)
            success += 1
            if len(batch_buffer) >= 50:
                safe_batch_write(conn, batch_buffer)
                batch_buffer = []
                print(f"  已完成 {success}/{len(task_list)} 只")
        else:
            failed_dict[code] = err_msg
            print(f"  ✗ {code} {name} 失败: {err_msg}")

        if (idx + 1) % 100 == 0:
            elapsed = time.time() - start_time
            print(f"  已处理 {idx+1}/{len(task_list)}，已用时 {elapsed/60:.1f} 分钟")

    if batch_buffer:
        safe_batch_write(conn, batch_buffer)
    conn.commit()
    conn.close()

    print(f"\n下载完成，成功 {success}/{len(task_list)} 只")
    if failed_dict:
        print(f"失败 {len(failed_dict)} 只，详情写入 failed_daily_bs.txt")
        with open('failed_daily_bs.txt', 'w') as f:
            for code, reason in failed_dict.items():
                f.write(f"{code},{reason}\n")
        print("前10条失败示例:")
        for code, reason in list(failed_dict.items())[:10]:
            print(f"  {code}: {reason}")

    bs.logout()