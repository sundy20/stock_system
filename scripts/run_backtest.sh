#!/bin/bash
# ============================================================
# 选股回测脚本 v2.0
# ============================================================
set -euo pipefail

cd "$(dirname "$0")/.."
source venv/bin/activate

echo "===== 选股回测 + 导出 v4.3 ====="
python3 app/backtest_runner.py
echo "✓ 回测完成"
echo "  选股结果:  selected_stocks.txt / selected_stocks_detail.csv"
echo "  信号归因:  signal_attribution.csv"
echo "  预计算缓存: .signal_cache.pkl（下次运行自动命中）"
