#!/usr/bin/env python3
"""
数据质量校验脚本 v4.2

使用方法：
    python3 app/validator.py
"""
import sqlite3, os, sys
import pandas as pd
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import strategy as st

DB_PATH = st.DB_PATH


def check_daily_quality(conn):
    print("=" * 60)
    print("日线数据质量检查")
    print("=" * 60)
    total = pd.read_sql("SELECT COUNT(*) as cnt FROM daily", conn).iloc[0]['cnt']
    codes = pd.read_sql("SELECT COUNT(DISTINCT code) as cnt FROM daily", conn).iloc[0]['cnt']
    date_range = pd.read_sql("SELECT MIN(date) as min_d, MAX(date) as max_d FROM daily", conn)
    print(f"总记录数: {total:,}")
    print(f"股票数:   {codes}")
    print(f"日期范围: {date_range.iloc[0]['min_d']} ~ {date_range.iloc[0]['max_d']}")

    print("\n--- 交易日缺口检查 ---")
    trade_dates = pd.read_sql("SELECT DISTINCT date FROM daily ORDER BY date", conn, parse_dates=['date'])['date']
    gaps = []
    for i in range(1, len(trade_dates)):
        delta = (trade_dates.iloc[i] - trade_dates.iloc[i - 1]).days
        if delta > 5:
            gaps.append((str(trade_dates.iloc[i - 1].date()), str(trade_dates.iloc[i].date()), delta))
    if gaps:
        print(f"⚠ 发现 {len(gaps)} 个缺口（>5天）:")
        for g in gaps[:10]:
            print(f"  {g[0]} → {g[1]}（间隔 {g[2]} 天）")
    else:
        print("✓ 无异常缺口")

    print("\n--- 价格异常检查 ---")
    bad_price = pd.read_sql("SELECT COUNT(*) as cnt FROM daily WHERE close <= 0 OR close IS NULL", conn).iloc[0]['cnt']
    print(f"close <= 0 或 NULL: {bad_price} 条 {'⚠' if bad_price > 0 else '✓'}")

    long_halt = pd.read_sql("""
        SELECT code, COUNT(*) as halt_days FROM daily
        WHERE volume = 0 OR volume IS NULL
        GROUP BY code HAVING COUNT(*) > 20 ORDER BY halt_days DESC
    """, conn)
    if not long_halt.empty:
        print(f"\n⚠ {len(long_halt)} 只股票长期停牌（>20天零成交）:")
        for _, row in long_halt.head(5).iterrows():
            print(f"  {row['code']}: {row['halt_days']} 天")


def check_financial_quality(conn):
    print("\n" + "=" * 60)
    print("财务数据质量检查")
    print("=" * 60)
    total = pd.read_sql("SELECT COUNT(*) as cnt FROM financial", conn).iloc[0]['cnt']
    codes = pd.read_sql("SELECT COUNT(DISTINCT code) as cnt FROM financial", conn).iloc[0]['cnt']
    print(f"总记录数: {total:,}")
    print(f"股票数:   {codes}")

    null_check = pd.read_sql("""
        SELECT SUM(CASE WHEN net_profit_yoy IS NULL THEN 1 ELSE 0 END) as null_profit,
               SUM(CASE WHEN revenue_yoy IS NULL THEN 1 ELSE 0 END) as null_revenue,
               SUM(CASE WHEN yoy_pni IS NULL THEN 1 ELSE 0 END) as null_pni,
               COUNT(*) as total FROM financial
    """, conn)
    if total > 0:
        row = null_check.iloc[0]
        t = row['total']
        print(f"\nNULL 率（{t} 条）:")
        print(f"  net_profit_yoy: {row['null_profit']}/{t} ({row['null_profit']/t*100:.1f}%)")
        print(f"  revenue_yoy:    {row['null_revenue']}/{t} ({row['null_revenue']/t*100:.1f}%)")
        print(f"  yoy_pni:        {row['null_pni']}/{t} ({row['null_pni']/t*100:.1f}%)")

    print("\n--- 季度分布 ---")
    quarter_dist = pd.read_sql("SELECT stat_date, COUNT(*) as cnt FROM financial GROUP BY stat_date ORDER BY stat_date DESC", conn)
    for _, row in quarter_dist.iterrows():
        print(f"  {row['stat_date']}: {row['cnt']} 条")


def check_cross_table(conn):
    print("\n" + "=" * 60)
    print("跨表一致性检查")
    print("=" * 60)
    basic_count = pd.read_sql("SELECT COUNT(*) as cnt FROM stock_basic", conn).iloc[0]['cnt']
    daily_count = pd.read_sql("SELECT COUNT(DISTINCT code) as cnt FROM daily", conn).iloc[0]['cnt']
    print(f"stock_basic: {basic_count}, daily: {daily_count}")
    diff = basic_count - daily_count
    if diff > 0:
        print(f"⚠ {diff} 只股票在 stock_basic 中但 daily 无数据")
    else:
        print("✓ 一致")
    no_fin = pd.read_sql("""
        SELECT COUNT(*) as cnt FROM stock_basic
        WHERE code NOT IN (SELECT DISTINCT code FROM financial) AND code != 'sh.000300'
    """, conn).iloc[0]['cnt']
    print(f"无财务数据: {no_fin} 只 {'⚠' if no_fin > 100 else '✓'}")


if __name__ == '__main__':
    conn = sqlite3.connect(DB_PATH)
    print(f"数据库: {DB_PATH}")
    print(f"校验时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    check_daily_quality(conn)
    check_financial_quality(conn)
    check_cross_table(conn)
    conn.close()
    print("\n" + "=" * 60)
    print("校验完成")
