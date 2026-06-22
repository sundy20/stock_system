#!/usr/bin/env python3
"""
选股 + 回测 + 导出 入口（v4.2）

使用方法：
    python3 app/backtest_runner.py

等同于原来的 backtest_twice_retest.py。
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import logging
import time
import os
import sys

# 确保项目根目录在 sys.path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import strategy as st
from backtest.engine import run_backtest_optimized, select_stocks_at_date

# ===================== 日志 =====================
logger = logging.getLogger("backtest_runner")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter(
        '%(asctime)s [%(levelname)-5s] %(message)s', datefmt='%H:%M:%S'))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)

# ===================== 回测参数 =====================
BACKTEST_START = (datetime.now() - timedelta(days=365*2)).strftime('%Y-%m-%d')
BACKTEST_END   = datetime.now().strftime('%Y-%m-%d')
INIT_CASH      = 1_000_000
MAX_STOCKS     = 200


def _load_params():
    """从 config.yaml 加载回测参数"""
    global BACKTEST_START, INIT_CASH, MAX_STOCKS
    try:
        import yaml
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        config_path = os.path.join(root, 'config.yaml')
        if os.path.exists(config_path):
            with open(config_path) as f:
                cfg = yaml.safe_load(f)
            bt = cfg.get('backtest', {})
            if bt:
                if 'init_cash' in bt:
                    INIT_CASH = bt['init_cash']
                if 'max_stocks' in bt:
                    MAX_STOCKS = bt['max_stocks']
                if 'lookback_years' in bt:
                    BACKTEST_START = (datetime.now() - timedelta(days=int(365 * bt['lookback_years']))).strftime('%Y-%m-%d')
            logger.info("已加载 config.yaml 中的回测参数")
    except Exception:
        pass


if __name__ == '__main__':
    _load_params()
    t_start = time.time()
    logger.info("=" * 50)
    logger.info("回测系统启动 v4.2")
    logger.info("=" * 50)

    # 数据校验
    conn = st.get_db_connection()
    df_daily = st.load_all_data(conn)
    df_fin   = st.load_financial_data(conn)

    ok, msgs = st.validate_data(df_daily, df_fin)
    for msg in msgs:
        logger.info("  %s", msg)
    if not ok:
        logger.error("数据校验不通过，退出。")
        conn.close()
        sys.exit(1)

    basic_df = st.load_basic_info(conn)
    basic = pd.read_sql("SELECT code, name, industry FROM stock_basic", conn)
    name_map = basic.set_index('code')['name'].to_dict()
    industry_map = basic.set_index('code')['industry'].to_dict()
    conn.close()

    # 预计算
    signal_cache, yearly = st.precompute_all_signals_once(df_daily)

    # 最新选股
    logger.info("=" * 50)
    logger.info("最新选股")
    logger.info("=" * 50)
    target_date = df_daily.index.get_level_values('date').max()
    selected, tier1, tier2 = select_stocks_at_date(
        signal_cache, yearly, df_daily, df_fin, basic_df,
        name_map, industry_map, target_date, MAX_STOCKS)

    if not selected:
        logger.warning("未选出股票")
    else:
        df_f = df_daily[df_daily.index.get_level_values('code').isin([s[0] for s in selected])]
        close_sel = df_f['close'].unstack(level='code')
        weekly_close = close_sel.resample('W').last()
        monthly_close = close_sel.resample('ME').last()
        w20 = weekly_close.rolling(20).mean().iloc[-1] if len(weekly_close) >= 20 else None
        m20 = monthly_close.rolling(20).mean().iloc[-1] if len(monthly_close) >= 20 else None
        w_std = weekly_close.rolling(20).std().iloc[-1] if len(weekly_close) >= 20 else None
        w_mid = weekly_close.rolling(20).mean().iloc[-1] if len(weekly_close) >= 20 else None
        w_upper = w_mid + 2 * w_std if w_mid is not None and w_std is not None else None

        logger.info("最终选股池（按层级排序）：")
        for c, n, ind, s in selected:
            logger.info("  %s %s [%s] %s", c, n, ind, s)

        # 导出
        with open('selected_stocks.txt', 'w') as f:
            for c, n, _, _ in selected:
                f.write(f"{c.replace('sh.','').replace('sz.','').replace('bj.','')},{n}\n")
        with open('selected_stocks_detail.txt', 'w') as f:
            f.write("代码,名称,行业,层级/信号,20周线,20月线,布林上轨(周),止损参考(10%)\n")
            for c, n, ind, s in selected:
                plain = c.replace('sh.','').replace('sz.','').replace('bj.','')
                w20v = w20.get(c, '') if w20 is not None else ''
                m20v = m20.get(c, '') if m20 is not None else ''
                wupp = w_upper.get(c, '') if w_upper is not None else ''
                last_close = close_sel[c].iloc[-1] if c in close_sel.columns else ''
                stop_loss = round(last_close * 0.9, 2) if isinstance(last_close, (int, float)) else ''
                f.write(f"{plain},{n},{ind},{s},{w20v},{m20v},{wupp},{stop_loss}\n")
        logger.info("结果已导出至 selected_stocks.txt / selected_stocks_detail.txt")

    # 回测
    logger.info("=" * 50)
    logger.info("运行回测")
    logger.info("=" * 50)
    perf, bench_ret, nv = run_backtest_optimized(
        signal_cache, yearly, df_daily, df_fin, basic_df,
        name_map, industry_map, BACKTEST_START, BACKTEST_END,
        init_cash=INIT_CASH, max_stocks=MAX_STOCKS)

    # 绩效报告
    logger.info("=" * 50)
    logger.info("回测绩效报告")
    logger.info("=" * 50)
    logger.info("初始资金:      %12.2f", INIT_CASH)
    logger.info("最终资金:      %12.2f", perf['final_value'])
    logger.info("总收益率:      %11.2f%%", perf['total_return'])
    logger.info("年化收益率:    %11.2f%%", perf['annual_return'])
    logger.info("夏普比率:      %12.2f", perf['sharpe'])
    logger.info("最大回撤:      %11.2f%%", perf['max_drawdown'])
    logger.info("卡玛比率:      %12.2f", perf['calmar'])
    logger.info("交易胜率:      %11.2f%%", perf['win_rate'])
    if bench_ret is not None:
        logger.info("沪深300收益:   %11.2f%%", bench_ret.iloc[-1])
        logger.info("超额收益:      %11.2f%%", perf['total_return'] - bench_ret.iloc[-1])
    logger.info("-" * 50)
    logger.info("--- 交易成本明细 ---")
    logger.info("总佣金:        %12.2f", perf['total_commission'])
    logger.info("总印花税:      %12.2f", perf['total_stamp_duty'])
    logger.info("总滑点:        %12.2f", perf['total_slippage'])
    logger.info("总成本:        %12.2f", perf['total_cost'])
    logger.info("成本占比:      %11.2f%%", perf['cost_ratio'])
    logger.info("总成交额:      %12.2f", perf['total_turnover'])
    logger.info("=" * 50)

    # 绘图
    try:
        import matplotlib.pyplot as plt
        plt.rcParams['font.sans-serif'] = ['Arial Unicode MS', 'PingFang HK', 'Heiti TC']
        plt.rcParams['axes.unicode_minus'] = False
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8))
        ax1.plot(nv.index, nv.values, label='策略净值', color='blue')
        if bench_ret is not None:
            ax1.plot(bench_ret.index, (bench_ret / 100 + 1) * INIT_CASH, label='沪深300', alpha=0.6)
        ax1.legend()
        ax1.grid()
        ax1.set_title('策略净值 vs 沪深300')
        dd = (nv / nv.cummax() - 1) * 100
        ax2.fill_between(dd.index, dd.values, 0, color='red', alpha=0.3)
        ax2.grid()
        ax2.set_title('回撤 %')
        plt.tight_layout()
        plt.show()
    except Exception as e:
        logger.warning("绘图失败: %s", e)

    elapsed = time.time() - t_start
    logger.info("选股回测完成，总耗时 %.1fs", elapsed)
