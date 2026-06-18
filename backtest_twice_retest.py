#!/usr/bin/env python3
"""
选股+回测+导出
条件：年线上升 + 净利润同比增长 > 0 + 月线/周线两次回踩20均线
（营收增长率检查已注释，如需启用请取消注释）
"""
import sqlite3
import backtrader as bt
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

DB_PATH = 'stocks_2y.db'

# ==================== 可调参数 ====================
START_DATE = (datetime.now() - timedelta(days=365)).strftime('%Y-%m-%d')
END_DATE = datetime.now().strftime('%Y-%m-%d')
INIT_CASH = 100000
FAST_MA = 5
SLOW_MA = 20
COMMISSION = 0.0001
STAMP_DUTY = 0.001
MIN_COMM = 5.0
SLIPPAGE = 0.001
RISK_FREE_RATE = 0.03
MAX_STOCKS = 5

# ==================== 佣金模型 ====================
class AStockCommission(bt.CommInfoBase):
    params = (
        ('commission', COMMISSION),
        ('stamp_duty', STAMP_DUTY),
        ('min_commission', MIN_COMM),
    )
    def _getcommission(self, size, price, pseudoexec):
        value = abs(size) * price
        comm = max(value * self.p.commission, self.p.min_commission)
        if size < 0:
            comm += value * self.p.stamp_duty
        return comm

# ==================== 策略 ====================
class DualMAStrategy(bt.Strategy):
    params = (('fast', FAST_MA), ('slow', SLOW_MA), ('target_codes', []))
    def __init__(self):
        self.ma_fast, self.ma_slow, self.crossover = {}, {}, {}
        for d in self.datas:
            if 'benchmark' in d._name: continue
            self.ma_fast[d._name] = bt.indicators.SMA(d.close, period=self.p.fast)
            self.ma_slow[d._name] = bt.indicators.SMA(d.close, period=self.p.slow)
            self.crossover[d._name] = bt.indicators.CrossOver(
                self.ma_fast[d._name], self.ma_slow[d._name])
    def next(self):
        for d in self.datas:
            name = d._name
            if 'benchmark' in name or name not in self.p.target_codes: continue
            if d.volume[0] == 0: continue
            if d.close[-1] > 0 and (d.close[0] >= d.close[-1]*1.099 or d.close[0] <= d.close[-1]*0.901):
                continue
            pos = self.getposition(d).size
            cross = self.crossover[name]
            if cross > 0 and pos == 0:
                self.buy(data=d, size=100)
            elif cross < 0 and pos > 0:
                self.close(data=d)

# ==================== 数据工具 ====================
def get_daily_df(conn, code, start, end):
    query = """SELECT date, name, open, high, low, close, volume
               FROM daily WHERE code=? AND date BETWEEN ? AND ? ORDER BY date"""
    df = pd.read_sql_query(query, conn, params=(code, start, end), parse_dates=['date'])
    if df.empty:
        return df
    df.set_index('date', inplace=True)
    df.sort_index(inplace=True)
    return df

def resample_to_monthly(daily_df):
    monthly = daily_df[['open','high','low','close','volume']].resample('M').agg({
        'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'})
    monthly.dropna(inplace=True)
    return monthly

def resample_to_weekly(daily_df):
    weekly = daily_df[['open','high','low','close','volume']].resample('W').agg({
        'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'})
    weekly.dropna(inplace=True)
    return weekly

def detect_twice_retest(df, ma_period=20, signal_ma=5, tolerance=0.1):
    if len(df) < ma_period + 10:
        return None
    close = df['close']
    low = df['low']
    ma20 = close.rolling(ma_period).mean()
    ma5 = close.rolling(signal_ma).mean()
    touch = (low <= ma20) & (low >= ma20 * (1 - tolerance))
    event_idx = [i for i in range(ma_period, len(df)) if touch.iloc[i]]
    if len(event_idx) < 2:
        return None
    last_touch = event_idx[-1]
    for i in range(last_touch+1, len(df)):
        if close.iloc[i] > ma5.iloc[i] and ma5.iloc[i] > 0:
            signal_date = df.index[i]
            if close.iloc[-1] > ma20.iloc[-1]:
                return signal_date
            else:
                return None
    return None

def get_latest_financials(conn, code):
    query = """SELECT net_profit_yoy, revenue_yoy FROM financial
               WHERE code=? AND net_profit_yoy IS NOT NULL
               ORDER BY stat_date DESC LIMIT 1"""
    row = conn.execute(query, (code,)).fetchone()
    if row:
        return {'net_profit_yoy': row[0], 'revenue_yoy': row[1]}
    return None

# ==================== 选股主逻辑 ====================
def select_stocks(conn):
    codes = pd.read_sql_query("SELECT DISTINCT code FROM daily", conn)['code'].tolist()
    print(f"数据库中总股票数：{len(codes)}")
    selected = []
    for code in codes:
        try:
            df_day = get_daily_df(conn, code, '2020-01-01', datetime.now().strftime('%Y-%m-%d'))
            if df_day.empty or len(df_day) < 300:
                continue

            # 年线上升
            close = df_day['close']
            ma250 = close.rolling(250).mean()
            if ma250.isnull().all():
                continue
            ma250_valid = ma250.dropna()
            if len(ma250_valid) < 20:
                continue
            if ma250_valid.iloc[-1] <= ma250_valid.iloc[-20]:
                continue

            # 财务：净利润同比增长 > 0
            fin = get_latest_financials(conn, code)
            if fin is None:
                continue
            if fin['net_profit_yoy'] is None or fin['net_profit_yoy'] <= 0:
                continue
            # 如需启用营收检查，取消下面两行注释
            # if fin['revenue_yoy'] is None or fin['revenue_yoy'] <= 0:
            #     continue

            stock_name = df_day['name'].iloc[-1] if 'name' in df_day.columns else code

            # 月线两次回踩
            df_monthly = resample_to_monthly(df_day)
            signal_monthly = detect_twice_retest(df_monthly, ma_period=20, signal_ma=5, tolerance=0.1)
            if signal_monthly is not None:
                selected.append((code, stock_name, '月线', signal_monthly))
                print(f"月线信号：{code} {stock_name}  信号日期 {signal_monthly.date()}")
                if len(selected) >= MAX_STOCKS:
                    break
                continue

            # 周线两次回踩
            df_weekly = resample_to_weekly(df_day)
            signal_weekly = detect_twice_retest(df_weekly, ma_period=20, signal_ma=5, tolerance=0.1)
            if signal_weekly is not None:
                selected.append((code, stock_name, '周线', signal_weekly))
                print(f"周线信号：{code} {stock_name}  信号日期 {signal_weekly.date()}")
                if len(selected) >= MAX_STOCKS:
                    break
        except:
            continue
    return selected

# ==================== 主流程 ====================
if __name__ == '__main__':
    conn = sqlite3.connect(DB_PATH)
    selected = select_stocks(conn)

    if not selected:
        print("没有选出符合条件的股票，可放宽条件或增大 MAX_STOCKS。")
        conn.close()
        exit()

    stock_codes = [s[0] for s in selected]
    print("\n最终选股池：")
    for s in selected:
        print(f"  {s[0]} {s[1]}  信号类型: {s[2]}  信号日期: {s[3].date()}")

    # 导出同花顺
    export_file = 'selected_stocks.txt'
    with open(export_file, 'w', encoding='utf-8') as f:
        for s in selected:
            code = s[0].replace('sh.', '').replace('sz.', '')
            name = s[1]
            f.write(f"{code},{name}\n")
    print(f"\n选股结果已导出至 {export_file}")

    # 加载回测数据
    data_feeds = []
    for code in stock_codes:
        df = get_daily_df(conn, code, START_DATE, END_DATE)
        if df.empty:
            continue
        df['openinterest'] = 0
        data = bt.feeds.PandasData(dataname=df, name=code)
        data_feeds.append(data)

    # 基准
    bench_code = 'sh.000300'
    bench_df = get_daily_df(conn, bench_code, START_DATE, END_DATE)
    if not bench_df.empty:
        bench_df['open'] = bench_df['high'] = bench_df['low'] = bench_df['close']
        bench_df['volume'] = 1
        bench_df['openinterest'] = 0
        bench_data = bt.feeds.PandasData(dataname=bench_df, name='benchmark')
    else:
        bench_data = None

    conn.close()

    cerebro = bt.Cerebro()
    for d in data_feeds:
        cerebro.adddata(d)
    if bench_data:
        cerebro.adddata(bench_data)

    cerebro.addstrategy(DualMAStrategy, target_codes=stock_codes)
    cerebro.broker.setcash(INIT_CASH)
    cerebro.broker.addcommissioninfo(AStockCommission())
    cerebro.broker.set_slippage_perc(perc=SLIPPAGE)
    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name='sharpe', riskfreerate=RISK_FREE_RATE)
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name='drawdown')
    if bench_data:
        cerebro.addanalyzer(bt.analyzers.TimeReturn, _name='bench', data=bench_data)

    print("\n回测开始...")
    init_val = cerebro.broker.getvalue()
    results = cerebro.run()
    final_val = cerebro.broker.getvalue()
    strat_ret = (final_val / init_val - 1) * 100

    print(f"初始资金: {init_val:.2f}")
    print(f"最终资金: {final_val:.2f}")
    print(f"策略收益率: {strat_ret:.2f}%")
    s = results[0]
    sharpe = s.analyzers.sharpe.get_analysis().get('sharperatio', 'N/A')
    print(f"夏普比率: {sharpe}")
    dd = s.analyzers.drawdown.get_analysis()
    max_dd = dd.get('max', {}).get('drawdown', 0)
    print(f"最大回撤: {max_dd:.2f}%")

    if bench_data:
        b = s.analyzers.bench.get_analysis()
        bench_cum = np.prod([1 + r for r in b]) - 1
        print(f"沪深300收益率: {bench_cum*100:.2f}%")
        print(f"超额收益: {strat_ret - bench_cum*100:.2f}%")

    try:
        cerebro.plot()
    except:
        print("绘图跳过")

    print("\n回测完成。")