#!/usr/bin/env python3
"""
baostock 财务数据下载 —— v2.0（统一 schema + revenue_yoy 修复 + express 缓存）
- 默认增量模式：仅补充缺失的季度数据（最近两年）
- 全量模式：python3 download_financials.py --full  强制全量重新下载
- 单线程顺序执行，请求间隔固定 0.1 秒
- express report 按 (code, year) 缓存，省 75% express 调用
- operation data 按 (code, year) 缓存，自动计算 revenue_yoy
- 缺失季度批量 SQL 查询，一次取全部股票既有数据
- 网络错误重试，确定性失败直接跳过
- 断网重连等待 10~20 秒
- 保存字段：成长能力 + 盈利能力 + 业绩快报 + 营收增长率
- 失败股票输出到 failed_financial.txt
"""
import warnings
import baostock as bs
import sqlite3
import pandas as pd
from datetime import datetime
import time, sys, socket, random
import sys, os; sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))); from db import schema as db_schema

warnings.filterwarnings('ignore', category=FutureWarning)

DB_PATH = 'stocks_2y.db'   # 可被 config.yaml database.path 覆盖
REQUEST_DELAY = 0.1             # 固定请求间隔（秒）
MAX_RETRY = 1                   # 网络错误重试次数
SOCKET_TIMEOUT = 180
CURRENT_YEAR = datetime.now().year
QUARTERS = [1, 2, 3, 4]


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


def is_quarter_available(year, quarter):
    """
    根据A股季报披露规则，判断该季度数据现在是否可能已发布。
    """
    if quarter == 1:
        deadline = pd.Timestamp(year=year, month=5, day=5)
    elif quarter == 2:
        deadline = pd.Timestamp(year=year, month=9, day=5)
    elif quarter == 3:
        deadline = pd.Timestamp(year=year, month=11, day=5)
    else:
        deadline = pd.Timestamp(year=year+1, month=5, day=5)
    return pd.Timestamp.now() >= deadline


def _quarter_stat_date(year, quarter):
    """将 (年, 季度) 映射为 stat_date 字符串"""
    month = quarter * 3
    day = 30 if month in (6, 9) else 31
    return f"{year}-{month:02d}-{day}"


# ===================== 营收数据缓存（修复 revenue_yoy 永远 NULL） =====================

def _ensure_operation_cache(code, year, op_cache):
    """
    确保 (code, year) 的营收数据在缓存中。
    按季度查询，将累积营收转换为单季度营收。
    返回 {1: Q1单季营收, 2: Q2单季营收, 3: Q3单季营收, 4: Q4单季营收}
    """
    cache_key = (code, year)
    if cache_key in op_cache:
        return op_cache[cache_key]

    cum_revs = {}   # 累积营收 per quarter
    for q in QUARTERS:
        if not is_quarter_available(year, q):
            continue  # ★ 跳过未公布季度的空查询
        try:
            rs = bs.query_operation_data(code, year=year, quarter=q)
            if rs.error_code == '0':
                rows = []
                while rs.next():
                    rows.append(rs.get_row_data())
                if rows:
                    df = pd.DataFrame(rows, columns=rs.fields)
                    # 尝试多个可能的营收字段名
                    for rev_field in ['operrevenue', 'OperRevenue', 'operRev']:
                        if rev_field in rs.fields:
                            cum_revs[q] = pd.to_numeric(df[rev_field].iloc[0], errors='coerce')
                            break
        except:
            pass
        time.sleep(REQUEST_DELAY)

    # 累积 → 单季度
    single_q = {}
    for q in QUARTERS:
        if q == 1:
            single_q[q] = cum_revs.get(1)
        elif q in cum_revs and (q - 1) in cum_revs:
            single_q[q] = cum_revs[q] - cum_revs[q - 1]
        elif q in cum_revs:
            single_q[q] = cum_revs.get(q)  # 仅有累积，无法拆分
        else:
            single_q[q] = None

    op_cache[cache_key] = single_q
    return single_q


def _calc_revenue_yoy(code, year, quarter, op_cache):
    """
    计算营收同比增长率 = (本季 - 去年同季) / |去年同季| * 100。
    返回 (yoy_value, None) 或 (None, error_msg)
    """
    try:
        curr_revs = _ensure_operation_cache(code, year, op_cache)
        last_revs = _ensure_operation_cache(code, year - 1, op_cache)
        curr = curr_revs.get(quarter)
        last = last_revs.get(quarter)
        if curr is None or last is None:
            return None
        if last == 0:
            return None
        return round((curr - last) / abs(last) * 100, 2)
    except Exception as e:
        return None


# ===================== 单股下载 =====================

def download_single(code, name, year, quarter, express_cache, op_cache):
    """
    下载单只股票指定季度的财务数据。
    express_cache: {(code, year): DataFrame}   express report 缓存
    op_cache:      {(code, year): {q: revenue}} 营收数据缓存
    """
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
                return None, f"成长能力接口错误: error_code={rs.error_code}, msg={rs.error_msg}"

            if not growth_data:
                return None, "成长能力无数据"

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

            # 3. 业绩快报（缓存优化：同 (code, year) 只查一次）
            cache_key = (code, year)
            if cache_key in express_cache:
                express_df = express_cache[cache_key]
            else:
                express_df = None
                try:
                    rs_express = bs.query_performance_express_report(
                        code, start_date=f"{year}-01-01", end_date=f"{year+1}-12-31")
                    if rs_express.error_code == '0':
                        rows = []
                        while rs_express.next():
                            rows.append(rs_express.get_row_data())
                        if rows:
                            express_df = pd.DataFrame(rows, columns=rs_express.fields)
                except:
                    pass
                express_cache[cache_key] = express_df

            if express_df is not None and not express_df.empty:
                target_end = _quarter_stat_date(year, quarter)
                mask = express_df['performanceExpStatDate'] == target_end
                if mask.any():
                    latest = express_df[mask].iloc[-1]
                    if 'performanceExpressGRYOY' in express_df.columns:
                        express_data['express_gryoy'] = pd.to_numeric(
                            latest['performanceExpressGRYOY'], errors='coerce')
                    if 'performanceExpressOPYOY' in express_df.columns:
                        express_data['express_opyoy'] = pd.to_numeric(
                            latest['performanceExpressOPYOY'], errors='coerce')
                    express_data['express_pub_date'] = latest.get('performanceExpPubDate', '')
                    express_data['express_stat_date'] = latest.get('performanceExpStatDate', '')

            # 4. ★ 营收增长率（v2.0 修复：从 operation_data 获取）
            revenue_yoy = _calc_revenue_yoy(code, year, quarter, op_cache)

            record = {
                'code': code, 'name': name,
                'pub_date': growth_data.get('pub_date', ''),
                'stat_date': growth_data.get('stat_date', _quarter_stat_date(year, quarter)),
                'net_profit_yoy': growth_data.get('net_profit_yoy'),
                'revenue_yoy': revenue_yoy,                     # ★ 修复
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
    return None, "网络重试后仍失败"


# ===================== 主流程 =====================

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
    db_schema.init_all_tables(conn)
    db_schema.init_db_pragmas(conn)

    codes = get_mainboard_codes(conn)
    total = len(codes)

    # === 任务构建 ===
    tasks = []
    skipped = 0

    if full_mode:
        for code, name in codes:
            for y in [CURRENT_YEAR - 1, CURRENT_YEAR]:
                for q in QUARTERS:
                    if is_quarter_available(y, q):
                        tasks.append((code, name, y, q))
    else:
        min_date = f"{CURRENT_YEAR-2}-01-01"
        rows = conn.execute("""
            SELECT code, stat_date FROM financial
            WHERE stat_date >= ? AND net_profit_yoy IS NOT NULL
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
                        tasks.append((code, name, year, quarter))
                        missing_count += 1
            if missing_count == 0:
                skipped += 1

    print(f"共 {total} 只股票，{len(tasks)} 个季度任务，已跳过 {skipped} 只完整股票")
    if not tasks:
        print("全部财务数据已是最新，无需下载。")
        bs.logout()
        conn.close()
        sys.exit(0)

    print(f"请求间隔固定 {REQUEST_DELAY:.1f} 秒，"
          f"express 按 (code, year) 缓存，operation 按 (code, year) 缓存")
    print(f"预计耗时约 {len(tasks) * REQUEST_DELAY / 60:.1f} 分钟（不含 API 响应）")

    success = 0
    failed_stocks = {}
    batch_buffer = []
    express_cache = {}
    op_cache = {}              # ★ 营收数据缓存
    start_time = time.time()

    for idx, (code, name, year, quarter) in enumerate(tasks):
        df, err_msg = download_single(code, name, year, quarter, express_cache, op_cache)

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
            print(f"  已处理 {idx+1}/{len(tasks)}，已用时 {elapsed/60:.1f} 分钟，"
                  f"express缓存命中 {sum(1 for v in express_cache.values() if v is not None)} 次，"
                  f"op缓存 {len(op_cache)} 条(股×年)")

    if batch_buffer:
        db_schema.safe_batch_write(conn, batch_buffer, 'financial', db_schema.FINANCIAL_COLUMNS)
    db_schema.checkpoint_db(conn)
    conn.commit()
    conn.close()

    print(f"\n成功 {success}/{len(tasks)} 个季度")
    if failed_stocks:
        print(f"失败股票 {len(failed_stocks)} 只，详情写入 failed_financial.txt")
        with open('failed_financial.txt', 'w') as f:
            for cd, reason in failed_stocks.items():
                f.write(f"{cd},{reason}\n")
        print("前10条失败示例:")
        for cd, reason in list(failed_stocks.items())[:10]:
            print(f"  {cd}: {reason}")

    bs.logout()
    print("下载结束。")
