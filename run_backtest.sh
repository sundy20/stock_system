#!/bin/bash
cd /Users/plus/AI/stock_system
source venv/bin/activate
echo "===== 选股回测 + 导出 ====="
python3 backtest_twice_retest.py
echo "✅ 回测完成，选股结果已导出至 selected_stocks.txt"