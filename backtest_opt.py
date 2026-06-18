#!/usr/bin/env python3
"""
选股+回测+导出 优化最终版
条件：年线上升 + 净利润同比增长 > 0 + 月线/周线两次回踩20均线
优化点：全向量化选股、定期调仓回测、财报按发布日期生效、无未来函数
"""
import sqlite3
import pandas as pd
import numpy as np
import vectorbt as vbt
from datetime import datetime, timedelta

DB_PATH = 'stocks_2y.db'

# ==================== 可调参数 ====================
BACKTEST_START = (datetime.now() - timedelta(days=365)).strftime('%Y-%m-%d')
BACKTEST_END = datetime.now().strftime('%Y-%m-%d')
INIT_CASH = 100000
MAX_STOCKS = 80
TOLERANCE = 0.1          # 回踩容忍度 10%
MIN_LIQUIDITY = 5000     # 日均成交额最低阈值（万元）
REBALANCE_FREQ = 'W'     # 调仓频率：W周 / M月
COMMISSION = 0.0003      # 佣金费率
SLIPPAGE = 0.001         # 滑点
STAMP_DUTY = 0.001       # 印花税（卖出）

# ==================== 数据加载 ====================
def load_all_data(conn):
    """一次性加载所有日线数据"""
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
    """加载财务数据，按发布日期生效（消除未来函数）"""
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
def detect_retest_signal(price_df, ma_period=20, signal_ma=5, tolerance=TOLERANCE):
    """向量化检测上升趋势中的两次回踩信号"""
    close = price_df['close']
    low = price_df['low']

    ma20 = close.rolling(ma_period).mean()
    ma5 = close.rolling(signal_ma).mean()

    # 前提：20均线向上，确保是上升趋势中的回调
    ma20_slope = ma20.diff(10) / 10
    ma20_up = ma20_slope > 0

    # 回踩触碰（允许下探容忍度）
    touch = (low <= ma20) & (low >= ma20 * (1 - tolerance))
    # 60周期内至少2次回踩
    touch_count = touch.rolling(60).sum()
    twice_touch = touch_count >= 2

    # 站上5均线且当前价高于20均线
    above_ma5 = close > ma5
    above_ma20 = close > ma20

    return ma20_up & twice_touch & above_ma5 & above_ma20

# ==================== 选股主逻辑 ====================
def vectorized_select_stocks(df_daily, df_fin, target_date=None):
    """
    向量化批量选股
    target_date: 选股日期，默认最新交易日
    """
    if target_date is None:
        target_date = df_daily.index.get_level_values('date').max()

    # 截取到目标日期的数据
    df = df_daily.loc[df_daily.index.get_level_values('date') <= target_date].copy()

    # 1. 日线级别基础筛选：年线上升 + 站上250 + 流动性
    grouped = df.groupby(level='code')
    df['ma250'] = grouped['close'].transform(lambda x: x.rolling(250).mean())
    df['ma250_slope'] = grouped['ma250'].transform(lambda x: x.diff(20) / 20)
    df['amount'] = df['close'] * df['volume'] / 10000
    df['amount_ma20'] = grouped['amount'].transform(lambda x: x.rolling(20).mean())

    latest = df.groupby(level='code').tail(1)
    cond_ma250_up = latest['ma250_slope'] > 0
    cond_above_ma250 = latest['close'] > latest['ma250']
    cond_liquid = latest['amount_ma20'] >= MIN_LIQUIDITY

    base_selected = latest[cond_ma250_up & cond_above_ma250 & cond_liquid].index.get_level_values('code').tolist()
    df_filtered = df[df.index.get_level_values('code').isin(base_selected)]

    if df_filtered.empty:
        return []

    # 展开为日期×股票矩阵
    daily_close = df_filtered['close'].unstack(level='code')
    daily_low = df_filtered['low'].unstack(level='code')

    # 2. 周线级别检测
    weekly_close = daily_close.resample('W').last()
    weekly_low = daily_low.resample('W').min()
    weekly_df = pd.DataFrame({'close': weekly_close.stack(), 'low': weekly_low.stack()})

    weekly_signal = detect_retest_signal(weekly_df, ma_period=20, signal_ma=5)
    weekly_signal_latest = weekly_signal.groupby(level='code').tail(1)
    weekly_codes = weekly_signal_latest[weekly_signal_latest].index.get_level_values('code').tolist()

    # 3. 月线级别检测
    monthly_close = daily_close.resample('M').last()
    monthly_low = daily_low.resample('M').min()
    monthly_df = pd.DataFrame({'close': monthly_close.stack(), 'low': monthly_low.stack()})

    monthly_signal = detect_retest_signal(monthly_df, ma_period=20, signal_ma=5)
    monthly_signal_latest = monthly_signal.groupby(level='code').tail(1)
    monthly_codes = monthly_signal_latest[monthly_signal_latest].index.get_level_values('code').tolist()

    # 合并技术面信号
    tech_codes = list(set(weekly_codes + monthly_codes))

    # 4. 财务条件：最新生效的净利润同比 > 0
    fin_before = df_fin[df_fin['effective_date'] <= target_date]
    if fin_before.empty:
        return []
    fin_latest = fin_before.sort_values('effective_date').groupby('code').last()
    fin_codes = fin_latest[fin_latest['net_profit_yoy'] > 0].index.tolist()

    # 最终交集，取前N只
    final_codes = [c for c in tech_codes if c in fin_codes][:MAX_STOCKS]

    # 获取名称与信号类型
    name_map = pd.read_sql("SELECT code, name FROM stock_basic", conn).set_index('code')['name'].to_dict()
    result = []
    for code in final_codes:
        signal_type = '月线' if code in monthly_codes else '周线'
        result.append((code, name_map.get(code, code), signal_type))
    return result

# ==================== 定期调仓回测 ====================
def run_rebalance_backtest(df_daily, df_fin, start_date, end_date):
    """定期调仓回测，与实盘逻辑完全对齐"""
    close = df_daily['close'].unstack(level='code')
    close = close.loc[start_date:end_date].dropna(axis=1, how='all')

    rebalance_dates = close.resample(REBALANCE_FREQ).last().index

    # 计算每期选股池
    stock_pools = {}
    for date in rebalance_dates:
        selected = vectorized_select_stocks(df_daily, df_fin, target_date=date)
        stock_pools[date] = [s[0] for s in selected if s[0] in close.columns]

    # 构建等权重组态矩阵
    weights = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    for date in rebalance_dates:
        codes = stock_pools.get(date, [])
        if len(codes) > 0:
            weights.loc[date, codes] = 1 / len(codes)

    # 持有期仓位不变，前向填充
    weights = weights.replace(0, np.nan).ffill().fillna(0)

    # 运行回测
    portfolio = vbt.Portfolio.from_weights(
        close, weights,
        init_cash=INIT_CASH,
        fees=COMMISSION,
        slippage=SLIPPAGE,
        freq='d',
        stamp_duty=STAMP_DUTY,
        stamp_duty_side='sell'
    )

    return portfolio, stock_pools

# ==================== 主流程 ====================
if __name__ == '__main__':
    conn = sqlite3.connect(DB_PATH)

    print("加载数据...")
    df_daily = load_all_data(conn)
    df_fin = load_financial_data(conn)

    # 最新选股
    print("\n执行选股...")
    selected = vectorized_select_stocks(df_daily, df_fin)

    if not selected:
        print("没有选出符合条件的股票，可放宽条件或增大 MAX_STOCKS。")
        conn.close()
        exit()

    print("\n最终选股池：")
    for code, name, sig_type in selected:
        print(f"  {code} {name}  信号类型: {sig_type}")

    # 导出同花顺
    export_file = 'selected_stocks.txt'
    with open(export_file, 'w', encoding='utf-8') as f:
        for code, name, _ in selected:
            code_plain = code.replace('sh.', '').replace('sz.', '')
            f.write(f"{code_plain},{name}\n")
    print(f"\n选股结果已导出至 {export_file}")

    # 回测
    print("\n运行回测...")
    portfolio, pools = run_rebalance_backtest(df_daily, df_fin, BACKTEST_START, BACKTEST_END)

    # 输出绩效报告
    stats = portfolio.stats()
    print("\n" + "=" * 45)
    print("回测绩效报告")
    print("=" * 45)
    print(f"初始资金:    {INIT_CASH:>12.2f}")
    print(f"最终资金:    {portfolio.value()[-1]:>12.2f}")
    print(f"总收益率:    {stats['Total Return [%]']:>11.2f}%")
    print(f"年化收益率:  {stats['Annualized Return [%]']:>11.2f}%")
    print(f"夏普比率:    {stats['Sharpe Ratio']:>12.2f}")
    print(f"最大回撤:    {stats['Max Drawdown [%]']:>11.2f}%")
    print(f"卡玛比率:    {stats['Calmar Ratio']:>12.2f}")
    print(f"交易胜率:    {stats['Win Rate [%]']:>11.2f}%")
    print("=" * 45)

    # 绘图（可选）
    try:
        portfolio.plot().show()
    except:
        print("绘图跳过")

    conn.close()
    print("\n选股回测完成。")