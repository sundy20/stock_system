#!/bin/bash
cd "$(dirname "$0")"
source venv/bin/activate
echo "===== 更新日线数据（tushare） ====="
python3 download_daily_tushare.py
echo "===== 更新财务数据（baostock，无积分门槛） ====="
python3 download_financials.py          # 使用旧的 baostock 财务脚本
echo "✅ 数据更新完毕"