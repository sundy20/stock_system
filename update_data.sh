#!/bin/bash
cd /Users/plus/AI/stock_system
source venv/bin/activate
echo "===== 更新日线数据 ====="
python3 download_2years.py
echo "===== 更新财务数据 ====="
python3 download_financials.py
echo "✅ 数据更新完毕"