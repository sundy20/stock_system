#!/usr/bin/env python3
"""
选股+回测+导出 最终版（修复 numpy.datetime64 报错 + 调仓对齐）
条件：年线上升 + 连续N期净利润正增长 + 月/周线两次回踩均线
"""
import sqlite3
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

DB_PATH = 'stocks_2y.db'
BENCH_CODE = 'sh.000300'

# ==================== 可调参数 ====================
BACKTEST_START = (datetime.now() - timedelta(days=365)).strftime('%Y-%m-%d')
BACKTEST_END = datetime.now().strftime('%Y-%m-%d')
INIT_CASH = 100000
MAX_STOCKS = 200
TOLERANCE = 0.2                # 回踩容忍度20%
MIN_LIQUIDITY = 3000           # 日均成交额最低3000万元
CONSECUTIVE_FIN_PERIODS = 1    # 连续盈利期数
MONTHLY_MA = 12
WEEKLY_MA = 20
REBALANCE_FREQ = 'W'           # W=周，M=月
COMMISSION = 0.0003
SLIPPAGE = 0.001
STAMP_DUTY = 0.001

# ==================== 数据加载 ====================
def load_all_data(conn):
    query = """
            SELECT code, date, open, high, low, close, volume
            FROM daily
            WHERE date >= date('now', '-3 years')
            ORDER BY code, date \
            """
    df = pd.read_sql(query, conn, parse_dates=['date'])
    return df.set_index(['code', 'date']).sort_index()

def load_financial_data(conn):
    query = """
            SELECT code, stat_date, pub_date, net_profit_yoy
            FROM financial
            WHERE net_profit_yoy IS NOT NULL
            ORDER BY code, stat_date \
            """
    df = pd.read_sql(query, conn, parse_dates=['stat_date', 'pub_date'])
    df['effective_date'] = df['pub_date'].fillna(df['stat_date'] + timedelta(days=30))
    df['effective_date'] += timedelta(days=10)
    return df

# ==================== 形态检测 ====================
def detect_retest_signal(price_df, ma_period, signal_ma=5, tolerance=TOLERANCE, window=24):
    close = price_df['close']
    low = price_df['low']
    ma_slow = close.rolling(ma_period).mean()
    ma_fast = close.rolling(signal_ma).mean()
    ma_up = ma_slow.diff(10) / 10 > 0
    touch = (low <= ma_slow) & (low >= ma_slow * (1 - tolerance))
    twice_touch = touch.rolling(window, min_periods=ma_period).sum() >= 2
    return ma_up & twice_touch & (close > ma_fast) & (close > ma_slow)

# ==================== 选股 ====================
def vectorized_select_stocks(df_daily, df_fin, name_map, target_date=None):
    if target_date is None:
        target_date = df_daily.index.get_level_values('date').max()
    # 确保 target_date 是 Timestamp 类型
    target_date = pd.Timestamp(target_date)
    print(f"  选股日期: {target_date.date()}")

    df = df_daily.loc[df_daily.index.get_level_values('date') <= target_date].copy()
    stock_codes = [c for c in df.index.get_level_values('code').unique() if c != BENCH_CODE]
    df_stocks = df[df.index.get_level_values('code').isin(stock_codes)]

    grouped = df_stocks.groupby(level='code')
    df_stocks['ma250'] = grouped['close'].transform(lambda x: x.rolling(250).mean())
    df_stocks['ma250_slope'] = grouped['ma250'].transform(lambda x: x.diff(20) / 20)
    df_stocks['amount'] = df_stocks['close'] * df_stocks['volume'] / 10000
    df_stocks['amount_ma20'] = grouped['amount'].transform(lambda x: x.rolling(20).mean())

    latest = df_stocks.groupby(level='code').tail(1)
    base = latest[(latest['ma250_slope'] > 0) & (latest['close'] > latest['ma250']) &
                  (latest['amount_ma20'] >= MIN_LIQUIDITY)]
    base_codes = base.index.get_level_values('code').tolist()
    print(f"  基础筛选: {len(base_codes)} 只")

    df_f = df_stocks[df_stocks.index.get_level_values('code').isin(base_codes)]
    if df_f.empty: return []

    daily_close = df_f['close'].unstack(level='code')
    daily_low = df_f['low'].unstack(level='code')

    # 周线
    w_close = daily_close.resample('W').last()
    w_low = daily_low.resample('W').min()
    w_df = pd.DataFrame({'close': w_close.stack(), 'low': w_low.stack()})
    w_signal = detect_retest_signal(w_df, ma_period=WEEKLY_MA, window=30)
    w_codes = w_signal.groupby(level='code').tail(1).pipe(lambda x: x[x].index.get_level_values('code').tolist())
    print(f"  周线信号: {len(w_codes)} 只")

    # 月线
    m_close = daily_close.resample('ME').last()
    m_low = daily_low.resample('ME').min()
    m_df = pd.DataFrame({'close': m_close.stack(), 'low': m_low.stack()})
    m_signal = detect_retest_signal(m_df, ma_period=MONTHLY_MA, window=24)
    m_codes = m_signal.groupby(level='code').tail(1).pipe(lambda x: x[x].index.get_level_values('code').tolist())
    print(f"  月线信号: {len(m_codes)} 只")

    tech = {}
    for c in set(w_codes + m_codes):
        if c in m_codes and c in w_codes:
            tech[c] = (1, '月周线共振')
        elif c in m_codes:
            tech[c] = (2, '月线')
        else:
            tech[c] = (3, '周线')

    fin_before = df_fin[df_fin['effective_date'] <= target_date]
    if fin_before.empty:
        print("  无有效财务数据")
        return []
    fin_latest = fin_before.sort_values('effective_date').groupby('code').tail(CONSECUTIVE_FIN_PERIODS)
    fin_pass = fin_latest.groupby('code').filter(lambda x: all(x['net_profit_yoy'] > 0) and len(x) == CONSECUTIVE_FIN_PERIODS)
    fin_codes = fin_pass['code'].unique().tolist()
    print(f"  财务符合: {len(fin_codes)} 只")

    candidates = [(c, *tech[c]) for c in tech if c in fin_codes]
    candidates.sort(key=lambda x: x[1])
    final = candidates[:MAX_STOCKS]
    print(f"  最终选股: {len(final)} 只")
    return [(c, name_map.get(c, c), t) for c, _, t in final]

# ==================== 自建回测引擎 ====================
def calc_performance(net_value, init_cash, risk_free=0.03):
    returns = net_value.pct_change(fill_method=None).dropna()
    total_return = (net_value.iloc[-1] / init_cash - 1) * 100
    days = (net_value.index[-1] - net_value.index[0]).days
    annual_return = ((net_value.iloc[-1] / init_cash) ** (365 / days) - 1) * 100 if days > 0 else 0
    sharpe = (returns.mean() * 252 - risk_free) / (returns.std() * np.sqrt(252)) if returns.std() != 0 else 0
    rolling_max = net_value.cummax()
    drawdown = (net_value - rolling_max) / rolling_max * 100
    max_drawdown = drawdown.min()
    calmar = annual_return / abs(max_drawdown) if max_drawdown != 0 else 0
    win_rate = (returns > 0).sum() / len(returns) * 100 if len(returns) > 0 else 0
    return {
        'total_return': total_return,
        'annual_return': annual_return,
        'sharpe': sharpe,
        'max_drawdown': max_drawdown,
        'calmar': calmar,
        'win_rate': win_rate,
        'final_value': net_value.iloc[-1]
    }

def run_backtest(df_daily, df_fin, name_map, start_date, end_date):
    close_all = df_daily['close'].unstack(level='code')
    close_all = close_all.loc[start_date:end_date].dropna(axis=1, how='all')
    bench_close = close_all[BENCH_CODE] if BENCH_CODE in close_all.columns else None
    stock_close = close_all[[c for c in close_all.columns if c != BENCH_CODE]]

    # 调仓日对齐：取每个周期最后一个真实交易日
    freq = 'W' if REBALANCE_FREQ == 'W' else 'ME'
    periods = stock_close.index.to_period(freq)
    rebalance_dates = stock_close.groupby(periods).apply(lambda x: x.index[-1]).values

    print(f"  回测调仓频率：{freq}，共 {len(rebalance_dates)} 次调仓")

    # 计算每期选股池
    stock_pools = {}
    for d_raw in rebalance_dates:
        d = pd.Timestamp(d_raw)          # 转换为 pandas Timestamp
        sel = vectorized_select_stocks(df_daily, df_fin, name_map, target_date=d)
        stock_pools[d] = [s[0] for s in sel if s[0] in stock_close.columns]
        print(f"    调仓日 {d.date()}，持股 {len(stock_pools[d])} 只")

    # 权重矩阵
    weights = pd.DataFrame(0.0, index=stock_close.index, columns=stock_close.columns)
    for d in rebalance_dates:
        d = pd.Timestamp(d)
        pool = stock_pools.get(d, [])
        if pool:
            weights.loc[d, pool] = 1.0 / len(pool)
    weights = weights.ffill().fillna(0)

    # 日收益率
    daily_ret = stock_close.pct_change(fill_method=None).fillna(0)

    # 组合每日收益率
    portfolio_ret = (weights.shift(1) * daily_ret).sum(axis=1)
    portfolio_ret.iloc[0] = 0

    # 净值曲线
    net_value = (1 + portfolio_ret).cumprod() * INIT_CASH
    net_value.iloc[0] = INIT_CASH

    perf = calc_performance(net_value, INIT_CASH)

    # 基准收益
    bench_ret = None
    if bench_close is not None:
        bench_ret = (bench_close / bench_close.iloc[0] - 1) * 100

    return perf, bench_ret, stock_pools

# ==================== 主流程 ====================
if __name__ == '__main__':
    conn = sqlite3.connect(DB_PATH)
    print("加载数据...")
    df_daily = load_all_data(conn)
    df_fin = load_financial_data(conn)
    name_map = pd.read_sql("SELECT code, name FROM stock_basic", conn).set_index('code')['name'].to_dict()
    conn.close()

    print("\n===== 最新选股 =====")
    selected = vectorized_select_stocks(df_daily, df_fin, name_map)

    if not selected:
        print("\n⚠️ 未选出股票，请尝试放宽条件：")
        print("   - 降低 CONSECUTIVE_FIN_PERIODS 至 1")
        print("   - 提高 TOLERANCE 至 0.3")
        print("   - 降低 MIN_LIQUIDITY 至 1000")
    else:
        print("\n最终选股池（按信号强弱排序）：")
        for code, name, sig in selected:
            print(f"  {code} {name}  信号: {sig}")

    # 导出
    export_file = 'selected_stocks.txt'
    with open(export_file, 'w', encoding='utf-8') as f:
        for code, name, _ in selected:
            plain = code.replace('sh.', '').replace('sz.', '')
            f.write(f"{plain},{name}\n")
    print(f"\n选股结果已导出至 {export_file}")

    print("\n===== 运行回测 =====")
    perf, bench_ret, pools = run_backtest(
        df_daily, df_fin, name_map, BACKTEST_START, BACKTEST_END
    )

    print("\n" + "=" * 50)
    print("回测绩效报告")
    print("=" * 50)
    print(f"初始资金:    {INIT_CASH:>12.2f}")
    print(f"最终资金:    {perf['final_value']:>12.2f}")
    print(f"总收益率:    {perf['total_return']:>11.2f}%")
    print(f"年化收益率:  {perf['annual_return']:>11.2f}%")
    print(f"夏普比率:    {perf['sharpe']:>12.2f}")
    print(f"最大回撤:    {perf['max_drawdown']:>11.2f}%")
    print(f"卡玛比率:    {perf['calmar']:>12.2f}")
    print(f"交易胜率:    {perf['win_rate']:>11.2f}%")

    if bench_ret is not None:
        bench_total = bench_ret.iloc[-1] if isinstance(bench_ret, pd.Series) else bench_ret
        excess = perf['total_return'] - bench_total
        print(f"沪深300收益: {bench_total:>11.2f}%")
        print(f"超额收益:    {excess:>11.2f}%")
    print("=" * 50)
    print("\n选股回测完成。")