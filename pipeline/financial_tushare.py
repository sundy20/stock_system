#!/usr/bin/env python3
"""
tushare 财务数据下载 —— v2.0（补齐字段 + 统一 schema）
- 动态日期：自动计算当前年和去年
- 增量模式：仅补充数据库中缺失的季度数据
- 全量模式：python3 download_financial_tushare.py --full
- 限速 50 次/分钟，失败重试 2 次
- 表结构与 baostock 版兼容，backtest 脚本可直接切换使用
- v2.0: 补齐 net_profit/roe_avg/gp_margin 字段；清理死代码
"""
import os, sys, time, sqlite3, pandas as pd
from datetime import datetime
import tushare as ts
import sys, os; sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))); from db import schema as db_schema

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
            sd = f"{year}-{quarter * 3:02d}-{['31','30','30','31'][quarter-1]}"
            if sd not in existing or not existing[sd]:
                missing.append((year, quarter, sd))
    return missing


def download_one(code, name, year, quarter):
    """
    下载单只股票指定季度的财务数据。
    使用 pro.fina_indicator（主）+ pro.income（补充净利润）。
    """
    ts_code = code[3:] + '.SH' if code.startswith('sh.') else code[3:] + '.SZ'
    stat_date = f"{year}-{quarter * 3:02d}-{['31','30','30','31'][quarter-1]}"
    period = stat_date.replace('-', '')  # 20240331

    try:
        # 1. 财务指标（主接口：成长性）
        df = pro.fina_indicator(
            ts_code=ts_code, period=period,
            fields='ts_code,ann_date,end_date,'
                   'q_profit_yoy,q_gr_yoy,q_fa_yoygr,'        # 成长性
                   'roe,roe_yearly,gp_margin,'                 # ★ v2.0 补齐：盈利能力
                   'profit_dedt'                                # ★ 扣非净利润
        )
        if df is None or df.empty:
            return None, "fina_indicator 无数据返回"

        record = {
            'code': code,
            'name': name,
            'pub_date': str(pd.to_datetime(df['ann_date'].iloc[0]).date()) if 'ann_date' in df.columns else '',
            'stat_date': stat_date,
            'net_profit_yoy': _safe_float(df, 'q_profit_yoy'),
            'revenue_yoy': _safe_float(df, 'q_gr_yoy'),
            'yoy_equity': _safe_float(df, 'q_fa_yoygr'),
            'yoy_asset': None,
            'yoy_eps': None,
            'yoy_pni': _safe_float(df, 'profit_dedt'),
            'net_profit': None,     # 需要通过 income 接口获取
            'roe_avg': _safe_float(df, 'roe_yearly') or _safe_float(df, 'roe'),
            'gp_margin': _safe_float(df, 'gp_margin'),
            'express_gryoy': None,
            'express_opyoy': None,
            'express_pub_date': '',
            'express_stat_date': '',
        }

        # 2. 利润表（补充净利润）
        try:
            df_inc = pro.income(
                ts_code=ts_code, period=period,
                fields='ts_code,end_date,n_income,n_income_attr_p'
            )
            if df_inc is not None and not df_inc.empty:
                record['net_profit'] = _safe_float(df_inc, 'n_income_attr_p') or _safe_float(df_inc, 'n_income')
                # 用利润表的 end_date 覆盖（有时与 fina_indicator 不同）
                if 'end_date' in df_inc.columns and not pd.isna(df_inc['end_date'].iloc[0]):
                    record['stat_date'] = str(pd.to_datetime(df_inc['end_date'].iloc[0]).date())
        except Exception:
            pass  # income 接口可能积分不足，静默跳过

        return pd.DataFrame([record]), None

    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def _safe_float(df, col):
    """安全取 DataFrame 第一行的 float 值"""
    if df is None or col not in df.columns:
        return None
    try:
        val = df[col].iloc[0]
        if pd.isna(val):
            return None
        return float(val)
    except (ValueError, TypeError):
        return None


# ===================== 主流程 =====================

if __name__ == '__main__':
    full_mode = '--full' in sys.argv
    mode_str = '全量重新下载' if full_mode else '增量更新'
    print(f"tushare 财务数据下载 v2.0：{mode_str}（{CURRENT_YEAR - 1}~{CURRENT_YEAR}）")

    conn = sqlite3.connect(DB_PATH)
    db_schema.init_all_tables(conn)
    db_schema.init_db_pragmas(conn)
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
                db_schema.safe_batch_write(conn, batch_buffer, 'financial', db_schema.FINANCIAL_COLUMNS)
                batch_buffer = []
                print(f"  已完成 {success}/{len(tasks)} 个季度")
        else:
            failed_stocks.append((code, name, err_msg))
            if len(failed_stocks) <= 20:
                print(f"  ✗ {code} {name} 失败: {err_msg}")

        if (success + len(failed_stocks)) % 200 == 0:
            print(f"  已处理 {success + len(failed_stocks)}/{len(tasks)}")

    if batch_buffer:
        db_schema.safe_batch_write(conn, batch_buffer, 'financial', db_schema.FINANCIAL_COLUMNS)

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
