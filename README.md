# A股量化选股与回测系统

## 概述
基于「大周期趋势反转 + 基本面健康验证 + 中周期技术择时」的中线选股策略，每周自动更新数据，输出同花顺可导入的股票列表，并附带回测绩效报告。

## 快速开始
```bash
# 1. 安装依赖
pip install tushare pandas numpy matplotlib baostock

# 2. 设置环境变量
export TUSHARE_TOKEN="你的token"

# 3. 更新数据并选股
./update_data.sh && ./run_backtest.sh