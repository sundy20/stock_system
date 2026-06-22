"""
绩效报告 — 收益计算 + 信号归因导出
"""

import numpy as np
import csv
import logging

logger = logging.getLogger("backtest.report")


def calc_performance(nv, init, r=0.03):
    """计算年化收益率、夏普比率、最大回撤等绩效指标"""
    ret = nv.pct_change(fill_method=None).dropna()
    if len(ret) < 2:
        return {
            'total_return': 0, 'annual_return': 0, 'sharpe': 0,
            'max_drawdown': 0, 'calmar': 0, 'win_rate': 0, 'final_value': init
        }
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


def export_signal_attribution(signal_stats, output_file='signal_attribution.csv'):
    """导出信号归因统计到 CSV，并在日志中打印摘要"""
    if not signal_stats:
        logger.info("无信号归因数据")
        return

    rows = []
    for label, stats in sorted(signal_stats.items(), key=lambda x: -x[1]['count']):
        n = stats['count']
        avg_ret = stats['total_return'] / n if n > 0 else 0
        win_rate = stats['wins'] / n * 100 if n > 0 else 0
        rows.append({
            '信号类型': label,
            '交易次数': n,
            '平均收益率(%)': round(avg_ret, 2),
            '胜率(%)': round(win_rate, 1),
            '累积收益(%)': round(stats['total_return'], 2),
        })

    with open(output_file, 'w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=['信号类型', '交易次数', '平均收益率(%)', '胜率(%)', '累积收益(%)'])
        writer.writeheader()
        writer.writerows(rows)

    logger.info("-" * 50)
    logger.info("信号归因报告 → %s", output_file)
    logger.info("%-24s %6s %10s %8s %10s", '信号类型', '次数', '平均收益%', '胜率%', '累积收益%')
    for row in rows:
        logger.info("%-24s %6s %10s %8s %10s",
                   row['信号类型'][:24], row['交易次数'],
                   row['平均收益率(%)'], row['胜率(%)'], row['累积收益(%)'])
    logger.info("-" * 50)
