#!/usr/bin/env python3
"""
同花顺自选股诊断脚本（基于公共策略模块 stock_strategy）
使用方法：
  1. 在同花顺中导出自选股，保存为 custom_watchlist.txt（每行格式：代码,名称）
  2. 运行 python3 check_custom_stocks.py
  3. 查看输出报告 custom_selection_report.csv
"""

import sqlite3
import pandas as pd
import os
import sys
from datetime import datetime, timedelta
import stock_strategy as st

WATCHLIST_FILE = 'custom_watchlist.txt'
REPORT_FILE = 'custom_selection_report.csv'


def parse_watchlist(filename):
    """
    解析同花顺导出的自选股文件，返回 [(内部代码, 原始名称), ...]
    支持多种格式：600519、600519.SH、sh.600519 等
    """
    codes = []
    with open(filename, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split(',')
            if len(parts) >= 2:
                raw_code = parts[0].strip()
                name = parts[1].strip()
                code = raw_code.lower()
                if code.startswith('sh.') or code.startswith('sz.'):
                    pass
                elif '.' in code:
                    code = code.replace('.sh', '.SH').replace('.sz', '.SZ')
                    if code.endswith('.SH'):
                        code = 'sh.' + code[:-3]
                    elif code.endswith('.SZ'):
                        code = 'sz.' + code[:-3]
                else:
                    if raw_code.startswith('6'):
                        code = 'sh.' + raw_code
                    else:
                        code = 'sz.' + raw_code
                codes.append((code, name))
    return codes


def diagnose_stock_from_cache(code, name, signal_cache, yearly, df_daily, df_fin, target_date):
    """使用预计算缓存诊断单只股票（无计算，纯切片取值）"""
    entry = signal_cache.get(code)
    if entry is None:
        return False, '', '无数据或数据不足'

    target_ts = pd.Timestamp(target_date)

    # 1. 近期停牌检查
    df_code = df_daily.loc[code].sort_index()
    recent = df_code[(df_code.index >= target_ts - timedelta(days=40)) & (df_code.index <= target_ts)]
    if len(recent) < 18:
        return False, '', '近期停牌过多'

    # 2. 年线趋势
    if not st.check_annual_trend_fast(code, entry, yearly, target_ts):
        return False, '', '年线趋势不通过'

    # 3. 流动性
    ma20 = st.get_latest_value(entry['amount_ma20'], target_ts)
    ma120 = st.get_latest_value(entry['amount_ma120'], target_ts)
    if ma20 is None or pd.isna(ma20) or ma20 < st.MIN_20D_AMOUNT:
        reason = f'20日均成交额不足 ({ma20:.0f}万)' if ma20 is not None and not pd.isna(ma20) else '20日均成交额数据缺失'
        return False, '', reason
    if ma120 is None or pd.isna(ma120) or ma120 < st.MIN_120D_AMOUNT:
        reason = f'120日均成交额不足 ({ma120:.0f}万)' if ma120 is not None and not pd.isna(ma120) else '120日均成交额数据缺失'
        return False, '', reason

    # 4. 技术信号
    m_retest_ok = bool(st.get_latest_value(entry['m_retest'], target_ts) or False)
    m_bb_ok     = bool(st.get_latest_value(entry['m_bb'], target_ts) or False)
    w_retest_ok = bool(st.get_latest_value(entry['w_retest'], target_ts) or False)
    w_bb_ok     = bool(st.get_latest_value(entry['w_bb'], target_ts) or False)

    has_signal = w_retest_ok or w_bb_ok or m_retest_ok or m_bb_ok
    if not has_signal:
        return False, '', '无任何技术信号'

    # 5. 财务检查
    if st.USE_FINANCIAL_FILTER:
        fin_codes = st.apply_financial_filter([code], df_fin, target_ts)
        if not fin_codes:
            return False, '', '财务条件不满足'

    # 6. 汇总信号描述
    signal_parts = []
    if w_retest_ok and w_bb_ok: signal_parts.append('周线回踩+周布林')
    elif w_retest_ok: signal_parts.append('周线回踩')
    if m_retest_ok and m_bb_ok: signal_parts.append('月线回踩+月布林')
    elif m_retest_ok: signal_parts.append('月线回踩')
    if w_bb_ok: signal_parts.append('周布林')
    if m_bb_ok: signal_parts.append('月布林')
    return True, '、'.join(signal_parts), ''


if __name__ == '__main__':
    if not os.path.exists(WATCHLIST_FILE):
        print(f"请将同花顺自选股导出为 {WATCHLIST_FILE}，格式：代码,名称")
        sys.exit(1)

    watchlist = parse_watchlist(WATCHLIST_FILE)
    print(f"读取到 {len(watchlist)} 只自选股")

    conn = sqlite3.connect(st.DB_PATH)
    df_daily = st.load_all_data(conn)
    df_fin   = st.load_financial_data(conn)
    conn.close()

    # 一次性预计算
    signal_cache, yearly = st.precompute_all_signals_once(df_daily)

    target_date = df_daily.index.get_level_values('date').max()

    passed, failed = [], []
    for code, name in watchlist:
        display_name = name if name else code
        ok, sig, reason = diagnose_stock_from_cache(
            code, display_name, signal_cache, yearly, df_daily, df_fin, target_date)
        if ok:
            passed.append((code, display_name, sig))
        else:
            failed.append((code, display_name, reason))

    # 导出 CSV
    with open(REPORT_FILE, 'w', encoding='utf-8') as f:
        f.write('类型,代码,名称,信号/原因\n')
        for code, name, sig in passed:
            f.write(f'符合,{code},{name},{sig}\n')
        for code, name, reason in failed:
            f.write(f'不符合,{code},{name},{reason}\n')

    print(f"\n诊断完成，报告已保存至 {REPORT_FILE}")
    print(f"符合: {len(passed)} 只，不符合: {len(failed)} 只")