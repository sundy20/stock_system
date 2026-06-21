#!/usr/bin/env python3
"""
选股 + 回测 + 导出（基于公共策略模块 stock_strategy）
所有选股逻辑已抽离至 stock_strategy.py，本脚本仅保留回测引擎和主流程。
修改参数请编辑 stock_strategy.py 顶部的变量区。
"""

import sqlite3
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import stock_strategy as st
import os

DB_PATH = st.DB_PATH
BENCH_CODE = st.BENCH_CODE
BACKTEST_START = (datetime.now() - timedelta(days=365*2)).strftime('%Y-%m-%d')
BACKTEST_END   = datetime.now().strftime('%Y-%m-%d')
INIT_CASH      = 1_000_000
MAX_STOCKS     = 200


def select_stocks_at_date(signal_cache, yearly, df_daily, df_fin, conn, name_map, industry_map, target_date):
    """
    使用预计算缓存，在 target_date 选取股票（两层共振机制）。
    无信号重算，只做缓存切片 + 筛选，耗时从分钟级降至毫秒级。
    """
    target_ts = pd.Timestamp(target_date)

    # 1. 前置剔除（上市≥24月 + 近期停牌不多）
    valid_codes = st.get_valid_codes(conn, df_daily, target_ts)

    # 2. 年线趋势 + 流动性
    base_codes = []
    for code in valid_codes:
        entry = signal_cache.get(code)
        if entry is None:
            continue
        if not st.check_annual_trend_fast(code, entry, yearly, target_ts):
            continue
        ma20 = st.get_latest_value(entry['amount_ma20'], target_ts)
        ma120 = st.get_latest_value(entry['amount_ma120'], target_ts)
        if ma20 is None or pd.isna(ma20) or ma20 < st.MIN_20D_AMOUNT:
            continue
        if ma120 is None or pd.isna(ma120) or ma120 < st.MIN_120D_AMOUNT:
            continue
        base_codes.append(code)

    print(f"年线+流动性通过：{len(base_codes)} 只")
    if not base_codes:
        return [], [], []

    # 3. 信号提取（从预计算 Series 切片）
    m_retest_codes, m_bb_codes, w_retest_codes, w_bb_codes = [], [], [], []
    for code in base_codes:
        entry = signal_cache[code]
        if st.get_latest_value(entry['m_retest'], target_ts): m_retest_codes.append(code)
        if st.get_latest_value(entry['m_bb'], target_ts):     m_bb_codes.append(code)
        if st.get_latest_value(entry['w_retest'], target_ts): w_retest_codes.append(code)
        if st.get_latest_value(entry['w_bb'], target_ts):     w_bb_codes.append(code)

    print(f"月线回踩: {len(m_retest_codes)} 只，月线布林: {len(m_bb_codes)} 只")
    print(f"周线回踩: {len(w_retest_codes)} 只，周线布林: {len(w_bb_codes)} 只")

    # 4. 财务筛选
    fin_codes = st.apply_financial_filter(base_codes, df_fin, target_ts)
    if st.USE_FINANCIAL_FILTER:
        print(f"财务符合：{len(fin_codes)} 只")
    else:
        print("财务条件已关闭，全部通过")

    # 5. 两层交集（与原逻辑完全一致）
    core_set = (set(base_codes) & set(m_retest_codes) & set(m_bb_codes) &
                set(w_retest_codes) & set(w_bb_codes) & set(fin_codes))
    hard_base = set(base_codes) & (set(m_retest_codes) | set(w_retest_codes)) & set(fin_codes)

    tier1, tier2 = [], []
    assigned = set()

    for code in sorted(core_set):
        assigned.add(code)
        tier1.append((code, '全信号共振'))

    for code in sorted(hard_base - assigned):
        soft_a = code in m_bb_codes
        soft_b = code in w_retest_codes
        soft_c = soft_b and code in w_bb_codes
        parts = []
        if soft_a: parts.append('月布林')
        if soft_b: parts.append('周二次回踩')
        if soft_c: parts.append('周布林')
        if parts:
            tier2.append((code, '弹性降级：' + '+'.join(parts)))

    final_selected = tier1 + tier2
    final = [(c, name_map.get(c, c), industry_map.get(c, ''), d) for c, d in final_selected[:MAX_STOCKS]]
    print(f"核心共振: {len(tier1)} 只，弹性降级: {len(tier2)} 只，合计: {len(final)} 只")
    return final, tier1, tier2


def run_backtest_optimized(signal_cache, yearly, conn, df_daily, df_fin, name_map, industry_map, start, end):
    """执行周度调仓回测（预计算信号 + 向量化净值）"""
    close = df_daily['close'].unstack(level='code').loc[start:end].dropna(axis=1, how='all')
    bench = close[BENCH_CODE] if BENCH_CODE in close.columns else None
    stock_close = close[[c for c in close.columns if c != BENCH_CODE]].ffill()

    freq = 'W' if st.REBALANCE_FREQ == 'W' else 'ME'
    periods = stock_close.index.to_period(freq)
    rebalance_dates = stock_close.groupby(periods).apply(lambda x: x.index[-1]).values

    pct_chg_df = df_daily['pct_chg'].unstack(level='code')

    cash = INIT_CASH
    holdings = {}
    net_vals = []
    last_date = None

    print(f"\n开始回测，共 {len(rebalance_dates)} 个调仓日")

    for idx, d_raw in enumerate(rebalance_dates):
        d = pd.Timestamp(d_raw)
        if (idx + 1) % 10 == 0 or idx == len(rebalance_dates) - 1:
            print(f"  回测进度: {idx+1}/{len(rebalance_dates)}")

        # 选股（缓存切片，无重算）
        sel, _, _ = select_stocks_at_date(
            signal_cache, yearly, df_daily, df_fin, conn, name_map, industry_map, d)
        targets = [s[0] for s in sel if s[0] in stock_close.columns]

        # 涨跌停检测
        today_pct = pct_chg_df.loc[d] if d in pct_chg_df.index else pd.Series(dtype=float)
        limit_up_codes   = set(today_pct[today_pct >= 9.8].index)
        limit_down_codes = set(today_pct[today_pct <= -9.8].index)

        # 调仓间每日净值（向量化）
        if last_date is not None:
            inter_mask = (stock_close.index > last_date) & (stock_close.index < d)
            inter_days = stock_close.index[inter_mask]
            if len(inter_days) > 0 and holdings:
                h = pd.Series(0.0, index=stock_close.columns)
                for c, s in holdings.items():
                    if c in h.index:
                        h[c] = float(s)
                portfolio = (h * stock_close.loc[inter_days]).sum(axis=1) + cash
                net_vals.extend(zip(inter_days, portfolio.values))
            elif len(inter_days) > 0:
                net_vals.extend((day, cash) for day in inter_days)

        # 卖出全部
        for c in list(holdings.keys()):
            if c in stock_close.columns:
                sell_price = stock_close.loc[d, c]
                sell_cost = max(holdings[c] * sell_price * (st.COMMISSION + st.STAMP_DUTY), 5.0)
                cash += holdings[c] * sell_price - sell_cost
        holdings.clear()

        # 等权买入
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

        # 调仓日净值
        if holdings:
            h = pd.Series(0.0, index=stock_close.columns)
            for c, s in holdings.items():
                if c in h.index:
                    h[c] = float(s)
            val = float((h * stock_close.loc[d]).sum() + cash)
        else:
            val = cash
        net_vals.append((d, val))
        last_date = d

    # 补全末段净值
    if last_date is not None and last_date < stock_close.index[-1]:
        final_mask = stock_close.index > last_date
        final_days = stock_close.index[final_mask]
        if len(final_days) > 0 and holdings:
            h = pd.Series(0.0, index=stock_close.columns)
            for c, s in holdings.items():
                if c in h.index:
                    h[c] = float(s)
            portfolio = (h * stock_close.loc[final_days]).sum(axis=1) + cash
            net_vals.extend(zip(final_days, portfolio.values))
        elif len(final_days) > 0:
            net_vals.extend((day, cash) for day in final_days)

    # 构建净值序列
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
    df_daily = st.load_all_data(conn)
    df_fin   = st.load_financial_data(conn)
    basic = pd.read_sql("SELECT code, name, industry FROM stock_basic", conn)
    name_map = basic.set_index('code')['name'].to_dict()
    industry_map = basic.set_index('code')['industry'].to_dict()

    # 一次性预计算（选股和回测共用）
    signal_cache, yearly = st.precompute_all_signals_once(df_daily)

    print("\n===== 最新选股 =====")
    target_date = df_daily.index.get_level_values('date').max()
    selected, tier1, tier2 = select_stocks_at_date(
        signal_cache, yearly, df_daily, df_fin, conn, name_map, industry_map, target_date)

    if not selected:
        print("未选出股票")
    else:
        # 参考指标计算
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
    perf, bench_ret, nv = run_backtest_optimized(
        signal_cache, yearly, conn, df_daily, df_fin, name_map, industry_map, BACKTEST_START, BACKTEST_END)
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