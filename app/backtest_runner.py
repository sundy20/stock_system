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
STOP_LOSS_ENABLED = True
STOP_LOSS_PCT     = -10.0

# 信号评分权重
SIGNAL_SCORES = {
    '月布林+周二次回踩+周布林': 9,
    '月布林+周二次回踩': 10,       # ★ 历史最佳
    '全信号共振': 8,
    '月布林': 7,
    '周二次回踩+周布林': 6,
    '周二次回踩': 5,
}


def _build_signal_scores():
    """从 config.yaml 加载信号权重，失败则用默认值"""
    try:
        import yaml
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        config_path = os.path.join(root, 'config.yaml')
        if os.path.exists(config_path):
            with open(config_path) as f:
                cfg = yaml.safe_load(f)
            sr = cfg.get('strategy', {}).get('signal_ranking', {})
            if sr:
                return {
                    '月布林+周二次回踩+周布林': sr.get('w_retest_w_bb', 6),
                    '月布林+周二次回踩': sr.get('m_bb_w_retest', 10),
                    '全信号共振': sr.get('all_resonance', 8),
                    '月布林': sr.get('m_bb', 7),
                    '周二次回踩+周布林': sr.get('w_retest_w_bb', 6),
                    '周二次回踩': sr.get('w_retest', 5),
                }
    except Exception:
        pass
    return SIGNAL_SCORES


def _calc_composite_score(signal_desc, signal_scores):
    """根据信号描述计算综合评分"""
    desc = signal_desc.replace('弹性降级：', '')
    # 精确匹配
    if desc in signal_scores:
        return float(signal_scores[desc])
    # 模糊匹配（取最高匹配分）
    best = 0
    for key, val in signal_scores.items():
        if key in desc or desc in key:
            best = max(best, val)
    return float(best) if best > 0 else 3.0


def _load_params():
    """从 config.yaml 加载回测参数"""
    global BACKTEST_START, INIT_CASH, MAX_STOCKS, STOP_LOSS_ENABLED, STOP_LOSS_PCT
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
                if 'stop_loss_enabled' in bt:
                    STOP_LOSS_ENABLED = bt['stop_loss_enabled']
                if 'stop_loss_pct' in bt:
                    STOP_LOSS_PCT = bt['stop_loss_pct']
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
        monthly_close = close_sel.resample('M').last()
        w20 = weekly_close.rolling(20).mean().iloc[-1] if len(weekly_close) >= 20 else None
        m20 = monthly_close.rolling(20).mean().iloc[-1] if len(monthly_close) >= 20 else None
        w_std = weekly_close.rolling(20).std().iloc[-1] if len(weekly_close) >= 20 else None
        w_mid = weekly_close.rolling(20).mean().iloc[-1] if len(weekly_close) >= 20 else None
        w_upper = w_mid + 2 * w_std if w_mid is not None and w_std is not None else None

        # ★ v4.2: 信号优先级排序 + 综合评分
        signal_scores = _build_signal_scores()
        for i, (c, n, ind, s) in enumerate(selected):
            score = _calc_composite_score(s, signal_scores)
            selected[i] = (c, n, ind, s, score)

        selected.sort(key=lambda x: x[4], reverse=True)

        logger.info("最终选股池（按综合评分排序）：")
        for c, n, ind, s, score in selected:
            logger.info("  %5.1f %s %s [%s] %s", score, c, n, ind, s)

        # 导出
        with open('selected_stocks.txt', 'w') as f:
            for c, n, _, _, _ in selected:
                f.write(f"{c.replace('sh.','').replace('sz.','').replace('bj.','')},{n}\n")
        with open('selected_stocks_detail.txt', 'w') as f:
            f.write("综合评分,代码,名称,行业,信号组合,20周线,20月线,布林上轨(周),止损参考(10%)\n")
            for c, n, ind, s, score in selected:
                plain = c.replace('sh.','').replace('sz.','').replace('bj.','')
                w20v = w20.get(c, '') if w20 is not None else ''
                m20v = m20.get(c, '') if m20 is not None else ''
                wupp = w_upper.get(c, '') if w_upper is not None else ''
                last_close = close_sel[c].iloc[-1] if c in close_sel.columns else ''
                stop_loss = round(last_close * 0.9, 2) if isinstance(last_close, (int, float)) else ''
                f.write(f"{score},{plain},{n},{ind},{s},{w20v},{m20v},{wupp},{stop_loss}\n")
        logger.info("结果已导出至 selected_stocks.txt / selected_stocks_detail.txt")

    # 回测
    logger.info("=" * 50)
    logger.info("运行回测")
    logger.info("=" * 50)
    perf, bench_ret, nv = run_backtest_optimized(
        signal_cache, yearly, df_daily, df_fin, basic_df,
        name_map, industry_map, BACKTEST_START, BACKTEST_END,
        init_cash=INIT_CASH, max_stocks=MAX_STOCKS,
        stop_loss_enabled=STOP_LOSS_ENABLED, stop_loss_pct=STOP_LOSS_PCT)

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
    if STOP_LOSS_ENABLED:
        logger.info("-" * 50)
        logger.info("--- 止损统计 ---")
        logger.info("止损触发次数:  %12.0f", perf.get('stop_loss_count', 0))
        logger.info("止损卖出金额:  %12.2f", perf.get('total_stop_loss_amount', 0))
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
