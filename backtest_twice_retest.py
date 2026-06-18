#!/usr/bin/env python3
"""
选股+回测+导出 最终定稿版
条件：年线上升 + 连续N期净利润正增长 + 月/周线两次回踩均线
优化：全向量化、定期调仓、无未来函数、基准对比、信号优先级
"""
import sqlite3
import pandas as pd
import numpy as np
import vectorbt as vbt
from datetime import datetime, timedelta

DB_PATH = 'stocks_2y.db'
BENCH_CODE = 'sh.000300'

# ==================== 可调参数 ====================
BACKTEST_START = (datetime.now() - timedelta(days=365)).strftime('%Y-%m-%d')
BACKTEST_END = datetime.now().strftime('%Y-%m-%d')
INIT_CASH = 100000
MAX_STOCKS = 100
TOLERANCE = 0.1                # 回踩容忍度 10%
MIN_LIQUIDITY = 5000           # 日均成交额最低阈值（万元）
CONSECUTIVE_FIN_PERIODS = 2    # 连续盈利财报期数
MONTHLY_MA = 12                # 月线均线周期
WEEKLY_MA = 20                 # 周线均线周期
REBALANCE_FREQ = 'W'           # 调仓频率：W周 / M月
COMMISSION = 0.0003            # 佣金费率
SLIPPAGE = 0.001               # 滑点
STAMP_DUTY = 0.001             # 印花税（卖出）

# ==================== 数据加载 ====================
def load_all_data(conn):
    query = """
            SELECT code, date, open, high, low, close, volume
            FROM daily
            WHERE date >= date('now', '-3 years')
            ORDER BY code, date \
            """
    df = pd.read_sql(query, conn, parse_dates=['date'])
    df = df.set_index(['code', 'date']).sort_index()
    return df

def load_financial_data(conn):
    query = """
            SELECT code, stat_date, pub_date, net_profit_yoy
            FROM financial
            WHERE net_profit_yoy IS NOT NULL
            ORDER BY code, stat_date \
            """
    df = pd.read_sql(query, conn, parse_dates=['stat_date', 'pub_date'])
    # 财报发布后保守滞后10天生效，彻底消除未来函数
    df['effective_date'] = df['pub_date'] + timedelta(days=10)
    return df

# ==================== 向量化形态检测 ====================
def detect_retest_signal(price_df, ma_period, signal_ma=5, tolerance=TOLERANCE):
    """向量化检测上升趋势中的两次回踩信号"""
    close = price_df['close']
    low = price_df['low']

    ma_slow = close.rolling(ma_period).mean()
    ma_fast = close.rolling(signal_ma).mean()

    # 前提：均线向上，确保是上升趋势中的回调
    ma_slope = ma_slow.diff(10) / 10
    ma_up = ma_slope > 0

    # 回踩触碰（允许下探容忍度）
    touch = (low <= ma_slow) & (low >= ma_slow * (1 - tolerance))
    # 60周期内至少2次回踩
    touch_count = touch.rolling(60).sum()
    twice_touch = touch_count >= 2

    # 站上快均线且当前价高于慢均线
    above_fast = close > ma_fast
    above_slow = close > ma_slow

    return ma_up & twice_touch & above_fast & above_slow

# ==================== 选股主逻辑 ====================
def vectorized_select_stocks(df_daily, df_fin, name_map, target_date=None):
    if target_date is None:
        target_date = df_daily.index.get_level_values('date').max()

    # 截取到目标日期的数据
    df = df_daily.loc[df_daily.index.get_level_values('date') <= target_date].copy()

    # 1. 日线级别基础筛选：年线上升 + 站上250 + 流动性，剔除基准
    all_codes = df.index.get_level_values('code').unique()
    stock_codes = [c for c in all_codes if c != BENCH_CODE]
    df_stocks = df[df.index.get_level_values('code').isin(stock_codes)]

    grouped = df_stocks.groupby(level='code')
    df_stocks['ma250'] = grouped['close'].transform(lambda x: x.rolling(250).mean())
    df_stocks['ma250_slope'] = grouped['ma250'].transform(lambda x: x.diff(20) / 20)
    df_stocks['amount'] = df_stocks['close'] * df_stocks['volume'] / 10000
    df_stocks['amount_ma20'] = grouped['amount'].transform(lambda x: x.rolling(20).mean())

    latest = df_stocks.groupby(level='code').tail(1)
    cond_ma250_up = latest['ma250_slope'] > 0
    cond_above_ma250 = latest['close'] > latest['ma250']
    cond_liquid = latest['amount_ma20'] >= MIN_LIQUIDITY

    base_selected = latest[cond_ma250_up & cond_above_ma250 & cond_liquid].index.get_level_values('code').tolist()
    df_filtered = df_stocks[df_stocks.index.get_level_values('code').isin(base_selected)]

    if df_filtered.empty:
        return []

    # 展开为日期×股票矩阵
    daily_close = df_filtered['close'].unstack(level='code')
    daily_low = df_filtered['low'].unstack(level='code')

    # 2. 周线级别检测
    weekly_close = daily_close.resample('W').last()
    weekly_low = daily_low.resample('W').min()
    weekly_df = pd.DataFrame({'close': weekly_close.stack(), 'low': weekly_low.stack()})

    weekly_signal = detect_retest_signal(weekly_df, ma_period=WEEKLY_MA)
    weekly_signal_latest = weekly_signal.groupby(level='code').tail(1)
    weekly_codes = weekly_signal_latest[weekly_signal_latest].index.get_level_values('code').tolist()

    # 3. 月线级别检测
    monthly_close = daily_close.resample('M').last()
    monthly_low = daily_low.resample('M').min()
    monthly_df = pd.DataFrame({'close': monthly_close.stack(), 'low': monthly_low.stack()})

    monthly_signal = detect_retest_signal(monthly_df, ma_period=MONTHLY_MA)
    monthly_signal_latest = monthly_signal.groupby(level='code').tail(1)
    monthly_codes = monthly_signal_latest[monthly_signal_latest].index.get_level_values('code').tolist()

    # 合并技术面信号，按优先级排序
    tech_codes_set = set(weekly_codes + monthly_codes)
    signal_priority = {}
    for code in tech_codes_set:
        is_monthly = code in monthly_codes
        is_weekly = code in weekly_codes
        if is_monthly and is_weekly:
            signal_priority[code] = (1, '月周线共振')
        elif is_monthly:
            signal_priority[code] = (2, '月线')
        elif is_weekly:
            signal_priority[code] = (3, '周线')

    # 4. 财务条件：连续N期净利润同比增长 > 0
    fin_before = df_fin[df_fin['effective_date'] <= target_date].copy()
    if fin_before.empty:
        return []

    fin_latest_n = fin_before.sort_values('effective_date').groupby('code').tail(CONSECUTIVE_FIN_PERIODS)
    fin_check = fin_latest_n.groupby('code').agg(
        count=('net_profit_yoy', 'count'),
        all_positive=('net_profit_yoy', lambda x: all(x > 0))
    )
    fin_codes = fin_check[(fin_check['count'] == CONSECUTIVE_FIN_PERIODS) & fin_check['all_positive']].index.tolist()

    # 最终交集，按优先级排序取前N只
    fin_code_set = set(fin_codes)
    final_candidates = sorted(
        [(code, pri, stype) for code, (pri, stype) in signal_priority.items() if code in fin_code_set],
        key=lambda x: x[1]
    )
    final_selection = final_candidates[:MAX_STOCKS]

    result = []
    for code, pri, sig_type in final_selection:
        result.append((code, name_map.get(code, code), sig_type))
    return result

# ==================== 定期调仓回测 ====================
def run_rebalance_backtest(df_daily, df_fin, name_map, start_date, end_date):
    # 股票收盘价矩阵
    close = df_daily['close'].unstack(level='code')
    close = close.loc[start_date:end_date].dropna(axis=1, how='all')

    # 基准收益
    bench_close = close[BENCH_CODE] if BENCH_CODE in close.columns else None

    rebalance_dates = close.resample(REBALANCE_FREQ).last().index

    # 计算每期选股池
    stock_codes = [c for c in close.columns if c != BENCH_CODE]
    stock_pools = {}
    for date in rebalance_dates:
        selected = vectorized_select_stocks(df_daily, df_fin, name_map, target_date=date)
        stock_pools[date] = [s[0] for s in selected if s[0] in stock_codes]

    # 构建等权重组态矩阵
    weights = pd.DataFrame(0.0, index=close.index, columns=stock_codes)
    for date in rebalance_dates:
        codes = stock_pools.get(date, [])
        if len(codes) > 0:
            weights.loc[date, codes] = 1 / len(codes)

    # 持有期仓位不变，前向填充
    weights = weights.replace(0, np.nan).ffill().fillna(0)

    # 运行策略回测
    portfolio = vbt.Portfolio.from_weights(
        close[stock_codes], weights,
        init_cash=INIT_CASH,
        fees=COMMISSION,
        slippage=SLIPPAGE,
        freq='d',
        stamp_duty=STAMP_DUTY,
        stamp_duty_side='sell'
    )

    # 计算基准收益
    bench_return = None
    if bench_close is not None:
        bench_return = bench_close / bench_close.iloc[0] * INIT_CASH

    return portfolio, bench_return, stock_pools

# ==================== 主流程 ====================
if __name__ == '__main__':
    conn = sqlite3.connect(DB_PATH)

    print("加载数据...")
    df_daily = load_all_data(conn)
    df_fin = load_financial_data(conn)

    # 一次性加载名称映射，避免重复读库
    name_map = pd.read_sql("SELECT code, name FROM stock_basic", conn).set_index('code')['name'].to_dict()
    conn.close()

    # 最新选股
    print("\n执行选股...")
    selected = vectorized_select_stocks(df_daily, df_fin, name_map)

    if not selected:
        print("没有选出符合条件的股票，可放宽条件或增大 MAX_STOCKS。")
        exit()

    print("\n最终选股池（按信号优先级排序）：")
    for code, name, sig_type in selected:
        print(f"  {code} {name}  信号类型: {sig_type}")

    # 导出同花顺（无表头，原生兼容）
    export_file = 'selected_stocks.txt'
    with open(export_file, 'w', encoding='utf-8') as f:
        for code, name, _ in selected:
            code_plain = code.replace('sh.', '').replace('sz.', '')
            f.write(f"{code_plain},{name}\n")
    print(f"\n选股结果已导出至 {export_file}")

    # 回测
    print("\n运行回测...")
    portfolio, bench_val, pools = run_rebalance_backtest(
        df_daily, df_fin, name_map, BACKTEST_START, BACKTEST_END
    )

    # 输出绩效报告
    stats = portfolio.stats()
    print("\n" + "=" * 50)
    print("回测绩效报告")
    print("=" * 50)
    print(f"初始资金:    {INIT_CASH:>12.2f}")
    print(f"最终资金:    {portfolio.value()[-1]:>12.2f}")
    print(f"总收益率:    {stats['Total Return [%]']:>11.2f}%")
    print(f"年化收益率:  {stats['Annualized Return [%]']:>11.2f}%")
    print(f"夏普比率:    {stats['Sharpe Ratio']:>12.2f}")
    print(f"最大回撤:    {stats['Max Drawdown [%]']:>11.2f}%")
    print(f"卡玛比率:    {stats['Calmar Ratio']:>12.2f}")
    print(f"交易胜率:    {stats['Win Rate [%]']:>11.2f}%")

    if bench_val is not None:
        bench_total_ret = (bench_val.iloc[-1] / bench_val.iloc[0] - 1) * 100
        excess_ret = stats['Total Return [%]'] - bench_total_ret
        print("-" * 50)
        print(f"沪深300收益: {bench_total_ret:>11.2f}%")
        print(f"超额收益:    {excess_ret:>11.2f}%")

    print("=" * 50)

    # 绘图（可选）
    try:
        portfolio.plot().show()
    except:
        print("绘图跳过")

    print("\n选股回测完成。")