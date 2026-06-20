#!/usr/bin/env python3
"""
选股 + 回测 + 导出 + 绘图（最终版）
================================
选股条件：
  1. 年线（250日均线）短期斜率 > 0（最近20日变化为正）
  2. 去年（最近一个完整自然年）放量收红，即视为趋势扭转向上
  3. 20日日均成交额 >= 2000 万元
  4. 连续最近2个季度净利润同比增长 > 0（若扣非存在则也需 > 0；若营收存在则也需 > 0）
  5. 月线/周线技术信号（优先级：周线布林扩张 > 月线布林扩张 > 周线回踩 > 月线回踩）：
     - 布林扩张：短期带宽均线 > 长期带宽均线，且短期均线方向向上
     - 回踩信号：均线附近“靠近”或“跌破”但幅度有限
        · 周线：30根周线内至少2次回踩，均线方向向上，收盘 >= 20周线
        · 月线：18根月线内至少1次回踩，收盘 >= 20月线（不要求均线方向）
"""
import sqlite3, pandas as pd, numpy as np
from datetime import datetime, timedelta

# ===================== 基本配置 =====================
DB_PATH = 'stocks_2y.db'
BENCH_CODE = 'sh.000300'                        # 基准指数（沪深300）

# ===================== 可调参数 =====================
BACKTEST_START = (datetime.now() - timedelta(days=365*2)).strftime('%Y-%m-%d')  # 回测起始：两年前
BACKTEST_END = datetime.now().strftime('%Y-%m-%d')                              # 回测结束：今日
INIT_CASH = 1_000_000                               # 初始资金
MAX_STOCKS = 200                                     # 最大持仓股数
TOLERANCE = 0.15                                     # 回踩下探容忍度（跌破情形）
TOLERANCE_NEAR = 0.10                                # 回踩靠近容忍度（未跌破）
MIN_LIQUIDITY = 2000                                 # 20日日均成交额最低阈值（万元）
CONSECUTIVE_FIN_PERIODS = 2                          # 连续盈利季度数
MONTHLY_MA = 20                                      # 月线慢速均线周期
WEEKLY_MA = 20                                       # 周线慢速均线周期
MONTHLY_WINDOW = 18                                  # 月线回踩窗口（根数）
WEEKLY_WINDOW = 30                                   # 周线回踩窗口（根数）
REBALANCE_FREQ = 'W'                                 # 调仓频率：W=周, M=月
COMMISSION = 0.0001                                  # 佣金（万1）
SLIPPAGE = 0.001                                     # 滑点
STAMP_DUTY = 0.001                                   # 卖出印花税

# 布林带参数（业界标准）
W_BB_PERIOD = 20          # 周线布林周期
W_BB_STD_MULT = 2         # 标准差倍数
W_BB_SHORT_MA = 5         # 短期带宽均线
W_BB_LONG_MA = 20         # 长期带宽均线

M_BB_PERIOD = 20          # 月线布林周期
M_BB_STD_MULT = 2         # 标准差倍数
M_BB_SHORT_MA = 5         # 短期带宽均线
M_BB_LONG_MA = 20         # 长期带宽均线

# ===================== 数据加载 =====================
def load_all_data(conn):
    """
    从 daily 表加载日线数据
    返回 multi-index (code, date) 的 DataFrame，至少包含 open,high,low,close,volume,amount
    """
    query = """SELECT code, date, open, high, low, close, volume, amount
               FROM daily WHERE date >= '2018-01-01' ORDER BY code, date"""
    df = pd.read_sql(query, conn, parse_dates=['date'])
    return df.set_index(['code', 'date']).sort_index()


def load_financial_data(conn):
    """
    从 financial 表加载财务数据，计算生效日期（pub_date + 10天）
    """
    query = """SELECT code, stat_date, pub_date, net_profit_yoy, revenue_yoy, yoy_pni
               FROM financial WHERE net_profit_yoy IS NOT NULL ORDER BY code, stat_date"""
    df = pd.read_sql(query, conn, parse_dates=['stat_date', 'pub_date'])
    # 发布日 +10 天，避免未来函数
    df['effective_date'] = df['pub_date'].fillna(df['stat_date'] + timedelta(days=30)) + timedelta(days=10)
    return df


# ===================== 技术形态检测 =====================
def detect_retest_signal(price_df, ma_period, tolerance=TOLERANCE,
                         tolerance_near=TOLERANCE_NEAR, window=24, min_touches=2,
                         require_ma_up=True):
    """
    均线回踩信号检测
    参数：
        price_df: 包含 close, low 的 DataFrame
        ma_period: 慢速均线周期
        window: 回踩观察窗口（K线根数）
        min_touches: 最少回踩次数
        require_ma_up: 是否要求慢速均线方向向上（月线回踩不要求）
    回踩定义：
        - 下探：最低价 < 均线 且 (均线 - 最低价)/均线 <= tolerance
        - 靠近：最低价 >= 均线 且 (最低价 - 均线)/均线 <= tolerance_near
    额外要求：当前收盘价 >= 慢速均线
    """
    close = price_df['close']
    low = price_df['low']
    ma_slow = close.rolling(ma_period).mean()          # 计算慢速均线

    # 均线方向（向上 = 近10周期差值均值 > 0）
    if require_ma_up:
        ma_up = ma_slow.diff(10) / 10 > 0
    else:
        ma_up = pd.Series(True, index=ma_slow.index)

    # 两种回踩事件
    touch_down = (low < ma_slow) & ((ma_slow - low) / ma_slow <= tolerance)
    touch_near = (low >= ma_slow) & ((low - ma_slow) / ma_slow <= tolerance_near)
    touch = touch_down | touch_near

    # 窗口内回踩次数达标
    min_periods = min(ma_period, window)
    enough_touches = touch.rolling(window, min_periods=min_periods).sum() >= min_touches

    # 收盘价条件：>= 均线
    close_above_ma = close >= ma_slow

    return ma_up & enough_touches & close_above_ma


def detect_bollinger_expansion(price_df, period=20, std_mult=2, short_ma=5, long_ma=20):
    """
    布林带扩张检测
    条件：短期带宽均线 > 长期带宽均线，且短期均线最近3期方向向上
    """
    close = price_df['close']
    middle = close.rolling(period).mean()
    std = close.rolling(period).std()
    bandwidth = std_mult * std                           # 带宽 = 标准差 * 倍数
    bw_short = bandwidth.rolling(short_ma).mean()       # 短期带宽均线
    bw_long = bandwidth.rolling(long_ma).mean()         # 长期带宽均线
    expanding = (bw_short > bw_long) & (bw_short.diff(3) > 0)
    return expanding


# ===================== 选股主逻辑 =====================
def vectorized_select_stocks(conn, df_daily, df_fin, name_map, target_date=None):
    """
    批量选股，返回 [(code, name, signal_desc), ...]
    target_date: 用于回测时指定某一天进行选股；None 则使用数据最新日期
    """
    if target_date is None:
        target_date = df_daily.index.get_level_values('date').max()
    target_date = pd.Timestamp(target_date)

    # 取出目标日期之前的数据
    df = df_daily.loc[df_daily.index.get_level_values('date') <= target_date].copy()
    # 排除基准指数
    stock_codes = [c for c in df.index.get_level_values('code').unique() if c != BENCH_CODE]
    df_stocks = df[df.index.get_level_values('code').isin(stock_codes)]

    # ---------- 基础指标计算 ----------
    grouped = df_stocks.groupby(level='code')
    # 250日均线及斜率
    df_stocks['ma250'] = grouped['close'].transform(lambda x: x.rolling(250).mean())
    df_stocks['ma250_slope'] = grouped['ma250'].transform(lambda x: x.diff(20) / 20)  # 最近20日斜率
    # 成交额（万元）及20日均值
    df_stocks['amount_wan'] = df_stocks['amount'] / 10000   # amount 单位：元
    df_stocks['amount_ma20'] = grouped['amount_wan'].transform(lambda x: x.rolling(20).mean())

    # ---------- 自然年条件计算 ----------
    df_stocks['year'] = df_stocks.index.get_level_values('date').year
    yearly = df_stocks.groupby(['code', 'year']).agg(
        first_open=('open', 'first'),   # 年开盘价
        last_close=('close', 'last'),   # 年收盘价
        total_volume=('volume', 'sum')  # 年总成交量
    ).sort_index()

    def check_yearly_conditions(code):
        """
        检查一只股票的年线条件：
        - 去年（最近完整自然年）收红且放量 -> 趋势向上
        - 次新股：上市以来上涨即可
        返回 (red, vol_up, trend_up) 均为布尔值
        """
        if code not in yearly.index.get_level_values('code'):
            return False, False, False
        df_code = yearly.loc[code].sort_index()
        years = df_code.index.tolist()
        if not years:
            return False, False, False

        last_year = target_date.year
        prev_year = last_year - 1   # 去年

        # 1. 去年收红放量
        red, vol_up = False, False
        if prev_year in years:
            row = df_code.loc[prev_year]
            red = row['last_close'] > row['first_open']        # 收红：收盘 > 开盘
            if prev_year - 1 in years:
                prev_row = df_code.loc[prev_year - 1]
                vol_up = row['total_volume'] > prev_row['total_volume']  # 放量：成交量大于前一年
            else:
                vol_up = True   # 无前一年数据，仅要求收红
        else:
            # 没有去年数据（次新股），要求上市以来上涨
            first_open = df_code.iloc[0]['first_open']
            last_close_all = df_code.iloc[-1]['last_close']
            red = last_close_all > first_open
            vol_up = True

        # 2. 三年趋势向上：去年放量收红即视为趋势扭转
        complete_years = [y for y in years if y < last_year]
        if len(complete_years) >= 1:
            trend_up = red and vol_up
        else:
            trend_up = True  # 不足一个完整年份，自动满足

        return red, vol_up, trend_up

    # 对每只股票应用年线条件检查
    yearly_ok = {}
    for code in stock_codes:
        r, v, t = check_yearly_conditions(code)
        yearly_ok[code] = (r and v and t)

    # ---------- 基础筛选 ----------
    latest = df_stocks.groupby(level='code').tail(1)   # 每只股票最新一天的数据
    base_codes = []
    for code in latest.index.get_level_values('code'):
        row = latest.loc[code]
        if (row['ma250_slope'] > 0 and                    # 年线短期趋势向上
                yearly_ok.get(code, False) and                # 年线条件通过
                row['amount_ma20'] >= MIN_LIQUIDITY):         # 成交额达标
            base_codes.append(code)

    print(f"基础筛选后：{len(base_codes)} 只")
    if not base_codes:
        return []

    # 只保留通过基础筛选的股票
    df_f = df_stocks[df_stocks.index.get_level_values('code').isin(base_codes)]
    if df_f.empty:
        return []

    # 构建宽表：行为日期，列为股票
    daily_close = df_f['close'].unstack(level='code')
    daily_low   = df_f['low'].unstack(level='code')

    # ---------- 周线信号 ----------
    w_close = daily_close.resample('W').last()            # 周线收盘价
    w_low = daily_low.resample('W').min()                 # 周线最低价
    w_df = pd.DataFrame({'close': w_close.stack(), 'low': w_low.stack()})
    w_retest = detect_retest_signal(w_df, ma_period=WEEKLY_MA, window=WEEKLY_WINDOW,
                                    min_touches=2, require_ma_up=True)
    w_bb = detect_bollinger_expansion(w_df, period=W_BB_PERIOD, std_mult=W_BB_STD_MULT,
                                      short_ma=W_BB_SHORT_MA, long_ma=W_BB_LONG_MA)
    w_signal = w_retest | w_bb
    w_codes = w_signal.groupby(level='code').tail(1).pipe(
        lambda x: x[x].index.get_level_values('code').tolist()
    )

    # ---------- 月线信号 ----------
    m_close = daily_close.resample('ME').last()           # 月线收盘价
    m_low = daily_low.resample('ME').min()                # 月线最低价
    m_df = pd.DataFrame({'close': m_close.stack(), 'low': m_low.stack()})
    m_retest = detect_retest_signal(m_df, ma_period=MONTHLY_MA, window=MONTHLY_WINDOW,
                                    min_touches=1, require_ma_up=False)
    m_bb = detect_bollinger_expansion(m_df, period=M_BB_PERIOD, std_mult=M_BB_STD_MULT,
                                      short_ma=M_BB_SHORT_MA, long_ma=M_BB_LONG_MA)
    m_signal = m_retest | m_bb
    m_codes = m_signal.groupby(level='code').tail(1).pipe(
        lambda x: x[x].index.get_level_values('code').tolist()
    )

    # 单独记录布林扩张的代码，用于优先级判定
    w_bb_codes = w_bb.groupby(level='code').tail(1).pipe(lambda x: x[x].index.get_level_values('code').tolist())
    m_bb_codes = m_bb.groupby(level='code').tail(1).pipe(lambda x: x[x].index.get_level_values('code').tolist())

    def priority(code):
        """返回信号的优先级和描述"""
        wb, mb = code in w_bb_codes, code in m_bb_codes
        if wb: return 1, '周线布林扩张'
        if mb: return 2, '月线布林扩张'
        if code in w_codes: return 3, '周线回踩'
        if code in m_codes: return 4, '月线回踩'
        return 99, ''

    # ---------- 财务筛选 ----------
    fin_before = df_fin[df_fin['effective_date'] <= target_date]
    if fin_before.empty:
        return []
    # 取最近 N 个季度的数据
    fin_latest = fin_before.sort_values('effective_date').groupby('code').tail(CONSECUTIVE_FIN_PERIODS)
    # 要求每个季度净利润增长率 > 0
    fin_pass = fin_latest.groupby('code').filter(
        lambda x: all(x['net_profit_yoy'] > 0) and len(x) == CONSECUTIVE_FIN_PERIODS)
    # 如果存在扣非净利润数据，则也必须 > 0
    if 'yoy_pni' in df_fin.columns:
        fin_pass = fin_pass[fin_pass['yoy_pni'].isna() | (fin_pass['yoy_pni'] > 0)]
    fin_codes = fin_pass['code'].unique().tolist()
    print(f"财务符合：{len(fin_codes)} 只")

    # ---------- 汇总结果 ----------
    candidates = []
    for code in set(w_codes + m_codes):
        if code not in fin_codes:
            continue
        # 二次确认最新净利润和营收
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

    # 消除随机性：先按优先级，再按代码排序
    candidates.sort(key=lambda x: (x[1], x[0]))
    final = candidates[:MAX_STOCKS]
    return [(c, name_map.get(c, c), d) for c, _, d in final]


# ===================== 回测引擎 =====================
def run_backtest(conn, df_daily, df_fin, name_map, start, end):
    """
    执行回测，返回 (绩效字典, 基准收益率序列, 策略净值序列)
    """
    # 准备价格数据
    close = df_daily['close'].unstack(level='code').loc[start:end].dropna(axis=1, how='all')
    bench = close[BENCH_CODE] if BENCH_CODE in close.columns else None
    stock_close = close[[c for c in close.columns if c != BENCH_CODE]]

    # 确定再平衡日期
    freq = 'W' if REBALANCE_FREQ == 'W' else 'ME'
    periods = stock_close.index.to_period(freq)
    rebalance_dates = stock_close.groupby(periods).apply(lambda x: x.index[-1]).values

    cash = INIT_CASH
    holdings = {}
    net_vals = []                # (日期, 总资产)
    last_date = None

    for d_raw in rebalance_dates:
        d = pd.Timestamp(d_raw)
        # 调仓日选股
        sel = vectorized_select_stocks(conn, df_daily, df_fin, name_map, target_date=d)
        targets = [s[0] for s in sel if s[0] in stock_close.columns]

        # 记录调仓日之间的每日净值
        if last_date is not None:
            mask = (stock_close.index > last_date) & (stock_close.index < d)
            for day in stock_close.index[mask]:
                val = cash
                for c, shares in holdings.items():
                    if c in stock_close.columns:
                        val += shares * stock_close.loc[day, c]
                net_vals.append((day, val))

        # 卖出全部
        for c in list(holdings.keys()):
            if c in stock_close.columns:
                cash += holdings[c] * stock_close.loc[d, c] * (1 - COMMISSION - STAMP_DUTY)
        holdings.clear()

        # 等额买入新标的
        if targets:
            prices = stock_close.loc[d, targets]
            avg_cash = cash / len(prices)
            for c in targets:
                price = prices[c]
                # 计算整百股数
                shares = int(avg_cash / (price * (1 + COMMISSION + SLIPPAGE))) // 100 * 100
                if shares > 0:
                    cash -= shares * price * (1 + COMMISSION + SLIPPAGE)
                    holdings[c] = shares

        # 记录调仓日净值
        val = cash
        for c, shares in holdings.items():
            if c in stock_close.columns:
                val += shares * stock_close.loc[d, c]
        net_vals.append((d, val))
        last_date = d

    # 记录最后一次调仓后的每日净值
    if last_date and last_date < stock_close.index[-1]:
        mask = stock_close.index > last_date
        for day in stock_close.index[mask]:
            val = cash
            for c, shares in holdings.items():
                if c in stock_close.columns:
                    val += shares * stock_close.loc[day, c]
            net_vals.append((day, val))

    # 整理为时间序列
    net_df = pd.DataFrame(net_vals, columns=['date', 'value']).set_index('date')
    net_df = net_df[~net_df.index.duplicated()].sort_index()
    # 确保回测起始点在序列中
    if stock_close.index[0] not in net_df.index:
        net_df.loc[stock_close.index[0]] = INIT_CASH
        net_df = net_df.sort_index()
    net_value = net_df['value']
    perf = calc_performance(net_value, INIT_CASH)
    bench_ret = (bench / bench.iloc[0] - 1) * 100 if bench is not None else None
    return perf, bench_ret, net_value


def calc_performance(nv, init, r=0.03):
    """计算各项绩效指标"""
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

    # ---------- 最新选股 ----------
    print("\n===== 最新选股 =====")
    selected = vectorized_select_stocks(conn, df_daily, df_fin, name_map)
    if not selected:
        print("未选出股票，请尝试放宽条件。")
    else:
        print("\n最终选股池（按信号优先级排序）：")
        for c, n, s in selected:
            print(f"  {c} {n}  信号: {s}")

    # 导出结果
    with open('selected_stocks.txt', 'w', encoding='utf-8') as f:
        for c, n, _ in selected:
            plain = c.replace('sh.', '').replace('sz.', '')
            f.write(f"{plain},{n}\n")
    with open('selected_stocks_detail.txt', 'w', encoding='utf-8') as f:
        for c, n, s in selected:
            plain = c.replace('sh.', '').replace('sz.', '')
            f.write(f"{plain},{n},{s}\n")
    print(f"\n结果已导出：selected_stocks.txt 和 selected_stocks_detail.txt")

    # ---------- 运行回测 ----------
    print("\n===== 运行回测 =====")
    perf, bench_ret, nv = run_backtest(conn, df_daily, df_fin, name_map, BACKTEST_START, BACKTEST_END)
    conn.close()

    # 打印绩效报告
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

    # 绘制净值曲线
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