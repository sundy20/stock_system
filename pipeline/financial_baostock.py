#!/usr/bin/env python3
"""
baostock 财务数据下载 —— v3.1
- 增量模式：python3 pipeline/financial_baostock.py   (自动跳过已有数据)
- 全量模式：python3 pipeline/financial_baostock.py --full
- 单线程顺序，请求间隔 0.1s
- express 按 (code, year) 缓存
- revenue_yoy：baostock 不提供单季度营收，置 NULL
- 网络错误自动重连 + 重试（MAX_RETRY=3），baostock API 错误也走重试
"""

import warnings
import baostock as bs
import sqlite3
import pandas as pd
from datetime import datetime
import time, sys, socket, random, os

# ── 项目路径 ──
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db import schema as db_schema

warnings.filterwarnings('ignore', category=FutureWarning)

DB_PATH = 'stocks_2y.db'
REQUEST_DELAY = 0.1
MAX_RETRY = 3
SOCKET_TIMEOUT = 180
CURRENT_YEAR = datetime.now().year
QUARTERS = [1, 2, 3, 4]

# ── 工具 ──

class BlacklistError(Exception):
    """被 baostock 封禁，不可重试"""


def reconnect_baostock():
    try:
        bs.logout()
    except:
        pass
    wait = random.uniform(10, 20)
    print(f"  ⚠ 网络异常，{wait:.1f}s 后重连...", file=sys.stderr)
    time.sleep(wait)
    lg = bs.login()
    if lg.error_code != '0':
        if '黑名单' in str(lg.error_msg) or '10001011' in str(lg.error_code):
            raise BlacklistError(f"IP已被baostock封禁: {lg.error_msg}")
        raise RuntimeError(f"重连失败: {lg.error_msg}")
    socket.setdefaulttimeout(SOCKET_TIMEOUT)


def is_quarter_available(year, quarter):
    if quarter == 1:
        deadline = pd.Timestamp(year=year, month=5, day=5)
    elif quarter == 2:
        deadline = pd.Timestamp(year=year, month=9, day=5)
    elif quarter == 3:
        deadline = pd.Timestamp(year=year, month=11, day=5)
    else:
        deadline = pd.Timestamp(year=year + 1, month=5, day=5)
    return pd.Timestamp.now() >= deadline


def _quarter_stat_date(year, quarter):
    month = quarter * 3
    day = 30 if month in (6, 9) else 31
    return f"{year}-{month:02d}-{day}"


def get_mainboard_codes(conn):
    df = pd.read_sql("SELECT code, name FROM stock_basic WHERE code != 'sh.000300'", conn)
    return list(zip(df['code'], df['name']))


# ── 单股下载 ──

def download_single(code, name, year, quarter, express_cache):
    time.sleep(REQUEST_DELAY)

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
                        'yoyni':        'net_profit_yoy',
                        'yoyequity':    'yoy_equity',
                        'yoyasset':     'yoy_asset',
                        'yoyepsbasic':  'yoy_eps',
                        'yoypni':       'yoy_pni',
                    }
                    for orig, target in col_map.items():
                        if orig in fields:
                            growth_data[target] = pd.to_numeric(
                                df[rs.fields[fields.index(orig)]], errors='coerce').iloc[0]
                    pub_d = next((f for f in rs.fields if 'pubdate' in f.lower()), None)
                    stat_d = next((f for f in rs.fields if 'statdate' in f.lower()), None)
                    if stat_d:
                        growth_data['stat_date'] = str(pd.to_datetime(df[stat_d].iloc[0]).date())
                        growth_data['pub_date'] = str(pd.to_datetime(df[pub_d].iloc[0]).date()) if pub_d else ''
            else:
                raise ConnectionError(f"growth error {rs.error_code}: {rs.error_msg}")

            if not growth_data:
                return None, "成长能力无数据"

            # 2. 盈利能力（可选）
            try:
                rsp = bs.query_profit_data(code, year=year, quarter=quarter)
                if rsp.error_code == '0':
                    rows = []
                    while rsp.next():
                        rows.append(rsp.get_row_data())
                    if rows:
                        dfp = pd.DataFrame(rows, columns=rsp.fields)
                        for src, dst in [('netProfit', 'net_profit'), ('roeAvg', 'roe_avg'),
                                         ('gpMargin', 'gp_margin')]:
                            if src in rsp.fields:
                                profit_data[dst] = pd.to_numeric(dfp[src].iloc[0], errors='coerce')
            except:
                pass

            # 3. 业绩快报（缓存）
            ck = (code, year)
            if ck in express_cache:
                express_df = express_cache[ck]
            else:
                express_df = None
                try:
                    rse = bs.query_performance_express_report(
                        code, start_date=f"{year}-01-01", end_date=f"{year+1}-12-31")
                    if rse.error_code == '0':
                        rows = []
                        while rse.next():
                            rows.append(rse.get_row_data())
                        if rows:
                            express_df = pd.DataFrame(rows, columns=rse.fields)
                except:
                    pass
                express_cache[ck] = express_df

            if express_df is not None and not express_df.empty:
                target_end = _quarter_stat_date(year, quarter)
                mask = express_df['performanceExpStatDate'] == target_end
                if mask.any():
                    latest = express_df[mask].iloc[-1]
                    for src, dst in [('performanceExpressGRYOY', 'express_gryoy'),
                                     ('performanceExpressOPYOY', 'express_opyoy')]:
                        if src in express_df.columns:
                            express_data[dst] = pd.to_numeric(latest[src], errors='coerce')
                    express_data['express_pub_date'] = latest.get('performanceExpPubDate', '')
                    express_data['express_stat_date'] = latest.get('performanceExpStatDate', '')

            # 4. 营收增长率（baostock 不提供单季度营收，无法计算）
            revenue_yoy = None

            record = {
                'code': code, 'name': name,
                'pub_date': growth_data.get('pub_date', ''),
                'stat_date': growth_data.get('stat_date', _quarter_stat_date(year, quarter)),
                'net_profit_yoy': growth_data.get('net_profit_yoy'),
                'revenue_yoy': revenue_yoy,
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
                'express_stat_date': express_data.get('express_stat_date', ''),
            }
            return pd.DataFrame([record]), None

        except BlacklistError:
            raise  # 黑名单不可重试，直接向上抛
        except (BrokenPipeError, ConnectionError, OSError, socket.timeout) as e:
            # 检查是否黑名单（baostock 在黑名单后也报 BrokenPipe）
            if '黑名单' in str(e) or '10001011' in str(e):
                raise BlacklistError(f"下载被拒: {e}")
            print(f"  ⚠ {code} {name} 网络异常 ({e})，重连重试({retry+1}/{MAX_RETRY+1})...",
                  file=sys.stderr)
            try:
                reconnect_baostock()
            except BlacklistError:
                raise
            except Exception:
                pass
            continue
        except Exception as e:
            return None, f"{type(e).__name__}: {e}"
    return None, f"网络重试{MAX_RETRY}次后仍失败"


# ── 主流程 ──

if __name__ == '__main__':
    full_mode = '--full' in sys.argv
    print(f"登录 baostock ... （{'全量重新下载' if full_mode else '智能增量更新'}）")

    lg = bs.login()
    if lg.error_code != '0':
        print(f"登录失败: {lg.error_msg}")
        sys.exit(1)
    # baostock 内部已打印 "login success!"，不重复输出

    conn = sqlite3.connect(DB_PATH)
    db_schema.init_all_tables(conn)
    db_schema.init_db_pragmas(conn)

    codes = get_mainboard_codes(conn)
    total = len(codes)

    # ── 任务构建 ──
    tasks = []           # 需要完整下载的季度（缺失或数据不全）
    skipped = 0

    if full_mode:
        for code, name in codes:
            for y in [CURRENT_YEAR - 1, CURRENT_YEAR]:
                for q in QUARTERS:
                    if is_quarter_available(y, q):
                        tasks.append((code, name, y, q))
    else:
        min_date = f"{CURRENT_YEAR - 2}-01-01"

        # 完整记录：核心字段都存在
        rows = conn.execute("""
            SELECT code, stat_date FROM financial
            WHERE stat_date >= ?
              AND net_profit_yoy IS NOT NULL
        """, (min_date,)).fetchall()

        existing_by_code = {}
        for cd, sd in rows:
            existing_by_code.setdefault(cd, set()).add(sd)

        for code, name in codes:
            existing = existing_by_code.get(code, set())
            missing_count = 0
            for year in (CURRENT_YEAR - 1, CURRENT_YEAR):
                for quarter in QUARTERS:
                    if not is_quarter_available(year, quarter):
                        continue
                    sd = _quarter_stat_date(year, quarter)
                    if sd not in existing:
                        # 完全不存在 → 完整下载
                        tasks.append((code, name, year, quarter))
                        missing_count += 1
            if missing_count == 0:
                skipped += 1

    print(f"共 {total} 只股票")
    print(f"  需下载: {len(tasks)} 个季度（缺失或不完整）")
    print(f"  已跳过: {skipped} 只")

    if not tasks:
        print("全部数据已是最新，无需下载。")
        bs.logout()
        conn.close()
        sys.exit(0)

    # 预估耗时
    total_est = len(tasks) * (REQUEST_DELAY + 0.15) / 60
    print(f"预估总耗时: {total_est:.0f}~{total_est*2:.0f} 分钟")

    success = 0
    failed_stocks = {}
    batch_buffer = []
    express_cache = {}
    start_time = time.time()

    blacklisted = False
    try:
        for idx, (code, name, year, quarter) in enumerate(tasks):
            try:
                df, err_msg = download_single(code, name, year, quarter, express_cache)
            except BlacklistError as e:
                print(f"\n⛔ {e}")
                print(f"  已成功 {success}/{len(tasks)}，进度已保存。请等待解封后重新运行增量更新。")
                blacklisted = True
                break

            if df is not None:
                batch_buffer.append(df)
                success += 1
                if len(batch_buffer) >= 50:
                    db_schema.safe_batch_write(conn, batch_buffer, 'financial', db_schema.FINANCIAL_COLUMNS)
                    batch_buffer = []
                    print(f"  已完成 {success}/{len(tasks)} 个季度")
            else:
                failed_stocks[code] = err_msg
                print(f"  ✗ {code} {name} 失败: {err_msg}")

            if (idx + 1) % 200 == 0:
                elapsed = time.time() - start_time
                eta = elapsed / (idx + 1) * (len(tasks) - idx - 1) if idx > 0 else 0
                print(f"  已处理 {idx+1}/{len(tasks)}，耗时 {elapsed/60:.1f}分, ETA {eta/60:.1f}分, "
                      f"express命中 {sum(1 for v in express_cache.values() if v is not None)}")
    except BlacklistError as e:
        print(f"\n⛔ {e}")
        print(f"  已成功 {success}/{len(tasks)}，进度已保存。请等待解封后重新运行增量更新。")
        blacklisted = True

    # 保存已下载数据（即使被黑名单中断）
    if batch_buffer:
        db_schema.safe_batch_write(conn, batch_buffer, 'financial', db_schema.FINANCIAL_COLUMNS)

    db_schema.checkpoint_db(conn)
    conn.commit()
    conn.close()

    print(f"\n===== 汇总 =====")
    print(f"季度下载: 成功 {success}/{len(tasks)} 个季度" if tasks else "季度下载: 无任务")
    if failed_stocks:
        print(f"失败股票 {len(failed_stocks)} 只，详情 → failed_financial.txt")
        with open('failed_financial.txt', 'w') as f:
            for cd, reason in failed_stocks.items():
                f.write(f"{cd},{reason}\n")
        print("前10条:")
        for cd, reason in list(failed_stocks.items())[:10]:
            print(f"  {cd}: {reason}")

    bs.logout()
    elapsed = time.time() - start_time
    print(f"下载结束，总耗时 {elapsed/60:.1f} 分钟")
