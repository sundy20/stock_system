#!/usr/bin/env python3
"""
选股 + 回测 + 导出（中线趋势升级版，最终参数）
策略条件：
  前置剔除：上市≥24月、非ST、近20日停牌≤2天
  年度趋势（任一条满足）：
    A. 滚动12个月涨幅≥15%且放量≥30%，收盘价≥年线，年线斜率≥0
    B. 上一个完整自然年收红且放量
  流动性：20日均成交≥2000万，120日均成交≥1500万
  财务（初期放宽）：连续2季度归母净利润同比>0%，扣非净利润同比>0%（必须存在）
  技术信号（优先级：周线回踩 > 月线回踩 > 周线布林扩张 > 月线布林扩张）
    回踩：均线向上，有效回踩（下探≤X% 或 靠近≤Y%），间隔计数
    布林扩张：中轨向上，标准化带宽短期均线上穿长期且方向向上，周线超买≤布林上轨×1.10
输出：selected_stocks.txt, selected_stocks_detail.txt（含止损止盈参考线）
"""
import sqlite3, pandas as pd, numpy as np
from datetime import datetime, timedelta

DB_PATH = 'stocks_2y.db'
BENCH_CODE = 'sh.000300'

# ===================== 可调参数 =====================
BACKTEST_START = (datetime.now() - timedelta(days=365*2)).strftime('%Y-%m-%d')
BACKTEST_END = datetime.now().strftime('%Y-%m-%d')
INIT_CASH = 1_000_000
MAX_STOCKS = 200

# 流动性
MIN_20D_AMOUNT = 2000          # 万元
MIN_120D_AMOUNT = 1500         # 万元

# 年度趋势滚动参数
ROLLING_DAYS = 250             # 近似12个月
ROLLING_PRICE_UP = 0.15        # 滚动涨幅≥15%
ROLLING_VOL_UP = 0.30          # 滚动日均成交额增长≥30%
MA_SLOPE_THRESHOLD = 0         # 年线斜率≥0

# 回踩参数
WEEKLY_RETEST_DOWN = 0.08      # 周线下探阈值≤8%
WEEKLY_RETEST_NEAR = 0.05      # 周线靠近阈值≤5%
WEEKLY_RETEST_WINDOW = 50      # 统计窗口（根）
WEEKLY_RETEST_MIN_GAP = 5      # 两次回踩最小间隔
MONTHLY_RETEST_DOWN = 0.12     # 月线下探≤12%
MONTHLY_RETEST_NEAR = 0.08     # 月线靠近≤8%
MONTHLY_RETEST_WINDOW = 18
MONTHLY_RETEST_MIN_GAP = 3

# 布林参数
BB_PERIOD = 20
BB_STD_MULT = 2
BB_SHORT_MA = 5
BB_LONG_MA = 20
BB_OVERBOUGHT = 1.10           # 周线超买限制（最终放宽至1.10）

# 均线方向：当期 > 前3期均值
MA_DIR_PERIOD = 3

# 财务（初期放宽）
FIN_CONSEC = 2
MIN_PROFIT_YOY = 0.0           # >0%
MIN_PNI_YOY = 0.0              # >0%，必须存在

# 交易费率
COMMISSION = 0.0001
SLIPPAGE = 0.001
STAMP_DUTY = 0.001
REBALANCE_FREQ = 'W'


# ===================== 数据加载 =====================
def load_all_data(conn):
    query = """SELECT code, date, open, high, low, close, volume, amount
               FROM daily WHERE date >= '2018-01-01' ORDER BY code, date"""
    df = pd.read_sql(query, conn, parse_dates=['date'])
    return df.set_index(['code', 'date']).sort_index()


def load_financial_data(conn):
    query = """SELECT code, stat_date, pub_date, net_profit_yoy, revenue_yoy, yoy_pni
               FROM financial WHERE net_profit_yoy IS NOT NULL ORDER BY code, stat_date"""
    df = pd.read_sql(query, conn, parse_dates=['stat_date', 'pub_date'])
    df['effective_date'] = df['pub_date'].fillna(df['stat_date'] + timedelta(days=30)) + timedelta(days=10)
    return df


# ===================== 前置剔除 =====================
def get_valid_codes(conn, df_daily, target_date):
    """返回符合前置剔除条件的股票代码列表"""
    # 1. 上市满24个月（利用stock_basic中的list_date）
    basic = pd.read_sql("SELECT code, list_date FROM stock_basic", conn, parse_dates=['list_date'])
    basic['months'] = ((target_date - basic['list_date']).dt.days / 30.44)
    valid_listed = basic[basic['months'] >= 24]['code'].tolist()

    # 2. 非ST（已在获取列表时过滤）
    # 3. 近20个交易日停牌天数 ≤ 2天
    df_recent = df_daily.loc[df_daily.index.get_level_values('date') >= target_date - timedelta(days=40)]
    trading_days = df_recent.groupby(level='code').size()
    # 最近20个交易日应有约20条记录，缺失天数 ≤ 2
    valid_trading = trading_days[trading_days >= 18].index.tolist()

    return list(set(valid_listed) & set(valid_trading))


# ===================== 年度趋势条件（滚动+自然年融合） =====================
def check_annual_trend(code, df_stocks, target_date, yearly_data):
    """返回是否满足年度趋势（滚动或自然年任一）"""
    code_data = df_stocks.loc[code].sort_index()
    # ---- 滚动12个月验证 ----
    rolling_ok = False
    if len(code_data) >= ROLLING_DAYS * 2:
        recent = code_data.iloc[-ROLLING_DAYS:]
        prev = code_data.iloc[-2*ROLLING_DAYS:-ROLLING_DAYS]
        # 价格涨幅
        price_up = (recent['close'].iloc[-1] - prev['close'].iloc[-1]) / prev['close'].iloc[-1] >= ROLLING_PRICE_UP
        # 量能放大：日均成交额增长
        vol_ratio = recent['amount'].mean() / prev['amount'].mean() - 1
        vol_up = vol_ratio >= ROLLING_VOL_UP
        # 年线位置与斜率
        ma250 = code_data['close'].rolling(250).mean()
        above_ma = code_data['close'].iloc[-1] >= ma250.iloc[-1]
        slope = (ma250.iloc[-1] - ma250.iloc[-20]) / 20 if len(ma250) >= 20 else -1
        slope_ok = slope >= MA_SLOPE_THRESHOLD
        rolling_ok = price_up and vol_up and above_ma and slope_ok

    # ---- 自然年验证（去年放量收红） ----
    natural_ok = False
    if code in yearly_data.index.get_level_values('code'):
        df_y = yearly_data.loc[code].sort_index()
        years = df_y.index.tolist()
        last_year = target_date.year - 1
        if last_year in years:
            row = df_y.loc[last_year]
            red = row['last_close'] > row['first_open']
            if last_year - 1 in years:
                vol_up = row['total_volume'] > df_y.loc[last_year - 1]['total_volume']
            else:
                vol_up = True
            natural_ok = red and vol_up
    return rolling_ok or natural_ok


# ===================== 回踩信号检测（含间隔计数） =====================
def detect_retest_with_gap(price_df, ma_period, tolerance_down, tolerance_near,
                           window, min_gap, require_ma_up=True):
    close = price_df['close']
    low = price_df['low']
    ma = close.rolling(ma_period).mean()

    # 均线方向：当期 > 前3期均值
    if require_ma_up:
        ma_up = ma > ma.shift(1).rolling(MA_DIR_PERIOD).mean()
    else:
        ma_up = pd.Series(True, index=ma.index)

    # 有效回踩事件
    touch_down = (low < ma) & ((ma - low) / ma <= tolerance_down)
    touch_near = (low >= ma) & ((low - ma) / ma <= tolerance_near)
    touch = (touch_down | touch_near) & ma_up

    # 间隔计数：连续满足只计1次，两次事件间隔至少min_gap根K线
    touch_int = touch.astype(int)
    # 标记事件开始：当前为1且前一根为0，或者是第一根K线即为1
    event_start = (touch_int.diff() == 1) | (touch_int == 1)
    # 计数：用rolling sum统计窗口内的事件开始次数
    event_count = event_start.rolling(window, min_periods=1).sum()
    has_event = event_count >= 1
    close_ok = close >= ma
    return has_event & close_ok


# ===================== 布林扩张检测（标准化带宽，中轨方向） =====================
def detect_bb_expand(price_df, period=20, std_mult=2, short_ma=5, long_ma=20,
                     overbought_limit=None):
    close = price_df['close']
    mid = close.rolling(period).mean()
    std = close.rolling(period).std()
    upper = mid + std_mult * std
    lower = mid - std_mult * std
    bandwidth = (upper - lower) / mid          # 标准化带宽
    bw_short = bandwidth.rolling(short_ma).mean()
    bw_long = bandwidth.rolling(long_ma).mean()
    # 中轨方向
    mid_up = mid > mid.shift(1).rolling(MA_DIR_PERIOD).mean()
    # 股价在中轨上方
    above_mid = close > mid
    # 带宽扩张
    expanding = (bw_short > bw_long) & (bw_short.diff(3) > 0)
    cond = above_mid & mid_up & expanding
    if overbought_limit:
        cond = cond & (close <= upper * overbought_limit)
    return cond


# ===================== 选股主逻辑 =====================
def vectorized_select_stocks(conn, df_daily, df_fin, name_map, industry_map, target_date=None):
    if target_date is None:
        target_date = df_daily.index.get_level_values('date').max()
    target_date = pd.Timestamp(target_date)

    # 前置剔除
    valid_codes = get_valid_codes(conn, df_daily, target_date)
    df_stocks = df_daily[df_daily.index.get_level_values('code').isin(valid_codes)].copy()
    if df_stocks.empty:
        print("前置剔除后无股票")
        return []

    # 基础指标
    grouped = df_stocks.groupby(level='code')
    df_stocks['amount_wan'] = df_stocks['amount'] / 10000
    df_stocks['amount_ma20'] = grouped['amount_wan'].transform(lambda x: x.rolling(20).mean())
    df_stocks['amount_ma120'] = grouped['amount_wan'].transform(lambda x: x.rolling(120).mean())
    df_stocks['year'] = df_stocks.index.get_level_values('date').year
    yearly = df_stocks.groupby(['code', 'year']).agg(
        first_open=('open', 'first'),
        last_close=('close', 'last'),
        total_volume=('volume', 'sum')
    ).sort_index()

    # 年度趋势
    annual_ok = {}
    for code in valid_codes:
        annual_ok[code] = check_annual_trend(code, df_stocks, target_date, yearly)

    # 基础筛选
    latest = df_stocks.groupby(level='code').tail(1)
    base_codes = []
    for code in latest.index.get_level_values('code'):
        row = latest.loc[code]
        if (annual_ok.get(code, False) and
                row['amount_ma20'] >= MIN_20D_AMOUNT and
                row['amount_ma120'] >= MIN_120D_AMOUNT):
            base_codes.append(code)
    print(f"基础筛选后：{len(base_codes)} 只")
    if not base_codes:
        return []

    # 技术信号
    df_f = df_stocks[df_stocks.index.get_level_values('code').isin(base_codes)]
    daily_close = df_f['close'].unstack(level='code')
    daily_low   = df_f['low'].unstack(level='code')

    # 周线
    w_close = daily_close.resample('W').last()
    w_low = daily_low.resample('W').min()
    w_df = pd.DataFrame({'close': w_close.stack(), 'low': w_low.stack()})
    w_retest = detect_retest_with_gap(w_df, ma_period=20, tolerance_down=WEEKLY_RETEST_DOWN,
                                      tolerance_near=WEEKLY_RETEST_NEAR, window=WEEKLY_RETEST_WINDOW,
                                      min_gap=WEEKLY_RETEST_MIN_GAP, require_ma_up=True)
    w_bb = detect_bb_expand(w_df, period=BB_PERIOD, std_mult=BB_STD_MULT, short_ma=BB_SHORT_MA,
                            long_ma=BB_LONG_MA, overbought_limit=BB_OVERBOUGHT)
    w_signal = w_retest | w_bb

    # 月线
    m_close = daily_close.resample('ME').last()
    m_low = daily_low.resample('ME').min()
    m_df = pd.DataFrame({'close': m_close.stack(), 'low': m_low.stack()})
    m_retest = detect_retest_with_gap(m_df, ma_period=20, tolerance_down=MONTHLY_RETEST_DOWN,
                                      tolerance_near=MONTHLY_RETEST_NEAR, window=MONTHLY_RETEST_WINDOW,
                                      min_gap=MONTHLY_RETEST_MIN_GAP, require_ma_up=True)
    m_bb = detect_bb_expand(m_df, period=BB_PERIOD, std_mult=BB_STD_MULT, short_ma=BB_SHORT_MA,
                            long_ma=BB_LONG_MA, overbought_limit=None)
    m_signal = m_retest | m_bb

    w_codes = w_signal.groupby(level='code').tail(1).pipe(lambda x: x[x].index.tolist())
    m_codes = m_signal.groupby(level='code').tail(1).pipe(lambda x: x[x].index.tolist())
    w_retest_codes = w_retest.groupby(level='code').tail(1).pipe(lambda x: x[x].index.tolist())
    m_retest_codes = m_retest.groupby(level='code').tail(1).pipe(lambda x: x[x].index.tolist())
    w_bb_codes = w_bb.groupby(level='code').tail(1).pipe(lambda x: x[x].index.tolist())
    m_bb_codes = m_bb.groupby(level='code').tail(1).pipe(lambda x: x[x].index.tolist())

    def priority(code):
        if code in w_retest_codes: return 1, '周线均线回踩'
        if code in m_retest_codes: return 2, '月线均线回踩'
        if code in w_bb_codes: return 3, '周线布林扩张'
        if code in m_bb_codes: return 4, '月线布林扩张'
        return 99, ''

    # 财务筛选（放宽到 >0%）
    fin_before = df_fin[df_fin['effective_date'] <= target_date]
    if fin_before.empty:
        return []
    fin_latest = fin_before.sort_values('effective_date').groupby('code').tail(FIN_CONSEC)
    # 要求扣非必须存在且>MIN_PNI_YOY(0)
    fin_pass = fin_latest.groupby('code').filter(
        lambda x: (len(x) == FIN_CONSEC) and
                  all(x['net_profit_yoy'] > MIN_PROFIT_YOY) and
                  all(x['yoy_pni'].notna()) and all(x['yoy_pni'] > MIN_PNI_YOY)
    )
    fin_codes = fin_pass['code'].unique().tolist()
    print(f"财务符合：{len(fin_codes)} 只")

    candidates = []
    for code in set(w_codes + m_codes):
        if code not in fin_codes:
            continue
        pri, desc = priority(code)
        if pri == 99:
            continue
        candidates.append((code, pri, desc))
    candidates.sort(key=lambda x: (x[1], x[0]))
    final = candidates[:MAX_STOCKS]
    return [(c, name_map.get(c, c), industry_map.get(c, ''), d) for c, _, d in final]


# ===================== 回测引擎 =====================
def run_backtest(conn, df_daily, df_fin, name_map, industry_map, start, end):
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
        sel = vectorized_select_stocks(conn, df_daily, df_fin, name_map, industry_map, target_date=d)
        targets = [s[0] for s in sel if s[0] in stock_close.columns]

        if last_date is not None:
            mask = (stock_close.index > last_date) & (stock_close.index < d)
            for day in stock_close.index[mask]:
                val = cash + sum(holdings.get(c, 0) * stock_close.loc[day, c] for c in holdings if c in stock_close.columns)
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

        val = cash + sum(holdings.get(c, 0) * stock_close.loc[d, c] for c in holdings if c in stock_close.columns)
        net_vals.append((d, val))
        last_date = d

    if last_date and last_date < stock_close.index[-1]:
        mask = stock_close.index > last_date
        for day in stock_close.index[mask]:
            val = cash + sum(holdings.get(c, 0) * stock_close.loc[day, c] for c in holdings if c in stock_close.columns)
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
    basic = pd.read_sql("SELECT code, name, industry FROM stock_basic", conn)
    name_map = basic.set_index('code')['name'].to_dict()
    industry_map = basic.set_index('code')['industry'].to_dict()

    print("\n===== 最新选股 =====")
    selected = vectorized_select_stocks(conn, df_daily, df_fin, name_map, industry_map)
    if not selected:
        print("未选出股票")
    else:
        # 计算参考指标
        df_f = df_daily[df_daily.index.get_level_values('code').isin([s[0] for s in selected])]
        close = df_f['close'].unstack(level='code')
        weekly_close = close.resample('W').last()
        monthly_close = close.resample('ME').last()
        w20 = weekly_close.rolling(20).mean().iloc[-1] if len(weekly_close) >= 20 else None
        m20 = monthly_close.rolling(20).mean().iloc[-1] if len(monthly_close) >= 20 else None
        w_std = weekly_close.rolling(20).std().iloc[-1]
        w_mid = weekly_close.rolling(20).mean().iloc[-1]
        w_upper = w_mid + 2 * w_std

        print("\n最终选股池（含参考线）：")
        for c, n, ind, s in selected:
            print(f"{c} {n} [{ind}] {s}")
        with open('selected_stocks.txt', 'w') as f:
            for c, n, _, _ in selected:
                f.write(f"{c.replace('sh.','').replace('sz.','')},{n}\n")
        with open('selected_stocks_detail.txt', 'w') as f:
            f.write("代码,名称,行业,信号,20周线,20月线,布林上轨(周),止损参考(10%)\n")
            for c, n, ind, s in selected:
                plain = c.replace('sh.','').replace('sz.','')
                w20v = w20.get(c, '') if w20 is not None else ''
                m20v = m20.get(c, '') if m20 is not None else ''
                wupp = w_upper.get(c, '') if w_upper is not None else ''
                last_close = close[c].iloc[-1] if c in close.columns else ''
                stop_loss = round(last_close * 0.9, 2) if isinstance(last_close, (int, float)) else ''
                f.write(f"{plain},{n},{ind},{s},{w20v},{m20v},{wupp},{stop_loss}\n")
        print("\n结果已导出。")

    print("\n===== 运行回测 =====")
    perf, bench_ret, nv = run_backtest(conn, df_daily, df_fin, name_map, industry_map, BACKTEST_START, BACKTEST_END)
    conn.close()
    print("\n" + "="*50)
    print("回测绩效报告")
    print("="*50)
    print(f"初始资金:    {INIT_CASH:>12.2f}")
    print(f"最终资金:    {perf['final_value']:>12.2f}")
    print(f"总收益率:    {perf['total_return']:>11.2f}%")
    print(f"年化收益率:  {perf['annual_return']:>11.2f}%")
    print(f"夏普比率:    {perf['sharpe']:>12.2f}")
    print(f"最大回撤:    {perf['max_drawdown']:>11.2f}%")
    print(f"卡玛比率:    {perf['calmar']:>12.2f}")
    print(f"交易胜率:    {perf['win_rate']:>11.2f}%")
    if bench_ret is not None:
        print(f"沪深300收益: {bench_ret.iloc[-1]:>11.2f}%")
        print(f"超额收益:    {perf['total_return'] - bench_ret.iloc[-1]:>11.2f}%")
    print("="*50)
    try:
        import matplotlib.pyplot as plt
        plt.rcParams['font.sans-serif'] = ['Arial Unicode MS', 'PingFang HK', 'Heiti TC']
        plt.rcParams['axes.unicode_minus'] = False
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12,8))
        ax1.plot(nv.index, nv.values, label='策略净值', color='blue')
        if bench_ret is not None:
            ax1.plot(bench_ret.index, (bench_ret/100+1)*INIT_CASH, label='沪深300', alpha=0.6)
        ax1.legend(); ax1.grid(); ax1.set_title('策略净值 vs 沪深300')
        dd = (nv / nv.cummax() - 1) * 100
        ax2.fill_between(dd.index, dd.values, 0, color='red', alpha=0.3)
        ax2.grid(); ax2.set_title('回撤 %')
        plt.tight_layout(); plt.show()
    except Exception as e:
        print(f"绘图失败: {e}")
    print("\n选股回测完成。")