#!/usr/bin/env python3
"""
选股 + 回测 + 导出 + 绘图（最终版，修复数据库连接管理）
================================
选股条件：
  1. 年线（250日均线）斜率向上，且收盘价在年线上方
  2. 最近一整年涨幅 > 0，且成交量放大（数据不足时仅要求上涨）
  3. 20日均成交额 >= 2000 万
  4. 连续最近2个季度净利润同比增长 > 0（若营收数据存在，则也需 > 0）
  5. 月线（20月均线）或周线（20周均线）出现技术信号（优先级：周线布林扩张 > 月线布林扩张 > 周线回踩 > 月线回踩）：
     - 布林扩张：短期带宽均线 > 长期带宽均线，且短期均线方向向上
     - 两次回踩：均线向上，窗口内至少2次回踩（下探≤15%或靠近≤10%），当前站上10均线且高于慢线
"""
import sqlite3, pandas as pd, numpy as np
from datetime import datetime, timedelta

# ===================== 基本配置 =====================
DB_PATH = 'stocks_2y.db'
BENCH_CODE = 'sh.000300'

# ===================== 可调参数 =====================
BACKTEST_START = (datetime.now() - timedelta(days=365*2)).strftime('%Y-%m-%d')
BACKTEST_END = datetime.now().strftime('%Y-%m-%d')
INIT_CASH = 1_000_000
MAX_STOCKS = 200
TOLERANCE = 0.15                            # 回踩下探容忍度
TOLERANCE_NEAR = 0.10                       # 回踩靠近（未触碰）容忍度
MIN_LIQUIDITY = 2000
CONSECUTIVE_FIN_PERIODS = 2
MONTHLY_MA = 20                             # 月线慢速均线
WEEKLY_MA = 20                              # 周线慢速均线
SIGNAL_MA = 10                              # 回踩后站上的快速均线
MONTHLY_WINDOW = 18                         # 月线回踩窗口（根数）
WEEKLY_WINDOW = 30                          # 周线回踩窗口（根数）
REBALANCE_FREQ = 'W'
COMMISSION = 0.0001
SLIPPAGE = 0.001
STAMP_DUTY = 0.001

# 布林带参数
BB_PERIOD = 20
BB_STD_MULT = 2
BB_SHORT_MA = 5
BB_LONG_MA = 20


# ===================== 数据加载（使用传入的连接） =====================
def load_all_data(conn):
    query = """SELECT code, date, open, high, low, close, volume
               FROM daily WHERE date >= '2023-01-01' ORDER BY code, date"""
    df = pd.read_sql(query, conn, parse_dates=['date'])
    return df.set_index(['code', 'date']).sort_index()


def load_financial_data(conn):
    query = """SELECT code, stat_date, pub_date, net_profit_yoy, revenue_yoy
               FROM financial WHERE net_profit_yoy IS NOT NULL ORDER BY code, stat_date"""
    df = pd.read_sql(query, conn, parse_dates=['stat_date', 'pub_date'])
    df['effective_date'] = df['pub_date'].fillna(df['stat_date'] + timedelta(days=30)) + timedelta(days=10)
    return df


# ===================== 技术形态检测 =====================
def detect_retest_signal(price_df, ma_period, signal_ma=SIGNAL_MA, tolerance=TOLERANCE,
                         tolerance_near=TOLERANCE_NEAR, window=24):
    close = price_df['close']
    low = price_df['low']
    ma_slow = close.rolling(ma_period).mean()
    ma_fast = close.rolling(signal_ma).mean()
    ma_up = ma_slow.diff(10) / 10 > 0
    touch_down = (low < ma_slow) & ((ma_slow - low) / ma_slow <= tolerance)
    touch_near = (low >= ma_slow) & ((low - ma_slow) / ma_slow <= tolerance_near)
    touch = touch_down | touch_near
    min_p = min(ma_period, window)
    twice_touch = touch.rolling(window, min_periods=min_p).sum() >= 2
    return ma_up & twice_touch & (close > ma_fast) & (close > ma_slow)


def detect_bollinger_expansion(price_df):
    close = price_df['close']
    middle = close.rolling(BB_PERIOD).mean()
    std = close.rolling(BB_PERIOD).std()
    bandwidth = 2 * BB_STD_MULT * std
    bw_short = bandwidth.rolling(BB_SHORT_MA).mean()
    bw_long = bandwidth.rolling(BB_LONG_MA).mean()
    expanding = (bw_short > bw_long) & (bw_short.diff(3) > 0)
    return expanding


# ===================== 选股主逻辑（使用传入的 conn） =====================
def vectorized_select_stocks(conn, df_daily, df_fin, name_map, target_date=None):
    if target_date is None:
        target_date = df_daily.index.get_level_values('date').max()
    target_date = pd.Timestamp(target_date)

    df = df_daily.loc[df_daily.index.get_level_values('date') <= target_date].copy()
    stock_codes = [c for c in df.index.get_level_values('code').unique() if c != BENCH_CODE]
    df_stocks = df[df.index.get_level_values('code').isin(stock_codes)]

    grouped = df_stocks.groupby(level='code')
    df_stocks['ma250'] = grouped['close'].transform(lambda x: x.rolling(250).mean())
    df_stocks['ma250_slope'] = grouped['ma250'].transform(lambda x: x.diff(20) / 20)
    df_stocks['amount'] = df_stocks['close'] * df_stocks['volume'] / 10000
    df_stocks['amount_ma20'] = grouped['amount'].transform(lambda x: x.rolling(20).mean())

    def yearly_bull(group):
        if len(group) < 252:
            return pd.Series(False, index=group.index)
        c, v = group['close'], group['volume']
        pos = c.iloc[-1] > c.iloc[-252]
        vol_up = (v.iloc[-252:].sum() > v.iloc[-504:-252].sum()) if len(v) >= 504 else True
        return pd.Series(pos & vol_up, index=group.index)

    df_stocks['yearly_bull'] = grouped.apply(yearly_bull).reset_index(level=0, drop=True)

    latest = df_stocks.groupby(level='code').tail(1)
    base = latest[(latest['ma250_slope'] > 0) &
                  (latest['close'] > latest['ma250']) &
                  (latest['yearly_bull']) &
                  (latest['amount_ma20'] >= MIN_LIQUIDITY)]
    base_codes = base.index.get_level_values('code').tolist()
    print(f"基础筛选后：{len(base_codes)} 只")
    if not base_codes:
        return []

    df_f = df_stocks[df_stocks.index.get_level_values('code').isin(base_codes)]
    if df_f.empty:
        return []

    daily_close = df_f['close'].unstack(level='code')
    daily_low   = df_f['low'].unstack(level='code')

    # 周线信号
    w_close = daily_close.resample('W').last()
    w_low = daily_low.resample('W').min()
    w_df = pd.DataFrame({'close': w_close.stack(), 'low': w_low.stack()})
    w_retest = detect_retest_signal(w_df, ma_period=WEEKLY_MA, window=WEEKLY_WINDOW)
    w_bb = detect_bollinger_expansion(w_df)
    w_signal = w_retest | w_bb
    w_codes = w_signal.groupby(level='code').tail(1).pipe(lambda x: x[x].index.get_level_values('code').tolist())

    # 月线信号
    m_close = daily_close.resample('ME').last()
    m_low = daily_low.resample('ME').min()
    m_df = pd.DataFrame({'close': m_close.stack(), 'low': m_low.stack()})
    m_retest = detect_retest_signal(m_df, ma_period=MONTHLY_MA, window=MONTHLY_WINDOW)
    m_bb = detect_bollinger_expansion(m_df)
    m_signal = m_retest | m_bb
    m_codes = m_signal.groupby(level='code').tail(1).pipe(lambda x: x[x].index.get_level_values('code').tolist())

    w_bb_codes = w_bb.groupby(level='code').tail(1).pipe(lambda x: x[x].index.get_level_values('code').tolist())
    m_bb_codes = m_bb.groupby(level='code').tail(1).pipe(lambda x: x[x].index.get_level_values('code').tolist())

    def priority(code):
        wb, mb = code in w_bb_codes, code in m_bb_codes
        if wb: return 1, '周线布林扩张'
        if mb: return 2, '月线布林扩张'
        if code in w_codes: return 3, '周线回踩'
        if code in m_codes: return 4, '月线回踩'
        return 99, ''

    fin_before = df_fin[df_fin['effective_date'] <= target_date]
    if fin_before.empty:
        return []
    fin_latest = fin_before.sort_values('effective_date').groupby('code').tail(CONSECUTIVE_FIN_PERIODS)
    fin_pass = fin_latest.groupby('code').filter(
        lambda x: all(x['net_profit_yoy'] > 0) and len(x) == CONSECUTIVE_FIN_PERIODS)
    fin_codes = fin_pass['code'].unique().tolist()
    print(f"财务符合：{len(fin_codes)} 只")

    candidates = []
    # 使用传入的 conn 查询最新财务数据，避免重复连接
    for code in set(w_codes + m_codes):
        if code not in fin_codes:
            continue
        row = conn.execute("""SELECT net_profit_yoy, revenue_yoy FROM financial
                              WHERE code=? AND net_profit_yoy IS NOT NULL
                              ORDER BY stat_date DESC LIMIT 1""", (code,)).fetchone()
        if row is None:
            continue
        net_yoy, rev_yoy = row[0], row[1]
        if net_yoy <= 0:
            continue
        if rev_yoy is not None and rev_yoy <= 0:
            continue
        pri, desc = priority(code)
        if pri == 99:
            continue
        candidates.append((code, pri, desc))
    candidates.sort(key=lambda x: x[1])
    final = candidates[:MAX_STOCKS]
    return [(c, name_map.get(c, c), d) for c, _, d in final]


# ===================== 回测引擎 =====================
def run_backtest(conn, df_daily, df_fin, name_map, start, end):
    close = df_daily['close'].unstack(level='code').loc[start:end].dropna(axis=1, how='all')
    bench = close[BENCH_CODE] if BENCH_CODE in close.columns else None
    stock_close = close[[c for c in close.columns if c != BENCH_CODE]]

    freq = 'W' if REBALANCE_FREQ == 'W' else 'ME'
    periods = stock_close.index.to_period(freq)
    rebalance_dates = stock_close.groupby(periods).apply(lambda x: x.index[-1]).values

    cash = INIT_CASH
    holdings = {}
    net_vals = []
    last_date = None

    for d_raw in rebalance_dates:
        d = pd.Timestamp(d_raw)
        sel = vectorized_select_stocks(conn, df_daily, df_fin, name_map, target_date=d)
        targets = [s[0] for s in sel if s[0] in stock_close.columns]

        if last_date is not None:
            mask = (stock_close.index > last_date) & (stock_close.index < d)
            for day in stock_close.index[mask]:
                val = cash
                for c, shares in holdings.items():
                    if c in stock_close.columns:
                        val += shares * stock_close.loc[day, c]
                net_vals.append((day, val))

        for c in list(holdings.keys()):
            if c in stock_close.columns:
                cash += holdings[c] * stock_close.loc[d, c] * (1 - COMMISSION - STAMP_DUTY)
        holdings.clear()

        if targets:
            prices = stock_close.loc[d, targets]
            avg_cash = cash / len(prices)
            for c in targets:
                price = prices[c]
                shares = int(avg_cash / (price * (1 + COMMISSION + SLIPPAGE))) // 100 * 100
                if shares > 0:
                    cash -= shares * price * (1 + COMMISSION + SLIPPAGE)
                    holdings[c] = shares

        val = cash
        for c, shares in holdings.items():
            if c in stock_close.columns:
                val += shares * stock_close.loc[d, c]
        net_vals.append((d, val))
        last_date = d

    if last_date and last_date < stock_close.index[-1]:
        mask = stock_close.index > last_date
        for day in stock_close.index[mask]:
            val = cash
            for c, shares in holdings.items():
                if c in stock_close.columns:
                    val += shares * stock_close.loc[day, c]
            net_vals.append((day, val))

    net_df = pd.DataFrame(net_vals, columns=['date', 'value']).set_index('date')
    net_df = net_df[~net_df.index.duplicated()].sort_index()
    if stock_close.index[0] not in net_df.index:
        net_df.loc[stock_close.index[0]] = INIT_CASH
        net_df = net_df.sort_index()
    net_value = net_df['value']
    perf = calc_performance(net_value, INIT_CASH)
    bench_ret = (bench / bench.iloc[0] - 1) * 100 if bench is not None else None
    return perf, bench_ret, net_value


def calc_performance(nv, init, r=0.03):
    ret = nv.pct_change(fill_method=None).dropna()
    total = (nv.iloc[-1] / init - 1) * 100
    days = (nv.index[-1] - nv.index[0]).days
    annual = ((nv.iloc[-1] / init) ** (365 / days) - 1) * 100 if days > 0 else 0
    sharpe = (ret.mean() * 252 - r) / (ret.std() * np.sqrt(252)) if ret.std() != 0 else 0
    dd = (nv / nv.cummax() - 1) * 100
    maxdd = dd.min()
    calmar = annual / abs(maxdd) if maxdd != 0 else 0
    win = (ret > 0).sum() / len(ret) * 100 if len(ret) > 0 else 0
    return {
        'total_return': total, 'annual_return': annual, 'sharpe': sharpe,
        'max_drawdown': maxdd, 'calmar': calmar, 'win_rate': win,
        'final_value': nv.iloc[-1]
    }


# ===================== 主流程 =====================
if __name__ == '__main__':
    conn = sqlite3.connect(DB_PATH)
    print("加载数据...")
    df_daily = load_all_data(conn)
    df_fin = load_financial_data(conn)
    name_map = pd.read_sql("SELECT code, name FROM stock_basic", conn).set_index('code')['name'].to_dict()

    print("\n===== 最新选股 =====")
    selected = vectorized_select_stocks(conn, df_daily, df_fin, name_map)
    if not selected:
        print("未选出股票，请尝试放宽条件。")
    else:
        print("\n最终选股池（按信号优先级排序）：")
        for c, n, s in selected:
            print(f"  {c} {n}  信号: {s}")

    with open('selected_stocks.txt', 'w', encoding='utf-8') as f:
        for c, n, _ in selected:
            plain = c.replace('sh.', '').replace('sz.', '')
            f.write(f"{plain},{n}\n")
    with open('selected_stocks_detail.txt', 'w', encoding='utf-8') as f:
        for c, n, s in selected:
            plain = c.replace('sh.', '').replace('sz.', '')
            f.write(f"{plain},{n},{s}\n")
    print(f"\n结果已导出：selected_stocks.txt 和 selected_stocks_detail.txt")

    print("\n===== 运行回测 =====")
    perf, bench_ret, nv = run_backtest(conn, df_daily, df_fin, name_map, BACKTEST_START, BACKTEST_END)

    conn.close()

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

    try:
        import matplotlib.pyplot as plt
        plt.rcParams['font.sans-serif'] = ['Arial Unicode MS', 'PingFang HK', 'Heiti TC']
        plt.rcParams['axes.unicode_minus'] = False
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8))
        ax1.plot(nv.index, nv.values, label='策略净值', color='blue')
        if bench_ret is not None:
            bench_nv = (bench_ret / 100 + 1) * INIT_CASH
            ax1.plot(bench_nv.index, bench_nv.values, label='沪深300', alpha=0.6)
        ax1.legend(); ax1.grid(); ax1.set_title('策略净值 vs 沪深300')
        dd = (nv / nv.cummax() - 1) * 100
        ax2.fill_between(dd.index, dd.values, 0, color='red', alpha=0.3)
        ax2.grid(); ax2.set_title('回撤 %')
        plt.tight_layout(); plt.show()
    except Exception as e:
        print(f"绘图失败: {e}")

    print("\n选股回测完成。")