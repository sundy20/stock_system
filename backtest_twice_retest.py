#!/usr/bin/env python3
"""
选股 + 回测 + 导出（基于公共策略模块 stock_strategy）
所有选股逻辑已抽离至 stock_strategy.py，本脚本仅保留回测引擎和主流程。
修改参数请编辑 stock_strategy.py 顶部的变量区。
"""

import sqlite3, pandas as pd, numpy as np
from datetime import datetime, timedelta
import stock_strategy as st                           # 导入公共策略模块

DB_PATH = st.DB_PATH                                  # 从模块获取数据库路径
BENCH_CODE = st.BENCH_CODE                            # 基准指数
BACKTEST_START = (datetime.now() - timedelta(days=365*2)).strftime('%Y-%m-%d')
BACKTEST_END   = datetime.now().strftime('%Y-%m-%d')
INIT_CASH      = 1_000_000                            # 初始资金
MAX_STOCKS     = 200                                  # 最大持仓数


def vectorized_select_stocks(conn, df_daily, df_fin, name_map, industry_map, target_date=None):
    """
    核心选股函数，应用两层策略（核心共振 + 弹性降级）
    返回 (最终列表, 第一层列表, 第二层列表)
    """
    if target_date is None:
        target_date = df_daily.index.get_level_values('date').max()
    target_date = pd.Timestamp(target_date)

    # ★ 防止前视偏差：只保留 target_date 及之前的数据
    df_daily = df_daily[df_daily.index.get_level_values('date') <= target_date]

    # ---------- 前置剔除 ----------
    valid_codes = st.get_valid_codes(conn, df_daily, target_date)
    df_stocks = df_daily[df_daily.index.get_level_values('code').isin(valid_codes)].copy()
    if df_stocks.empty:
        print("前置剔除后无股票")
        return [], [], []

    # ---------- 基础指标计算 ----------
    grouped = df_stocks.groupby(level='code')
    df_stocks['amount_wan']   = df_stocks['amount'] / 10000
    df_stocks['amount_ma20']  = grouped['amount_wan'].transform(lambda x: x.rolling(20).mean())
    df_stocks['amount_ma120'] = grouped['amount_wan'].transform(lambda x: x.rolling(120).mean())
    df_stocks['year'] = df_stocks.index.get_level_values('date').year
    yearly = df_stocks.groupby(['code', 'year']).agg(
        first_open=('open', 'first'), last_close=('close', 'last'), total_volume=('volume', 'sum')
    ).sort_index()

    # 年线趋势检查
    annual_ok = {code: st.check_annual_trend(code, df_stocks, target_date, yearly) for code in valid_codes}
    latest = df_stocks.groupby(level='code').tail(1)
    latest = latest[~latest.index.duplicated(keep='last')]
    base_codes = []
    for code in latest.index.get_level_values('code'):
        row = latest.loc[code]
        if isinstance(row, pd.DataFrame):
            row = row.iloc[0]
        if (annual_ok.get(code, False) and
                row['amount_ma20'] >= st.MIN_20D_AMOUNT and
                row['amount_ma120'] >= st.MIN_120D_AMOUNT):
            base_codes.append(code)
    print(f"年线+流动性通过：{len(base_codes)} 只")
    if not base_codes:
        return [], [], []

    # ---------- 技术信号计算 ----------
    df_f = df_stocks[df_stocks.index.get_level_values('code').isin(base_codes)]
    daily_close = df_f['close'].unstack(level='code')
    daily_low   = df_f['low'].unstack(level='code')

    # 构建周线、月线价格DataFrame
    w_close = daily_close.resample('W').last()
    w_low   = daily_low.resample('W').min()
    w_df = pd.DataFrame({'close': w_close.stack(), 'low': w_low.stack()})
    m_close = daily_close.resample('ME').last()
    m_low   = daily_low.resample('ME').min()
    m_df = pd.DataFrame({'close': m_close.stack(), 'low': m_low.stack()})

    # 月线信号
    m_retest = st.detect_retest_with_gap(m_df, 20, st.MONTHLY_RETEST_DOWN, st.MONTHLY_RETEST_NEAR,
                                         st.MONTHLY_RETEST_WINDOW, st.MONTHLY_RETEST_MIN_GAP,
                                         st.MONTHLY_RETEST_MIN_TOUCHES, True)
    m_bb = st.detect_bb_expand(m_df, st.BB_PERIOD, st.BB_STD_MULT, st.BB_SHORT_MA, st.BB_LONG_MA,
                               require_mid_up=st.MONTHLY_BB_REQUIRE_MID_UP,
                               short_dir_period=st.BB_SHORT_DIR_PERIOD, overbought_limit=None)

    # 周线信号（二次回踩 + 双模式布林）
    w_retest = st.detect_retest_with_gap(w_df, 20, st.WEEKLY_RETEST_DOWN, st.WEEKLY_RETEST_NEAR,
                                         st.WEEKLY_RETEST_WINDOW, st.WEEKLY_RETEST_MIN_GAP,
                                         st.WEEKLY_RETEST_MIN_TOUCHES, True)
    w_bb = st.detect_bb_expand(w_df, st.BB_PERIOD, st.BB_STD_MULT, st.BB_SHORT_MA, st.BB_LONG_MA,
                               require_mid_up=st.WEEKLY_BB_REQUIRE_MID_UP,
                               short_dir_period=st.BB_SHORT_DIR_PERIOD,
                               overbought_limit=None,            # 超买限制已取消
                               pre_expand=st.WEEKLY_BB_PRE_EXPAND,
                               contraction_ratio=st.WEEKLY_BB_CONTRACTION_RATIO,
                               use_dual_mode=st.WEEKLY_BB_USE_DUAL_MODE,
                               price_limit=st.WEEKLY_BB_PRICE_LIMIT)

    # 提取信号代码
    def extract_codes(signal_series):
        return signal_series.groupby(level='code').tail(1).pipe(
            lambda x: x[x].index.get_level_values('code').unique().tolist()
        )

    m_retest_codes = extract_codes(m_retest)
    m_bb_codes     = extract_codes(m_bb)
    w_retest_codes = extract_codes(w_retest)
    w_bb_codes     = extract_codes(w_bb)

    print(f"月线回踩: {len(m_retest_codes)} 只，月线布林扩张: {len(m_bb_codes)} 只")
    print(f"周线回踩(二次): {len(w_retest_codes)} 只，周线布林扩张(双模式): {len(w_bb_codes)} 只")

    # ---------- 财务筛选（根据开关决定） ----------
    fin_codes = st.apply_financial_filter(base_codes, df_fin, target_date)
    if st.USE_FINANCIAL_FILTER:
        print(f"财务符合：{len(fin_codes)} 只")
    else:
        print("财务条件已关闭，全部通过")

    # ---------- 两层交集计算 ----------
    # 第一层：核心共振
    core_set = (set(base_codes) & set(m_retest_codes) & set(m_bb_codes) &
                set(w_retest_codes) & set(w_bb_codes) & set(fin_codes))
    # 第二层硬条件池
    hard_base = set(base_codes) & (set(m_retest_codes) | set(w_retest_codes)) & set(fin_codes)

    tier1, tier2 = [], []
    assigned = set()

    # 第一层
    for code in sorted(core_set):
        assigned.add(code)
        tier1.append((code, '全信号共振'))

    # 第二层
    for code in sorted(hard_base - assigned):
        soft_a = code in m_bb_codes
        soft_b = code in w_retest_codes
        soft_c = soft_b and code in w_bb_codes
        parts = []
        if soft_a: parts.append('月布林')
        if soft_b: parts.append('周二次回踩')
        if soft_c: parts.append('周布林')
        if parts:
            desc = '弹性降级：' + '+'.join(parts)
            tier2.append((code, desc))

    final_selected = tier1 + tier2
    final = [(c, name_map.get(c, c), industry_map.get(c, ''), d) for c, d in final_selected[:MAX_STOCKS]]
    print(f"第一层核心共振: {len(tier1)} 只，第二层弹性降级: {len(tier2)} 只，合计: {len(final)} 只")
    return final, tier1, tier2


def run_backtest(conn, df_daily, df_fin, name_map, industry_map, start, end):
    """执行周度调仓回测，返回绩效和净值序列"""
    close = df_daily['close'].unstack(level='code').loc[start:end].dropna(axis=1, how='all')
    bench = close[BENCH_CODE] if BENCH_CODE in close.columns else None
    stock_close = close[[c for c in close.columns if c != BENCH_CODE]]
    freq = 'W' if st.REBALANCE_FREQ == 'W' else 'ME'
    periods = stock_close.index.to_period(freq)
    rebalance_dates = stock_close.groupby(periods).apply(lambda x: x.index[-1]).values

    pct_chg_df = df_daily['pct_chg'].unstack(level='code')

    cash = INIT_CASH
    holdings = {}
    net_vals = []
    last_date = None

    for d_raw in rebalance_dates:
        d = pd.Timestamp(d_raw)
        sel, _, _ = vectorized_select_stocks(conn, df_daily, df_fin, name_map, industry_map, target_date=d)
        targets = [s[0] for s in sel if s[0] in stock_close.columns]

        # 涨跌停检测
        today_pct = pct_chg_df.loc[d] if d in pct_chg_df.index else pd.Series(dtype=float)
        limit_up_codes   = set(today_pct[today_pct >= 9.8].index)
        limit_down_codes = set(today_pct[today_pct <= -9.8].index)

        # 记录每日净值
        if last_date is not None:
            mask = (stock_close.index > last_date) & (stock_close.index < d)
            for day in stock_close.index[mask]:
                val = cash + sum(holdings.get(c, 0) * stock_close.loc[day, c] for c in holdings if c in stock_close.columns)
                net_vals.append((day, val))

        # 卖出全部
        for c in list(holdings.keys()):
            if c in stock_close.columns:
                sell_cost = max(holdings[c] * stock_close.loc[d, c] * (st.COMMISSION + st.STAMP_DUTY), 5.0)
                cash += holdings[c] * stock_close.loc[d, c] - sell_cost
        holdings.clear()

        # 等权买入（排除涨跌停）
        if targets:
            buy_targets = [c for c in targets if c not in limit_up_codes and c not in limit_down_codes]
            if buy_targets:
                prices = stock_close.loc[d, buy_targets]
                avg_cash = cash / len(prices)
                for c in buy_targets:
                    price = prices[c]
                    buy_cost = max(price * st.COMMISSION, 5.0)
                    shares = int(avg_cash / (price + buy_cost + price * st.SLIPPAGE)) // 100 * 100
                    if shares > 0:
                        cash -= shares * (price + buy_cost / shares + price * st.SLIPPAGE)
                        holdings[c] = shares

        val = cash + sum(holdings.get(c, 0) * stock_close.loc[d, c] for c in holdings if c in stock_close.columns)
        net_vals.append((d, val))
        last_date = d

    # 补全最后一段净值
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
    """计算年化收益率、夏普比率、最大回撤等绩效指标"""
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
    df_daily = st.load_all_data(conn)                  # 使用模块中的数据加载函数
    df_fin   = st.load_financial_data(conn)
    basic = pd.read_sql("SELECT code, name, industry FROM stock_basic", conn)
    name_map = basic.set_index('code')['name'].to_dict()
    industry_map = basic.set_index('code')['industry'].to_dict()

    print("\n===== 最新选股 =====")
    selected, tier1, tier2 = vectorized_select_stocks(conn, df_daily, df_fin, name_map, industry_map)
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

        print("\n最终选股池（按层级排序）：")
        for c, n, ind, s in selected:
            print(f"{c} {n} [{ind}] {s}")

        # 导出结果
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

    # 绘制净值曲线
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