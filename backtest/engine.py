"""
回测引擎 — 选股 + 月/周度调仓 + 净值计算
"""

import warnings
warnings.filterwarnings('ignore', message=".*'M' is deprecated.*")
warnings.filterwarnings('ignore', message=".*'W' is deprecated.*")

import pandas as pd
import numpy as np
import logging
from datetime import timedelta

import strategy as st

logger = logging.getLogger("backtest.engine")

# ===================== 涨跌停检测（区分交易所） =====================

def get_limit_mask(pct_chg_series):
    """
    向量化涨跌停检测，返回 (limit_up_mask, limit_down_mask)。
    根据股票代码前缀区分交易所阈值：主板 ±9.8%, 科创/创业板 ±19.8%, 北交所 ±29.8%
    """
    if pct_chg_series.empty:
        return pd.Series(False, index=pct_chg_series.index), \
               pd.Series(False, index=pct_chg_series.index)

    codes = pct_chg_series.index
    is_bj   = pd.Index([str(c).startswith('bj.') for c in codes])
    is_star = pd.Index([str(c).startswith('sh.688') for c in codes])
    is_gem  = pd.Index([str(c).startswith('sz.300') for c in codes])
    is_main = ~(is_bj | is_star | is_gem)

    limit_up = pd.Series(False, index=pct_chg_series.index)
    limit_up[is_main] = pct_chg_series[is_main] >= 9.8
    limit_up[is_star] = pct_chg_series[is_star] >= 19.8
    limit_up[is_gem]  = pct_chg_series[is_gem] >= 19.8
    limit_up[is_bj]   = pct_chg_series[is_bj] >= 29.8

    limit_down = pd.Series(False, index=pct_chg_series.index)
    limit_down[is_main] = pct_chg_series[is_main] <= -9.8
    limit_down[is_star] = pct_chg_series[is_star] <= -19.8
    limit_down[is_gem]  = pct_chg_series[is_gem] <= -19.8
    limit_down[is_bj]   = pct_chg_series[is_bj] <= -29.8

    return limit_up, limit_down


# ===================== 选股 =====================

def select_stocks_at_date(signal_cache, yearly, df_daily, df_fin, basic_df,
                          name_map, industry_map, target_date, max_stocks=200):
    """
    使用预计算缓存，在 target_date 选取股票（两层共振机制）。
    无信号重算，只做缓存切片 + 筛选。
    """
    target_ts = pd.Timestamp(target_date)

    # 1. 前置剔除
    valid_codes = st.get_valid_codes(df_daily, target_ts, basic_df=basic_df)

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

    logger.info("年线+流动性通过：%s 只", len(base_codes))
    if not base_codes:
        return [], [], []

    # 3. 信号提取
    m_retest_codes, m_bb_codes, w_retest_codes, w_bb_codes = [], [], [], []
    for code in base_codes:
        entry = signal_cache[code]
        if st.get_latest_value(entry['m_retest'], target_ts): m_retest_codes.append(code)
        if st.get_latest_value(entry['m_bb'], target_ts):     m_bb_codes.append(code)
        if st.get_latest_value(entry['w_retest'], target_ts): w_retest_codes.append(code)
        if st.get_latest_value(entry['w_bb'], target_ts):     w_bb_codes.append(code)

    logger.info("月线回踩: %s 只，月线布林: %s 只", len(m_retest_codes), len(m_bb_codes))
    logger.info("周线回踩: %s 只，周线布林: %s 只", len(w_retest_codes), len(w_bb_codes))

    # 4. 财务筛选
    fin_codes = st.apply_financial_filter(base_codes, df_fin, target_ts)
    if st.USE_FINANCIAL_FILTER:
        logger.info("财务符合：%s 只", len(fin_codes))
    else:
        logger.info("财务条件已关闭，全部通过")

    # 5. 两层交集
    core_set = (set(base_codes) & set(m_retest_codes) & set(m_bb_codes) &
                set(w_retest_codes) & set(w_bb_codes) & set(fin_codes))
    hard_base = set(base_codes) & (set(m_retest_codes) | set(w_retest_codes)) & set(fin_codes)

    tier1, tier2 = [], []
    assigned = set()
    for code in sorted(core_set):
        assigned.add(code)
        tier1.append((code, '全信号共振'))
    for code in sorted(hard_base - assigned):
        soft_a, soft_b = code in m_bb_codes, code in w_retest_codes
        soft_c = soft_b and code in w_bb_codes
        parts = []
        if soft_a: parts.append('月布林')
        if soft_b: parts.append('周二次回踩')
        if soft_c: parts.append('周布林')
        if parts:
            tier2.append((code, '弹性降级：' + '+'.join(parts)))

    final_selected = tier1 + tier2
    final = [(c, name_map.get(c, c), industry_map.get(c, ''), d)
             for c, d in final_selected[:max_stocks]]
    logger.info("核心共振: %s 只，弹性降级: %s 只，合计: %s 只",
                len(tier1), len(tier2), len(final))
    return final, tier1, tier2


# ===================== 回测引擎 =====================

def run_backtest_optimized(signal_cache, yearly, df_daily, df_fin, basic_df,
                           name_map, industry_map, start, end,
                           init_cash=1_000_000, max_stocks=200, bench_code=None,
                           stop_loss_enabled=True, stop_loss_pct=-10.0):
    """执行月/周度调仓回测。v4.2: 支持日内止损"""
    bench_code = bench_code or st.BENCH_CODE
    close = df_daily['close'].unstack(level='code').loc[start:end].dropna(axis=1, how='all')
    bench = close[bench_code] if bench_code in close.columns else None
    stock_close = close[[c for c in close.columns if c != bench_code]].ffill()

    freq = 'W' if st.REBALANCE_FREQ == 'W' else 'M'
    periods = stock_close.index.to_period(freq)
    rebalance_dates = stock_close.groupby(periods).apply(lambda x: x.index[-1]).values

    pct_chg_df = df_daily['pct_chg'].unstack(level='code')

    cash = init_cash
    holdings = {}
    net_vals = []
    last_date = None

    total_commission = 0.0
    total_stamp_duty = 0.0
    total_slippage = 0.0
    total_turnover = 0.0

    signal_positions = {}
    signal_stats = {}
    stop_loss_count = 0           # v4.2: 止损触发次数
    total_stop_loss_amount = 0.0  # v4.2: 止损总金额

    logger.info("开始回测，共 %s 个调仓日", len(rebalance_dates))

    for idx, d_raw in enumerate(rebalance_dates):
        d = pd.Timestamp(d_raw)
        if (idx + 1) % 10 == 0 or idx == len(rebalance_dates) - 1:
            logger.info("  回测进度: %s/%s", idx + 1, len(rebalance_dates))

        # 选股
        sel, _, _ = select_stocks_at_date(
            signal_cache, yearly, df_daily, df_fin, basic_df,
            name_map, industry_map, d, max_stocks)
        targets = [s[0] for s in sel if s[0] in stock_close.columns]

        # 涨跌停检测
        if d in pct_chg_df.index:
            today_pct = pct_chg_df.loc[d]
            limit_up_mask, limit_down_mask = get_limit_mask(today_pct)
        else:
            limit_up_mask = pd.Series(False, dtype=bool)
            limit_down_mask = pd.Series(False, dtype=bool)

        # ★ v4.2: 调仓间每日净值 + 止损检查
        if last_date is not None:
            inter_mask = (stock_close.index > last_date) & (stock_close.index < d)
            inter_days = stock_close.index[inter_mask]

            for day in inter_days:
                # 止损检查
                if stop_loss_enabled and holdings:
                    for c in list(holdings.keys()):
                        if c not in stock_close.columns or c not in signal_positions:
                            continue
                        if pd.isna(stock_close.loc[day, c]):
                            continue
                        current_price = stock_close.loc[day, c]
                        _, buy_price, sig_label = signal_positions[c]
                        pnl_pct = (current_price / buy_price - 1) * 100
                        if pnl_pct <= stop_loss_pct:
                            # 执行止损卖出
                            shares = holdings.pop(c)
                            sell_amount = shares * current_price
                            commission = max(sell_amount * st.COMMISSION, 5.0)
                            stamp = sell_amount * st.STAMP_DUTY
                            cash += sell_amount - commission - stamp
                            total_commission += commission
                            total_stamp_duty += stamp
                            total_turnover += sell_amount
                            stop_loss_count += 1
                            total_stop_loss_amount += sell_amount
                            # 记录信号归因
                            ret_pct = pnl_pct
                            if sig_label not in signal_stats:
                                signal_stats[sig_label] = {'count': 0, 'total_return': 0.0, 'wins': 0}
                            signal_stats[sig_label]['count'] += 1
                            signal_stats[sig_label]['total_return'] += ret_pct
                            del signal_positions[c]

                # 当日净值
                if holdings:
                    val = float(sum(
                        holdings[c] * stock_close.loc[day, c]
                        for c in holdings if c in stock_close.columns and not pd.isna(stock_close.loc[day, c])
                    ) + cash)
                else:
                    val = cash
                net_vals.append((day, val))

        # 卖出全部
        for c in list(holdings.keys()):
            if c in stock_close.columns:
                sell_price = stock_close.loc[d, c]
                shares = holdings[c]
                sell_amount = shares * sell_price
                commission = max(sell_amount * st.COMMISSION, 5.0)
                stamp = sell_amount * st.STAMP_DUTY
                sell_cost = commission + stamp
                cash += sell_amount - sell_cost
                total_commission += commission
                total_stamp_duty += stamp
                total_turnover += sell_amount
                # 信号归因
                if c in signal_positions:
                    buy_date, buy_price, sig_label = signal_positions.pop(c)
                    ret_pct = (sell_price / buy_price - 1) * 100
                    if sig_label not in signal_stats:
                        signal_stats[sig_label] = {'count': 0, 'total_return': 0.0, 'wins': 0}
                    signal_stats[sig_label]['count'] += 1
                    signal_stats[sig_label]['total_return'] += ret_pct
                    if ret_pct > 0:
                        signal_stats[sig_label]['wins'] += 1
        holdings.clear()

        # 等权买入
        if targets:
            buy_targets = [c for c in targets
                          if not limit_up_mask.get(c, False) and not limit_down_mask.get(c, False)]
            if buy_targets:
                prices = stock_close.loc[d, buy_targets]
                avg_cash = cash / len(prices)
                for c in buy_targets:
                    price = prices[c]
                    if pd.isna(price) or price <= 0:
                        continue
                    est_shares = int(avg_cash / (price * (1 + st.SLIPPAGE))) // 100 * 100
                    if est_shares <= 0:
                        continue
                    est_amount = est_shares * price
                    est_commission = max(est_amount * st.COMMISSION, 5.0)
                    per_share_cost = (est_amount * (1 + st.SLIPPAGE) + est_commission) / est_shares
                    shares = int(avg_cash / per_share_cost) // 100 * 100
                    if shares <= 0:
                        continue
                    amount = shares * price
                    commission = max(amount * st.COMMISSION, 5.0)
                    slippage = amount * st.SLIPPAGE
                    total_cost = amount + commission + slippage
                    cash -= total_cost
                    holdings[c] = shares
                    total_commission += commission
                    total_slippage += slippage
                    total_turnover += amount
                    # 信号归因记录
                    signal_label = next((s[3] for s in sel if s[0] == c), 'unknown')
                    signal_positions[c] = (d, price, signal_label)

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
        net_df.loc[stock_close.index[0]] = init_cash
        net_df = net_df.sort_index()
    net_value = net_df['value']

    # 绩效 + 归因
    from .report import calc_performance, export_signal_attribution
    perf = calc_performance(net_value, init_cash)
    bench_ret = (bench / bench.iloc[0] - 1) * 100 if bench is not None else None

    perf['total_commission'] = total_commission
    perf['total_stamp_duty'] = total_stamp_duty
    perf['total_slippage'] = total_slippage
    perf['total_turnover'] = total_turnover
    perf['total_cost'] = total_commission + total_stamp_duty + total_slippage
    perf['cost_ratio'] = (perf['total_cost'] / total_turnover * 100) if total_turnover > 0 else 0
    perf['avg_trades_per_rebalance'] = total_turnover / len(rebalance_dates) if len(rebalance_dates) > 0 else 0
    perf['signal_stats'] = signal_stats
    perf['stop_loss_count'] = stop_loss_count
    perf['total_stop_loss_amount'] = total_stop_loss_amount

    export_signal_attribution(signal_stats)
    return perf, bench_ret, net_value
