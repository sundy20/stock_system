#!/usr/bin/env python3
"""
同花顺自选股诊断脚本（基于公共策略模块 stock_strategy）
使用方法：
  1. 在同花顺中导出自选股，保存为 custom_watchlist.txt（每行格式：代码,名称）
  2. 运行 python3 check_custom_stocks.py
  3. 查看输出报告 custom_selection_report.txt，包含符合/不符合条件及详细原因
"""

import sqlite3, pandas as pd, os, sys
from datetime import datetime, timedelta
import stock_strategy as st                                # 导入公共策略模块

WATCHLIST_FILE = 'custom_watchlist.txt'
REPORT_FILE = 'custom_selection_report.txt'


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
                # 统一转换为内部格式 sh.XXXXXX 或 sz.XXXXXX
                code = raw_code.lower()
                if code.startswith('sh.') or code.startswith('sz.'):
                    pass                                       # 已经是内部格式
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


def diagnose_stock(code, name, df_daily, df_fin, target_date, yearly_data):
    """
    对单只股票进行完整策略诊断，返回 (是否通过, 信号描述, 失败原因)
    """
    # 0. 基础数据检查
    if code not in df_daily.index.get_level_values('code'):
        return False, '', '无日线数据'

    df_code = df_daily.loc[code].sort_index()
    recent_mask = df_code.index >= target_date - timedelta(days=40)
    if recent_mask.sum() < 18:
        return False, '', f'近期停牌过多'

    # 1. 年线趋势 + 流动性
    if not st.check_annual_trend(code, df_daily, target_date, yearly_data):
        return False, '', '年线趋势不通过'

    df_code['amount_wan'] = df_code['amount'] / 10000
    ma20 = df_code['amount_wan'].rolling(20).mean().iloc[-1]
    ma120 = df_code['amount_wan'].rolling(120).mean().iloc[-1]
    if pd.isna(ma20) or ma20 < st.MIN_20D_AMOUNT:
        return False, '', f'20日均成交额不足 ({ma20:.0f}万)'
    if pd.isna(ma120) or ma120 < st.MIN_120D_AMOUNT:
        return False, '', f'120日均成交额不足 ({ma120:.0f}万)'

    # 2. 技术信号计算
    df_code_w = df_code[['close', 'low']].resample('W').agg({'close':'last','low':'min'}).dropna()
    df_code_m = df_code[['close', 'low']].resample('ME').agg({'close':'last','low':'min'}).dropna()
    if len(df_code_w) < 20 or len(df_code_m) < 18:
        return False, '', '周线/月线数据不足'

    m_retest_ok = st.detect_retest_with_gap(df_code_m, 20, st.MONTHLY_RETEST_DOWN, st.MONTHLY_RETEST_NEAR,
                                            st.MONTHLY_RETEST_WINDOW, st.MONTHLY_RETEST_MIN_GAP,
                                            st.MONTHLY_RETEST_MIN_TOUCHES, True).iloc[-1]
    m_bb_ok = st.detect_bb_expand(df_code_m, st.BB_PERIOD, st.BB_STD_MULT, st.BB_SHORT_MA, st.BB_LONG_MA,
                                  require_mid_up=st.MONTHLY_BB_REQUIRE_MID_UP,
                                  short_dir_period=st.BB_SHORT_DIR_PERIOD, overbought_limit=None).iloc[-1]
    w_retest_ok = st.detect_retest_with_gap(df_code_w, 20, st.WEEKLY_RETEST_DOWN, st.WEEKLY_RETEST_NEAR,
                                            st.WEEKLY_RETEST_WINDOW, st.WEEKLY_RETEST_MIN_GAP,
                                            st.WEEKLY_RETEST_MIN_TOUCHES, True).iloc[-1]
    w_bb_ok = st.detect_bb_expand(df_code_w, st.BB_PERIOD, st.BB_STD_MULT, st.BB_SHORT_MA, st.BB_LONG_MA,
                                  require_mid_up=st.WEEKLY_BB_REQUIRE_MID_UP,
                                  short_dir_period=st.BB_SHORT_DIR_PERIOD,
                                  overbought_limit=st.WEEKLY_BB_OVERBOUGHT,
                                  pre_expand=st.WEEKLY_BB_PRE_EXPAND,
                                  contraction_ratio=st.WEEKLY_BB_CONTRACTION_RATIO,
                                  use_dual_mode=st.WEEKLY_BB_USE_DUAL_MODE,
                                  price_limit=st.WEEKLY_BB_PRICE_LIMIT).iloc[-1]

    has_signal = w_retest_ok or w_bb_ok or m_retest_ok or m_bb_ok
    if not has_signal:
        return False, '', '无任何技术信号'

    # 3. 财务检查（根据开关）
    if st.USE_FINANCIAL_FILTER:
        fin_codes = st.apply_financial_filter([code], df_fin, target_date)
        if not fin_codes:
            return False, '', '财务条件不满足'

    # 4. 汇总信号描述
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
    basic = pd.read_sql("SELECT code, name FROM stock_basic", conn)
    conn.close()

    target_date = df_daily.index.get_level_values('date').max()

    # 计算自然年聚合（用于年线判断）
    df_stocks = df_daily.copy()
    df_stocks['year'] = df_stocks.index.get_level_values('date').year
    yearly = df_stocks.groupby(['code', 'year']).agg(
        first_open=('open', 'first'), last_close=('close', 'last'), total_volume=('volume', 'sum')
    ).sort_index()

    passed, failed = [], []
    for code, name in watchlist:
        display_name = name if name else code
        ok, sig, reason = diagnose_stock(code, display_name, df_daily, df_fin, target_date, yearly)
        if ok:
            passed.append((code, display_name, sig))
        else:
            failed.append((code, display_name, reason))

    # ★ 修复点：将 f-string 拆分为多行，避免解析错误
    with open(REPORT_FILE, 'w', encoding='utf-8') as f:
        f.write(f"自选股诊断报告（策略 v4.0）  日期：{target_date.strftime('%Y-%m-%d')}\n")
        f.write("=" * 80 + "\n\n")
        f.write(f"✅ 符合条件（共 {len(passed)} 只）\n")
        f.write("-" * 40 + "\n")
        for code, name, sig in passed:
            f.write(f"{code} {name}  信号: {sig}\n")

        f.write(f"\n❌ 不符合条件（共 {len(failed)} 只）\n")
        f.write("-" * 40 + "\n")
        for code, name, reason in failed:
            f.write(f"{code} {name}  原因: {reason}\n")

    print(f"\n诊断完成，报告已保存至 {REPORT_FILE}")
    print(f"符合: {len(passed)} 只，不符合: {len(failed)} 只")