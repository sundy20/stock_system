# A股量化选股与回测系统 v4.3

基于「大周期趋势反转 + 基本面健康验证 + 中周期技术择时」的中线选股策略。

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 设置 Tushare Token
export TUSHARE_TOKEN="你的token"

# 3. 首次使用：下载全量数据
python3 pipeline/daily_tushare.py         # 日线（tushare, ~5分钟）
python3 pipeline/financial_baostock.py    # 财务（baostock, ~20分钟）

# 4. 运行回测 + 选股
./scripts/run_backtest.sh
```

## 日常使用

```bash
# 更新数据（自动备份）
./scripts/update_data.sh

# 运行回测（第二次开始命中缓存，~50秒）
./scripts/run_backtest.sh

# 自选股诊断
python3 app/stock_checker.py                     # 最新交易日
python3 app/stock_checker.py --date 2025-06-15   # 历史日期
python3 app/stock_checker.py -f my_list.txt -o report.csv

# 动态权重优化（基于信号归因自动调权）
python3 app/dynamic_weights.py                   # 查看权重报告
python3 app/dynamic_weights.py --apply           # 应用优化权重到 config.yaml

# 数据质量校验
python3 app/validator.py
```

## 调参

编辑项目根目录的 **`config.yaml`**，无需改动 Python 源码。

## 项目结构

```
stock_system/
├── config.yaml                  # ★ 全局配置（调参改这个）
├── requirements.txt
│
├── db/                          # 数据库层
│   ├── __init__.py
│   └── schema.py                # 统一 Schema、工具函数
│
├── pipeline/                    # 数据管道
│   ├── daily_tushare.py         # 日线下载（tushare 主力）
│   ├── daily_baostock.py        # 日线下载（baostock 备用）
│   ├── financial_baostock.py    # 财务下载（baostock 主力）
│   └── financial_tushare.py     # 财务下载（tushare 备用）
│
├── strategy/                    # 策略引擎
│   ├── __init__.py
│   ├── strategy.py              # 核心：参数、配置加载、数据加载、预计算
│   └── signals.py               # 信号检测：回踩、布林扩张
│
├── backtest/                    # 回测引擎
│   ├── __init__.py
│   ├── engine.py                # 选股 + 调仓 + 净值计算
│   └── report.py                # 绩效报告 + 信号归因
│
├── app/                         # 应用入口
│   ├── backtest_runner.py       # 选股回测入口
│   ├── stock_checker.py         # 自选股诊断
│   ├── validator.py             # 数据质量校验
│   └── dynamic_weights.py       # 动态权重优化（基于归因自动调权）
│
├── scripts/                     # Shell 脚本
│   ├── update_data.sh           # 一键更新数据
│   └── run_backtest.sh          # 一键回测
│
├── tools/                       # 工具
│   ├── check_data_fields.py     # 数据源字段查看
│   ├── test_api.py              # API 连通性测试
│   └── check.sql                # SQL 查询参考
│
└── tests/                       # 测试（待扩展）
    └── __init__.py
```

## 输出文件

| 文件 | 说明 |
|------|------|
| `selected_stocks.txt` | 选股结果，可导入同花顺 |
| `selected_stocks_detail.csv` | 选股详情（含20周线、止损价），CSV格式 |
| `signal_attribution.csv` | 信号归因（各信号交易次数、收益率、胜率） |
| `.signal_cache.pkl` | 预计算缓存（数据更新后自动失效） |
| `custom_selection_report.csv` | 自选股诊断报告 |
| `weight_report.txt` | 动态权重优化报告 |

## 数据源

| 数据类型 | 主力源 | 备用源 |
|---------|--------|--------|
| 日线行情 | tushare | baostock |
| 财务数据 | baostock | tushare（需积分） |
