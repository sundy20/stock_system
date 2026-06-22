#!/usr/bin/env python3
"""
tushare 日线数据下载（前复权）—— 最终版，智能增量，面向失败设计
- 默认增量模式：仅下载缺失数据（与数据库最新交易日的差距）
- 全量模式：python3 download_daily_tushare.py --full  强制从2018年起全量重新下载
- 自动过滤 ST / 退市 / 长期停牌（最新交易日距今>60天）股票
- 保存字段：open,high,low,close,volume,amount(元),pct_chg,turn(暂填0),pre_close
- 限速 50次/分钟，使用临时表 + INSERT OR REPLACE 写入
- 股票列表自动更新到 stock_basic 表（含行业、上市日期）
- 失败股票输出到 failed_daily.txt，附带具体异常信息
- 沪深300指数使用 index_daily 接口单独处理
"""
import os, sys, time, sqlite3, pandas as pd
from datetime import datetime, timedelta
import tushare as ts
import sys, os as _sos; sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))); from db import schema as db_schema

TOKEN = os.getenv('TUSHARE_TOKEN')
if not TOKEN:
    raise RuntimeError("请先设置环境变量 TUSHARE_TOKEN")
ts.set_token(TOKEN)
pro = ts.pro_api()

DB_PATH = 'stocks_2y.db'
FULL_START_DATE = '20180101'        # 全量下载的起始日期
CALL_PER_MIN = 50                   # 每分钟最大请求数
SLEEP_SEC = 60 / CALL_PER_MIN       # 两次请求最小间隔（秒）
INACTIVE_DAYS = 60                  # 退市判定：最新交易日距今超过此天数即过滤



def get_stock_list_smart(conn):
    """
    获取活跃股票列表（含行业、上市日期）
    1. 从tushare拉取最新股票列表，失败则用本地缓存
    2. 仅保留沪深主板（60/00开头）且非ST
    3. 过滤退市/长期停牌：最新交易日距今 > INACTIVE_DAYS 的股票被剔除
    返回 DataFrame（code, name, industry, list_date）
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
        print(f"  ✓ 获取 {len(df)} 只股票，缓存已更新")
    except Exception as e:
        print(f"  ⚠ 获取失败 ({e})，使用本地缓存")
        df = pd.read_sql("SELECT code, name, industry, list_date FROM stock_basic", conn)
        if df.empty:
            raise RuntimeError("本地缓存为空，请检查网络")

    # 过滤退市/长期停牌
    # 核心逻辑：有数据但最近无交易 → 过滤；从未下载过（新股）→ 保留
    cutoff = (datetime.now() - timedelta(days=INACTIVE_DAYS)).strftime('%Y-%m-%d')
    last_dates = pd.read_sql("""
                             SELECT code, MAX(date) as last_date FROM daily
                             WHERE code IN (SELECT code FROM stock_basic) GROUP BY code
                             """, conn)
    active_with_data = set(last_dates[last_dates['last_date'] >= cutoff]['code'].tolist())
    codes_with_data   = set(last_dates['code'].tolist())
    codes_without_data = set(df['code'].tolist()) - codes_with_data

    active = active_with_data | codes_without_data
    removed = df[~df['code'].isin(active)]
    if not removed.empty:
        print(f"  ⚠ 过滤掉 {len(removed)} 只退市/停牌股（有数据但最新日>{INACTIVE_DAYS}天前）")
        removed_list = removed['code'].tolist()
        if len(removed_list) <= 20:
            print(f"     {removed_list}")
        else:
            print(f"     {removed_list[:10]} ... 等共{len(removed_list)}只")
    if codes_without_data:
        print(f"  ℹ 保留 {len(codes_without_data)} 只无历史数据的新股（首次下载）")
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


def download_one(code, start_date):
    """
    下载单只股票/指数从 start_date 至今的前复权日线
    返回 (DataFrame, error_msg)
    """
    if code == 'sh.000300':
        # 沪深300指数使用 index_daily 接口
        try:
            df = pro.index_daily(ts_code='000300.SH',
                                 start_date=start_date.replace('-', ''),
                                 end_date=datetime.now().strftime('%Y%m%d'))
            if df.empty:
                return None, "沪深300指数无数据"
            df = df.rename(columns={'trade_date': 'date', 'vol': 'volume'})
            if 'amount' not in df.columns:
                df['amount'] = 0.0
            df['date'] = pd.to_datetime(df['date'], format='%Y%m%d').dt.strftime('%Y-%m-%d')
            df['volume'] = (df['volume'] * 100).astype(int)
            df['amount'] = df['amount'].astype(float)
            df['pct_chg'] = df['pct_chg'].astype(float) if 'pct_chg' in df.columns else 0.0
            df['turn'] = 0.0
            df['pre_close'] = df['pre_close'].astype(float) if 'pre_close' in df.columns else 0.0
            df = df[['date', 'open', 'high', 'low', 'close', 'volume', 'amount', 'pct_chg', 'turn', 'pre_close']]
            df['code'] = code
            return df, None
        except Exception as e:
            return None, f"沪深300指数下载失败: {type(e).__name__}: {e}"
    else:
        ts_code = code[3:] + '.SH' if code.startswith('sh.') else code[3:] + '.SZ'
        try:
            df = pro.daily(ts_code=ts_code,
                           start_date=start_date.replace('-', ''),
                           end_date=datetime.now().strftime('%Y%m%d'),
                           adj='qfq')
        except Exception as e:
            return None, f"{type(e).__name__}: {e}"
        if df.empty:
            return None, f"返回空数据 (可能无此代码或退市)"
        # 统一处理字段
        df = df.rename(columns={'trade_date': 'date', 'vol': 'volume'})
        df['date'] = pd.to_datetime(df['date'], format='%Y%m%d').dt.strftime('%Y-%m-%d')
        df['volume'] = (df['volume'] * 100).astype(int)
        df['amount'] = (df['amount'] * 1000).astype(float)
        df['pct_chg'] = df['pct_chg'].astype(float)
        df['turn'] = 0.0
        df = df[['date', 'open', 'high', 'low', 'close', 'volume', 'amount', 'pct_chg', 'turn', 'pre_close']]
        df['code'] = code
        return df, None




if __name__ == '__main__':
    full_mode = '--full' in sys.argv
    start_msg = "全量重新下载" if full_mode else "智能增量更新"
    print(f"tushare 日线下载：{start_msg}模式")
    conn = sqlite3.connect(DB_PATH)
    db_schema.init_all_tables(conn)
    db_schema.init_db_pragmas(conn)

    # 1. 获取活跃股票列表
    stock_info = get_stock_list_smart(conn)
    # 添加沪深300作为基准
    stock_info = pd.concat([stock_info, pd.DataFrame([{
        'code': 'sh.000300', 'name': '沪深300', 'industry': '', 'list_date': ''
    }])], ignore_index=True)

    # 2. 获取市场最近交易日
    market_last_date = get_market_latest_date(conn)
    print(f"  市场最近交易日: {market_last_date}")

    # 3. 确定每只股票需要下载的起始日期
    task_list = []
    skipped = 0
    for _, row in stock_info.iterrows():
        code = row['code']
        name = row['name']
        if full_mode:
            task_list.append((code, name, FULL_START_DATE))
        else:
            last_date = get_latest_date(conn, code)
            if last_date is None:
                # 没有任何数据，全量下载
                task_list.append((code, name, FULL_START_DATE))
            elif last_date < market_last_date:
                # 数据落后，补最近缺失
                next_date = (pd.to_datetime(last_date) + timedelta(days=1)).strftime('%Y-%m-%d')
                task_list.append((code, name, next_date))
            else:
                skipped += 1

    print(f"共需处理 {len(task_list)} 只股票，已跳过 {skipped} 只最新股票")
    if not task_list:
        print("全部数据已是最新，无需下载。")
        conn.close()
        sys.exit(0)

    print(f"限速 {CALL_PER_MIN} 次/分钟，预计耗时 {len(task_list)/CALL_PER_MIN:.1f} 分钟")

    success, failed_list, batch = 0, [], []
    last_time = time.time()
    for code, name, start_date in task_list:
        elapsed = time.time() - last_time
        if elapsed < SLEEP_SEC:
            time.sleep(SLEEP_SEC - elapsed)
        last_time = time.time()

        df, err_msg = download_one(code, start_date)
        if df is not None and not df.empty:
            df['name'] = name
            batch.append(df)
            success += 1
            if len(batch) >= 50:
                db_schema.safe_batch_write(conn, batch, 'daily', db_schema.DAILY_COLUMNS)
                batch = []
                print(f"  已完成 {success}/{len(task_list)} 只")
        else:
            failed_list.append((code, name, err_msg))
            print(f"  ✗ {code} {name} 失败: {err_msg}")

        if (success + len(failed_list)) % 200 == 0:
            print(f"  已处理 {success+len(failed_list)}/{len(task_list)}")

    if batch:
        db_schema.safe_batch_write(conn, batch, 'daily', db_schema.DAILY_COLUMNS)

    conn.close()
    print(f"\n下载完成，成功 {success}/{len(task_list)} 只")
    if failed_list:
        print(f"失败 {len(failed_list)} 只，详情写入 failed_daily.txt")
        with open('failed_daily.txt', 'w') as f:
            for code, name, reason in failed_list:
                f.write(f"{code},{name},{reason}\n")
        print("前10条失败示例:")
        for code, name, reason in failed_list[:10]:
            print(f"  {code} {name}: {reason}")