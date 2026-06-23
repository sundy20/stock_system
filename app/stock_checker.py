#!/usr/bin/env python3
"""
同花顺自选股诊断脚本 v4.3

使用方法：
  python3 app/stock_checker.py [--date YYYY-MM-DD] [-f watchlist.txt] [-o report.csv]
"""
import os, sys, logging, time, argparse
from datetime import datetime, timedelta
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import strategy as st

WATCHLIST_FILE = 'custom_watchlist.txt'
REPORT_FILE    = 'custom_selection_report.csv'

logger = logging.getLogger("stock_checker")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)-5s] %(message)s', datefmt='%H:%M:%S'))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)


def parse_code(raw_code):
    """统一代码格式：600519→sh.600519, 688001→sh.688001, 830799→bj.830799"""
    code = raw_code.strip().lower()
    if code.startswith(('sh.', 'sz.', 'bj.')):
        return code
    if '.' in code:
        parts = code.rsplit('.', 1)
        digits, suffix = parts[0], parts[1]
        if suffix in ('sh', 'sz', 'bj'):
            return f'{suffix}.{digits}'
        return _infer_exchange(digits)
    return _infer_exchange(code)


def _infer_exchange(code):
    code = code.strip()
    if code.startswith('8') or code.startswith('4') and len(code) == 6:
        return 'bj.' + code
    if code.startswith('688'):
        return 'sh.' + code
    if code.startswith('6'):
        return 'sh.' + code
    if code.startswith(('0', '2', '3')):
        return 'sz.' + code
    return ('sh.' if code[0] == '6' else 'sz.') + code


def parse_watchlist(filename):
    """解析自选股文件"""
    codes, skipped = [], []
    with open(filename, 'r', encoding='utf-8') as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split(',')
            raw_code = parts[0].strip()
            name = parts[1].strip() if len(parts) >= 2 else raw_code
            try:
                codes.append((parse_code(raw_code), name))
            except Exception as e:
                skipped.append((lineno, raw_code, str(e)))
    if skipped:
        logger.warning("共 %s 行无法解析", len(skipped))
    return codes


def diagnose_stock_from_cache(code, name, signal_cache, yearly, df_daily, df_fin, target_date):
    """使用预计算缓存诊断单只股票"""
    entry = signal_cache.get(code)
    if entry is None:
        return False, '', '无数据或数据不足'

    target_ts = pd.Timestamp(target_date)

    df_code = df_daily.loc[code].sort_index()
    recent = df_code[(df_code.index >= target_ts - timedelta(days=40)) & (df_code.index <= target_ts)]
    if len(recent) < 18:
        return False, '', '近期停牌过多'

    if not st.check_annual_trend_fast(code, entry, yearly, target_ts):
        return False, '', '年线趋势不通过'

    ma20 = st.get_latest_value(entry['amount_ma20'], target_ts)
    ma120 = st.get_latest_value(entry['amount_ma120'], target_ts)
    if ma20 is None or pd.isna(ma20) or ma20 < st.MIN_20D_AMOUNT:
        reason = f'20日均成交额不足 ({ma20:.0f}万)' if ma20 is not None and not pd.isna(ma20) else '20日均成交额数据缺失'
        return False, '', reason
    if ma120 is None or pd.isna(ma120) or ma120 < st.MIN_120D_AMOUNT:
        reason = f'120日均成交额不足 ({ma120:.0f}万)' if ma120 is not None and not pd.isna(ma120) else '120日均成交额数据缺失'
        return False, '', reason

    m_retest_ok = bool(st.get_latest_value(entry['m_retest'], target_ts) or False)
    m_bb_ok     = bool(st.get_latest_value(entry['m_bb'], target_ts) or False)
    w_retest_ok = bool(st.get_latest_value(entry['w_retest'], target_ts) or False)
    w_bb_ok     = bool(st.get_latest_value(entry['w_bb'], target_ts) or False)

    if not (w_retest_ok or w_bb_ok or m_retest_ok or m_bb_ok):
        return False, '', '无任何技术信号'

    if st.USE_FINANCIAL_FILTER:
        fin_codes = st.apply_financial_filter([code], df_fin, target_ts)
        if not fin_codes:
            return False, '', '财务条件不满足'

    parts = []
    if w_retest_ok and w_bb_ok: parts.append('周线回踩+周布林')
    elif w_retest_ok: parts.append('周线回踩')
    if m_retest_ok and m_bb_ok: parts.append('月线回踩+月布林')
    elif m_retest_ok: parts.append('月线回踩')
    if w_bb_ok and '周布林' not in str(parts): parts.append('周布林')
    if m_bb_ok and '月布林' not in str(parts): parts.append('月布林')
    return True, '、'.join(parts), ''


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='自选股诊断')
    parser.add_argument('--date', '-d', type=str, default=None, help='诊断日期 YYYY-MM-DD')
    parser.add_argument('--file', '-f', type=str, default=WATCHLIST_FILE, help='自选股文件')
    parser.add_argument('--output', '-o', type=str, default=REPORT_FILE, help='输出报告')
    parser.add_argument('--verbose', '-v', action='store_true', help='详细日志')
    args = parser.parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    t_start = time.time()
    logger.info("=" * 50)
    logger.info("自选股诊断 v4.3")
    logger.info("=" * 50)

    if not os.path.exists(args.file):
        logger.error("自选股文件不存在: %s", args.file)
        sys.exit(1)

    watchlist = parse_watchlist(args.file)
    logger.info("读取到 %s 只自选股", len(watchlist))

    conn = st.get_db_connection()
    df_daily = st.load_all_data(conn)
    df_fin   = st.load_financial_data(conn)
    ok, msgs = st.validate_data(df_daily, df_fin)
    for msg in msgs: logger.info("  %s", msg)
    if not ok:
        logger.error("数据校验不通过，退出。")
        conn.close()
        sys.exit(1)
    conn.close()

    if args.date:
        target_date = pd.Timestamp(args.date)
        max_date = df_daily.index.get_level_values('date').max()
        if target_date > max_date:
            logger.warning("指定日期 %s 超出范围，使用最新 %s", args.date, max_date.strftime('%Y-%m-%d'))
            target_date = max_date
    else:
        target_date = df_daily.index.get_level_values('date').max()
    logger.info("诊断日期: %s", target_date.strftime('%Y-%m-%d'))

    signal_cache, yearly = st.precompute_all_signals_once(df_daily)

    passed, failed = [], []
    for code, name in watchlist:
        ok_flag, sig, reason = diagnose_stock_from_cache(
            code, name, signal_cache, yearly, df_daily, df_fin, target_date)
        if ok_flag:
            passed.append((code, name, sig))
        else:
            failed.append((code, name, reason))

    reason_counts = {}
    for _, _, reason in failed:
        reason_counts[reason] = reason_counts.get(reason, 0) + 1

    with open(args.output, 'w', encoding='utf-8') as f:
        f.write('类型,代码,名称,信号/原因\n')
        for code, name, sig in passed:
            plain_code = code.replace('sh.', '').replace('sz.', '').replace('bj.', '')
            f.write(f'符合,{plain_code},{name},{sig}\n')
        for code, name, reason in failed:
            plain_code = code.replace('sh.', '').replace('sz.', '').replace('bj.', '')
            f.write(f'不符合,{plain_code},{name},{reason}\n')

    logger.info("=" * 50)
    logger.info("符合: %s 只 (%.1f%%)", len(passed), len(passed)/len(watchlist)*100 if watchlist else 0)
    logger.info("不符合: %s 只 (%.1f%%)", len(failed), len(failed)/len(watchlist)*100 if watchlist else 0)
    for reason, count in sorted(reason_counts.items(), key=lambda x: -x[1]):
        logger.info("  %s: %s 只", reason, count)
    logger.info("报告已保存至 %s", args.output)
    logger.info("总耗时 %.1fs", time.time() - t_start)
