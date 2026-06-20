#!/usr/bin/env python3
"""
选股 + 回测 + 导出（核心共振 + 弹性降级，最终放宽版）
================================
第一层「核心共振」：
    年线趋势 + 流动性 + 月线回踩(≥1次) + 月线布林扩张 + 周线二次回踩(≥2次) + 周线布林扩张 + 财务达标
第二层「弹性降级」：
    硬条件：年线趋势 + 流动性 + (月线回踩≥1次 或 周线二次回踩) + 财务达标
    软条件（至少满足一项）：
        A. 月线布林扩张
        B. 周线二次回踩
        C. 周线二次回踩 + 周线布林扩张

重要调整（与前一版区别）：
    - 财务条件放宽：扣非净利润若为NULL则跳过检查（不再强制必须存在）
    - 月线布林扩张放宽：中轨方向不做强制要求（仅要求股价在中轨上方）
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

# -------------------- 流动性 --------------------
MIN_20D_AMOUNT = 2000          # 20日均成交额 ≥ 2000万元
MIN_120D_AMOUNT = 1500         # 120日均成交额 ≥ 1500万元

# -------------------- 年度趋势（滚动+自然年） --------------------
ROLLING_DAYS = 250             # 滚动周期，近似12个月
ROLLING_PRICE_UP = 0.15        # 滚动涨幅 ≥ 15%
ROLLING_VOL_UP = 0.30          # 滚动日均成交额增长 ≥ 30%
MA_SLOPE_THRESHOLD = 0         # 年线斜率 ≥ 0（走平或向上）

# -------------------- 月线回踩（至少1次） --------------------
MONTHLY_RETEST_DOWN = 0.12     # 下探幅度 ≤ 12%
MONTHLY_RETEST_NEAR = 0.08     # 靠近幅度 ≤ 8%
MONTHLY_RETEST_WINDOW = 18     # 观察窗口（根月K线）
MONTHLY_RETEST_MIN_GAP = 3     # 两次回踩最小间隔（根）
MONTHLY_RETEST_MIN_TOUCHES = 1 # 最少回踩次数

# -------------------- 周线回踩（至少2次，即二次回踩） --------------------
WEEKLY_RETEST_DOWN = 0.12
WEEKLY_RETEST_NEAR = 0.08
WEEKLY_RETEST_WINDOW = 50
WEEKLY_RETEST_MIN_GAP = 5
WEEKLY_RETEST_MIN_TOUCHES = 2  # ★ 二次回踩

# -------------------- 布林扩张（最终优化版） --------------------
BB_PERIOD = 20
BB_STD_MULT = 2
BB_SHORT_MA = 5                # 带宽短期均线周期
BB_LONG_MA = 20                # 带宽长期均线周期
BB_SHORT_DIR_PERIOD = 2        # 带宽短期方向确认（连续2期上升）
BB_MID_DIR_PERIOD = 3          # 中轨方向计算周期（本版已弱化）
# 月线布林：不要求中轨方向，仅需股价在中轨上方
MONTHLY_BB_REQUIRE_MID_UP = False   # ★ 改为 False
# 周线布林：仍保持中轨走平或向上（可取消，目前设为 True）
WEEKLY_BB_REQUIRE_MID_UP = True
WEEKLY_BB_OVERBOUGHT = None    # 超买限制已取消

# -------------------- 均线方向判断（回踩用） --------------------
MA_DIR_PERIOD = 3              # 当期均线值 > 前3期均值

# -------------------- 财务（放宽版） --------------------
FIN_CONSEC = 2                 # 连续季度数
MIN_PROFIT_YOY = 0.0           # 归母净利润同比 > 0%
MIN_PNI_YOY = 0.0              # 扣非净利润同比 > 0%（若存在，否则不检查）

# -------------------- 交易费率 --------------------
COMMISSION = 0.0001            # 佣金（万1）
SLIPPAGE = 0.001               # 滑点
STAMP_DUTY = 0.001             # 卖出印花税
REBALANCE_FREQ = 'W'           # 调仓频率（W=周）


# ===================== 数据加载 =====================
def load_all_data(conn):
    """加载2018年至今的日线数据，返回 multi-index (code, date) 的 DataFrame"""
    query = """SELECT code, date, open, high, low, close, volume, amount
               FROM daily WHERE date >= '2018-01-01' ORDER BY code, date"""
    df = pd.read_sql(query, conn, parse_dates=['date'])
    return df.set_index(['code', 'date']).sort_index()


def load_financial_data(conn):
    """加载财务数据，计算生效日期（pub_date + 10天）"""
    query = """SELECT code, stat_date, pub_date, net_profit_yoy, revenue_yoy, yoy_pni
               FROM financial WHERE net_profit_yoy IS NOT NULL ORDER BY code, stat_date"""
    df = pd.read_sql(query, conn, parse_dates=['stat_date', 'pub_date'])
    df['effective_date'] = df['pub_date'].fillna(df['stat_date'] + timedelta(days=30)) + timedelta(days=10)
    return df


# ===================== 前置剔除 =====================
def get_valid_codes(conn, df_daily, target_date):
    """
    返回符合前置剔除条件的股票代码列表：
    - 上市 ≥ 24 个月
    - 近 20 个交易日停牌天数 ≤ 2 天
    """
    basic = pd.read_sql("SELECT code, list_date FROM stock_basic", conn, parse_dates=['list_date'])
    basic['months'] = ((target_date - basic['list_date']).dt.days / 30.44)
    valid_listed = basic[basic['months'] >= 24]['code'].tolist()

    df_recent = df_daily.loc[df_daily.index.get_level_values('date') >= target_date - timedelta(days=40)]
    trading_days = df_recent.groupby(level='code').size()
    valid_trading = trading_days[trading_days >= 18].index.tolist()

    return list(set(valid_listed) & set(valid_trading))


# ===================== 年度趋势 =====================
def check_annual_trend(code, df_stocks, target_date, yearly_data):
    """
    检查年线趋势（滚动或自然年任一满足即可）
    - 滚动：最近250交易日涨幅≥15%且日均成交额增长≥30%，收盘价≥250日均线，斜率≥0
    - 自然年：上一个完整自然年收红且放量
    """
    code_data = df_stocks.loc[code].sort_index()
    rolling_ok = False

    if len(code_data) >= ROLLING_DAYS * 2:
        recent = code_data.iloc[-ROLLING_DAYS:]
        prev = code_data.iloc[-2*ROLLING_DAYS:-ROLLING_DAYS]
        price_up = (recent['close'].iloc[-1] - prev['close'].iloc[-1]) / prev['close'].iloc[-1] >= ROLLING_PRICE_UP
        vol_ratio = recent['amount'].mean() / prev['amount'].mean() - 1
        vol_up = vol_ratio >= ROLLING_VOL_UP
        ma250 = code_data['close'].rolling(250).mean()
        above_ma = code_data['close'].iloc[-1] >= ma250.iloc[-1]
        slope = (ma250.iloc[-1] - ma250.iloc[-20]) / 20 if len(ma250) >= 20 else -1
        slope_ok = slope >= MA_SLOPE_THRESHOLD
        rolling_ok = price_up and vol_up and above_ma and slope_ok

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


# ===================== 回踩检测 =====================
def detect_retest_with_gap(price_df, ma_period, tolerance_down, tolerance_near,
                           window, min_gap, min_touches=1, require_ma_up=True):
    """
    均线回踩信号检测（支持最少回踩次数和间隔计数）
    参数：
        min_touches: 最少回踩次数（月线1次，周线2次）
        require_ma_up: 是否要求均线方向向上
    返回布尔Series
    """
    close = price_df['close']
    low = price_df['low']
    ma = close.rolling(ma_period).mean()

    if require_ma_up:
        ma_up = ma > ma.shift(1).rolling(MA_DIR_PERIOD).mean()
    else:
        ma_up = pd.Series(True, index=ma.index)

    touch_down = (low < ma) & ((ma - low) / ma <= tolerance_down)
    touch_near = (low >= ma) & ((low - ma) / ma <= tolerance_near)
    touch = (touch_down | touch_near) & ma_up

    touch_int = touch.astype(int)
    event_start = (touch_int.diff() == 1) | (touch_int == 1)
    event_count = event_start.rolling(window, min_periods=1).sum()
    has_event = event_count >= min_touches

    close_ok = close >= ma
    return has_event & close_ok


# ===================== 布林扩张检测（支持中轨方向可选） =====================
def detect_bb_expand(price_df, period=20, std_mult=2, short_ma=5, long_ma=20,
                     require_mid_up=True, mid_dir_period=3, short_dir_period=2,
                     overbought_limit=None):
    """
    布林带扩张检测
    参数：
        require_mid_up: 是否要求中轨方向向上（或走平）。True=要求，False=仅要求股价在中轨上方
    """
    close = price_df['close']
    mid = close.rolling(period).mean()
    std = close.rolling(period).std()
    upper = mid + std_mult * std
    lower = mid - std_mult * std
    bandwidth = (upper - lower) / mid
    bw_short = bandwidth.rolling(short_ma).mean()
    bw_long = bandwidth.rolling(long_ma).mean()

    # 股价在中轨上方（基本要求）
    above_mid = close > mid

    if require_mid_up:
        # 中轨方向：当期 ≥ 前 mid_dir_period 期均值（允许走平）
        mid_up = mid >= mid.shift(1).rolling(mid_dir_period).mean()
        cond = above_mid & mid_up
    else:
        cond = above_mid

    # 带宽扩张
    expanding = (bw_short > bw_long) & (bw_short.diff(short_dir_period) > 0)
    cond = cond & expanding

    if overbought_limit is not None:
        cond = cond & (close <= upper * overbought_limit)
    return cond


# ===================== 选股主逻辑（两层体系） =====================
def vectorized_select_stocks(conn, df_daily, df_fin, name_map, industry_map, target_date=None):
    if target_date is None:
        target_date = df_daily.index.get_level_values('date').max()
    target_date = pd.Timestamp(target_date)

    # ---------- 前置剔除 + 年线流动性 ----------
    valid_codes = get_valid_codes(conn, df_daily, target_date)
    df_stocks = df_daily[df_daily.index.get_level_values('code').isin(valid_codes)].copy()
    if df_stocks.empty:
        print("前置剔除后无股票")
        return [], [], []

    grouped = df_stocks.groupby(level='code')
    df_stocks['amount_wan'] = df_stocks['amount'] / 10000
    df_stocks['amount_ma20'] = grouped['amount_wan'].transform(lambda x: x.rolling(20).mean())
    df_stocks['amount_ma120'] = grouped['amount_wan'].transform(lambda x: x.rolling(120).mean())
    df_stocks['year'] = df_stocks.index.get_level_values('date').year
    yearly = df_stocks.groupby(['code', 'year']).agg(
        first_open=('open', 'first'), last_close=('close', 'last'), total_volume=('volume', 'sum')
    ).sort_index()

    annual_ok = {code: check_annual_trend(code, df_stocks, target_date, yearly) for code in valid_codes}
    latest = df_stocks.groupby(level='code').tail(1)
    latest = latest[~latest.index.duplicated(keep='last')]
    base_codes = []
    for code in latest.index.get_level_values('code'):
        row = latest.loc[code]
        if isinstance(row, pd.DataFrame):
            row = row.iloc[0]
        if (annual_ok.get(code, False) and
                row['amount_ma20'] >= MIN_20D_AMOUNT and
                row['amount_ma120'] >= MIN_120D_AMOUNT):
            base_codes.append(code)
    print(f"年线+流动性通过：{len(base_codes)} 只")
    if not base_codes:
        return [], [], []

    # ---------- 技术信号计算 ----------
    df_f = df_stocks[df_stocks.index.get_level_values('code').isin(base_codes)]
    daily_close = df_f['close'].unstack(level='code')
    daily_low = df_f['low'].unstack(level='code')

    w_close = daily_close.resample('W').last()
    w_low = daily_low.resample('W').min()
    w_df = pd.DataFrame({'close': w_close.stack(), 'low': w_low.stack()})

    m_close = daily_close.resample('ME').last()
    m_low = daily_low.resample('ME').min()
    m_df = pd.DataFrame({'close': m_close.stack(), 'low': m_low.stack()})

    # 月线回踩（至少1次）
    m_retest = detect_retest_with_gap(m_df, 20, MONTHLY_RETEST_DOWN, MONTHLY_RETEST_NEAR,
                                      MONTHLY_RETEST_WINDOW, MONTHLY_RETEST_MIN_GAP,
                                      MONTHLY_RETEST_MIN_TOUCHES, True)

    # 月线布林扩张（放宽：不要求中轨方向）
    m_bb = detect_bb_expand(m_df, BB_PERIOD, BB_STD_MULT, BB_SHORT_MA, BB_LONG_MA,
                            require_mid_up=MONTHLY_BB_REQUIRE_MID_UP,
                            short_dir_period=BB_SHORT_DIR_PERIOD,
                            overbought_limit=None)

    # 周线回踩（二次回踩）
    w_retest = detect_retest_with_gap(w_df, 20, WEEKLY_RETEST_DOWN, WEEKLY_RETEST_NEAR,
                                      WEEKLY_RETEST_WINDOW, WEEKLY_RETEST_MIN_GAP,
                                      WEEKLY_RETEST_MIN_TOUCHES, True)

    # 周线布林扩张（保持中轨方向要求）
    w_bb = detect_bb_expand(w_df, BB_PERIOD, BB_STD_MULT, BB_SHORT_MA, BB_LONG_MA,
                            require_mid_up=WEEKLY_BB_REQUIRE_MID_UP,
                            short_dir_period=BB_SHORT_DIR_PERIOD,
                            overbought_limit=WEEKLY_BB_OVERBOUGHT)

    def extract_codes(signal_series):
        """从多级索引Series中提取唯一代码列表"""
        return signal_series.groupby(level='code').tail(1).pipe(
            lambda x: x[x].index.get_level_values('code').unique().tolist()
        )

    m_retest_codes = extract_codes(m_retest)
    m_bb_codes = extract_codes(m_bb)
    w_retest_codes = extract_codes(w_retest)
    w_bb_codes = extract_codes(w_bb)

    print(f"月线回踩: {len(m_retest_codes)} 只，月线布林扩张: {len(m_bb_codes)} 只")
    print(f"周线回踩(二次): {len(w_retest_codes)} 只，周线布林扩张: {len(w_bb_codes)} 只")

    # ---------- 财务筛选（放宽版） ----------
    fin_before = df_fin[df_fin['effective_date'] <= target_date]
    if fin_before.empty:
        return [], [], []
    fin_latest = fin_before.sort_values('effective_date').groupby('code').tail(FIN_CONSEC)
    # 放宽条件：净利润>0，扣非净利润若存在则>0，若为NULL则忽略
    fin_pass = fin_latest.groupby('code').filter(
        lambda x: (len(x) == FIN_CONSEC) and all(x['net_profit_yoy'] > MIN_PROFIT_YOY) and
                  all((x['yoy_pni'].isna()) | (x['yoy_pni'] > MIN_PNI_YOY))
    )
    fin_codes = fin_pass['code'].unique().tolist()
    print(f"财务符合（扣非缺失也放过）：{len(fin_codes)} 只")

    # ---------- 两层分类 ----------
    # 第一层：全共振
    core_set = set(base_codes) & set(m_retest_codes) & set(m_bb_codes) & set(w_retest_codes) & set(w_bb_codes) & set(fin_codes)

    # 第二层硬条件池：年线 + (月线回踩 或 周线二次回踩) + 财务
    hard_base = set(base_codes) & (set(m_retest_codes) | set(w_retest_codes)) & set(fin_codes)

    tier1, tier2 = [], []
    assigned = set()

    # 第一层
    for code in sorted(core_set):
        assigned.add(code)
        tier1.append((code, '全信号共振'))

    # 第二层：软条件检查
    for code in sorted(hard_base - assigned):
        soft_a = code in m_bb_codes
        soft_b = code in w_retest_codes
        soft_c = soft_b and code in w_bb_codes
        parts = []
        if soft_a:
            parts.append('月布林')
        if soft_b:
            parts.append('周二次回踩')
        if soft_c:
            parts.append('周布林')
        if parts:
            desc = '弹性降级：' + '+'.join(parts)
            tier2.append((code, desc))

    final_selected = tier1 + tier2
    final = [(c, name_map.get(c, c), industry_map.get(c, ''), d) for c, d in final_selected[:MAX_STOCKS]]
    print(f"第一层核心共振: {len(tier1)} 只，第二层弹性降级: {len(tier2)} 只，合计: {len(final)} 只")
    return final, tier1, tier2


# ===================== 回测引擎 =====================
def run_backtest(conn, df_daily, df_fin, name_map, industry_map, start, end):
    """执行周度调仓回测，返回绩效指标和净值序列"""
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
        sel, _, _ = vectorized_select_stocks(conn, df_daily, df_fin, name_map, industry_map, target_date=d)
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
    """计算年化收益、夏普比率、最大回撤、卡玛比率、胜率等"""
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
        'max_drawdown': maxdd, 'calmar': calmar, 'win_rate': win, 'final_value': nv.iloc[-1]
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
    selected, tier1, tier2 = vectorized_select_stocks(conn, df_daily, df_fin, name_map, industry_map)
    if not selected:
        print("未选出股票")
    else:
        df_f = df_daily[df_daily.index.get_level_values('code').isin([s[0] for s in selected])]
        close = df_f['close'].unstack(level='code')
        weekly_close = close.resample('W').last()
        monthly_close = close.resample('ME').last()
        w20 = weekly_close.rolling(20).mean().iloc[-1] if len(weekly_close) >= 20 else None
        m20 = monthly_close.rolling(20).mean().iloc[-1] if len(monthly_close) >= 20 else None
        w_std = weekly_close.rolling(20).std().iloc[-1]
        w_mid = weekly_close.rolling(20).mean().iloc[-1]
        w_upper = w_mid + 2 * w_std

        print("\n最终选股池（按层级排序）：")
        for c, n, ind, s in selected:
            print(f"{c} {n} [{ind}] {s}")

        with open('selected_stocks.txt', 'w') as f:
            for c, n, _, _ in selected:
                f.write(f"{c.replace('sh.','').replace('sz.','')},{n}\n")
        with open('selected_stocks_detail.txt', 'w') as f:
            f.write("代码,名称,行业,层级/信号,20周线,20月线,布林上轨(周),止损参考(10%)\n")
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