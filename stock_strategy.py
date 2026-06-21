#!/usr/bin/env python3
"""
公共选股策略模块 v4.0 （所有参数、信号函数、数据加载集中于此）
由 backtest_twice_retest.py 和 check_custom_stocks.py 共同导入。
修改策略只需编辑本文件顶部的参数区，无需改动其他脚本。
"""

import sqlite3
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

# ===================== 可调参数 =====================
# --- 流动性 ---
MIN_20D_AMOUNT = 2000          # 20日均成交额 ≥ 2000万元
MIN_120D_AMOUNT = 1500         # 120日均成交额 ≥ 1500万元

# --- 年度趋势（滚动+自然年） ---
ROLLING_DAYS = 250             # 滚动周期，近似12个月
ROLLING_PRICE_UP = 0.15        # 滚动涨幅 ≥ 15%
ROLLING_VOL_UP = 0.30          # 滚动日均成交额增长 ≥ 30%
MA_SLOPE_THRESHOLD = 0         # 年线斜率 ≥ 0（走平或向上）

# --- 月线回踩（至少1次） ---
MONTHLY_RETEST_DOWN = 0.12     # 下探幅度 ≤ 12%
MONTHLY_RETEST_NEAR = 0.08     # 靠近幅度 ≤ 8%
MONTHLY_RETEST_WINDOW = 18     # 观察窗口（根月K线）
MONTHLY_RETEST_MIN_GAP = 3     # 两次回踩最小间隔（根）
MONTHLY_RETEST_MIN_TOUCHES = 1 # 最少回踩次数

# --- 周线回踩（至少2次，即二次回踩） ---
WEEKLY_RETEST_DOWN = 0.12
WEEKLY_RETEST_NEAR = 0.08
WEEKLY_RETEST_WINDOW = 50
WEEKLY_RETEST_MIN_GAP = 5
WEEKLY_RETEST_MIN_TOUCHES = 2  # ★ 二次回踩确认

# --- 布林扩张（v3.1 双模式） ---
BB_PERIOD = 20                 # 布林带周期
BB_STD_MULT = 2                # 标准差倍数
BB_SHORT_MA = 5                # 带宽短期均线周期
BB_LONG_MA = 20                # 带宽长期均线周期
BB_SHORT_DIR_PERIOD = 2        # 标准模式连续上升期数
BB_MID_DIR_PERIOD = 3          # 中轨方向计算周期（前3期均值）
# 月线布林：不要求中轨方向，仅需股价在中轨上方
MONTHLY_BB_REQUIRE_MID_UP = False
# 周线布林：要求中轨走平或向上，同时启用双模式扩张
WEEKLY_BB_REQUIRE_MID_UP = True
WEEKLY_BB_OVERBOUGHT = None          # 超买限制已取消
WEEKLY_BB_PRE_EXPAND = True           # 启用收缩预警
WEEKLY_BB_CONTRACTION_RATIO = 0.9    # 收缩阈值（越小越容易触发）
WEEKLY_BB_USE_DUAL_MODE = True       # 双模式：标准扩张 + 收缩预警并行
WEEKLY_BB_PRICE_LIMIT = None         # 高位过滤已取消（设为None）

# --- 均线方向（回踩用） ---
MA_DIR_PERIOD = 3              # 当期均线值 > 前3期均值

# --- 财务筛选开关（★ 核心新增） ---
USE_FINANCIAL_FILTER = False   # 设为 True 则启用财务筛选，默认关闭（纯技术面选股）
# 财务阈值（仅在开关开启时生效）
FIN_CONSEC = 2                 # 连续季度数
MIN_PROFIT_YOY = 0.0           # 归母净利润同比 > 0%
MIN_PNI_YOY = 0.0              # 扣非净利润同比 > 0%（若存在，否则跳过）
MIN_NET_PROFIT = 10000000      # 单季度净利润 ≥ 1000万元（若有数据）

# --- 回测交易参数 ---
COMMISSION = 0.0001            # 佣金（万1）
SLIPPAGE = 0.001               # 滑点
STAMP_DUTY = 0.001             # 卖出印花税
REBALANCE_FREQ = 'W'           # 调仓频率：W=周, M=月

# --- 数据库与基准 ---
DB_PATH = 'stocks_2y.db'       # 数据库文件路径
BENCH_CODE = 'sh.000300'       # 基准指数（沪深300）


# ===================== 数据加载函数 =====================
def load_all_data(conn):
    """加载2018年至今的日线数据，返回 multi-index (code, date) 的 DataFrame"""
    query = """SELECT code, date, open, high, low, close, volume, amount, pct_chg
               FROM daily WHERE date >= '2018-01-01' ORDER BY code, date"""
    df = pd.read_sql(query, conn, parse_dates=['date'])
    return df.set_index(['code', 'date']).sort_index()


def load_financial_data(conn):
    """加载财务数据，计算生效日期（pub_date + 10天）"""
    query = """SELECT code, stat_date, pub_date, net_profit_yoy, revenue_yoy, yoy_pni, net_profit
               FROM financial WHERE net_profit_yoy IS NOT NULL ORDER BY code, stat_date"""
    df = pd.read_sql(query, conn, parse_dates=['stat_date', 'pub_date'])
    # 发布日 +10 天生效，避免未来函数
    df['effective_date'] = df['pub_date'].fillna(df['stat_date'] + timedelta(days=30)) + timedelta(days=10)
    return df


# ===================== 前置剔除 =====================
def get_valid_codes(conn, df_daily, target_date):
    """
    返回符合前置剔除条件的股票代码列表：
    - 上市 ≥ 24 个月
    - 近20个交易日停牌天数 ≤ 2天
    """
    basic = pd.read_sql("SELECT code, list_date FROM stock_basic", conn, parse_dates=['list_date'])
    basic['months'] = ((target_date - basic['list_date']).dt.days / 30.44)
    valid_listed = basic[basic['months'] >= 24]['code'].tolist()

    df_recent = df_daily.loc[df_daily.index.get_level_values('date') >= target_date - timedelta(days=40)]
    trading_days = df_recent.groupby(level='code').size()
    valid_trading = trading_days[trading_days >= 18].index.tolist()

    return list(set(valid_listed) & set(valid_trading))


# ===================== 年度趋势 =====================
def check_annual_trend(code, df_stocks, target_date, yearly_data):
    """
    检查年线趋势（滚动、自然年收红放量、自然年收红 任一满足即可）
    - 滚动：最近250交易日涨幅≥15%且日均成交额增长≥30%，收盘价≥250日均线，斜率≥0
    - 自然年收红放量：上一个完整自然年收红且放量（已冗余，被收红条件覆盖）
    - 自然年收红（新增）：上一个完整自然年收红，不要求放量
    """
    code_data = df_stocks.loc[code].sort_index()
    rolling_ok = False

    # 1. 滚动12个月验证
    if len(code_data) >= ROLLING_DAYS * 2:
        recent = code_data.iloc[-ROLLING_DAYS:]
        prev = code_data.iloc[-2*ROLLING_DAYS:-ROLLING_DAYS]
        price_up = (recent['close'].iloc[-1] - prev['close'].iloc[-1]) / prev['close'].iloc[-1] >= ROLLING_PRICE_UP
        vol_ratio = recent['amount'].mean() / prev['amount'].mean() - 1
        vol_up = vol_ratio >= ROLLING_VOL_UP
        ma250 = code_data['close'].rolling(250).mean()
        above_ma = code_data['close'].iloc[-1] >= ma250.iloc[-1]
        slope = (ma250.iloc[-1] - ma250.iloc[-20]) / 20 if len(ma250) >= 20 else -1
        slope_ok = slope >= MA_SLOPE_THRESHOLD
        rolling_ok = price_up and vol_up and above_ma and slope_ok

    # 2. 自然年验证（收红即通过，不要求放量）
    natural_ok = False
    if code in yearly_data.index.get_level_values('code'):
        df_y = yearly_data.loc[code].sort_index()
        years = df_y.index.tolist()
        last_year = target_date.year - 1
        if last_year in years:
            row = df_y.loc[last_year]
            red = row['last_close'] > row['first_open']
            natural_ok = red  # ← 不再要求放量
        else:
            # 没有去年数据（次新股），要求上市以来上涨
            first_open = df_y.iloc[0]['first_open']
            last_close = df_y.iloc[-1]['last_close']
            natural_ok = last_close > first_open

    return rolling_ok or natural_ok


# ===================== 回踩检测（含间隔计数） =====================
def detect_retest_with_gap(price_df, ma_period, tolerance_down, tolerance_near,
                           window, min_gap, min_touches=1, require_ma_up=True):
    """
    均线回踩信号检测（支持最少回踩次数和间隔计数）
    参数：
        min_touches: 最少回踩次数（月线1次，周线2次）
        require_ma_up: 是否要求均线方向向上
    返回布尔Series
    """
    close = price_df['close']
    low   = price_df['low']
    ma    = close.rolling(ma_period).mean()

    # 均线方向：当期 > 前3期均值（避免单期毛刺）
    if require_ma_up:
        ma_up = ma > ma.shift(1).rolling(MA_DIR_PERIOD).mean()
    else:
        ma_up = pd.Series(True, index=ma.index)

    # 有效回踩事件：下探 或 靠近
    touch_down = (low < ma) & ((ma - low) / ma <= tolerance_down)
    touch_near = (low >= ma) & ((low - ma) / ma <= tolerance_near)
    touch = (touch_down | touch_near) & ma_up

    # 间隔计数：连续满足仅计1次，两次事件间隔至少 min_gap 根K线
    touch_int = touch.astype(int)
    event_start = (touch_int.diff() == 1) | (touch_int == 1)  # 事件开端
    event_count = event_start.rolling(window, min_periods=1).sum()
    has_event = event_count >= min_touches

    # 当前收盘价必须站上均线
    close_ok = close >= ma
    return has_event & close_ok


# ===================== 布林扩张检测（双模式） =====================
def detect_bb_expand(price_df, period=20, std_mult=2, short_ma=5, long_ma=20,
                     require_mid_up=True, mid_dir_period=3, short_dir_period=2,
                     overbought_limit=None, pre_expand=False, contraction_ratio=0.9,
                     use_dual_mode=False, price_limit=None):
    """
    布林带扩张检测（v3.1 优化版）
    参数：
        require_mid_up: 是否要求中轨方向向上（或走平）
        pre_expand: 是否启用收缩预警模式
        contraction_ratio: 收缩阈值，短期带宽 < 长期带宽 × 该值
        use_dual_mode: 是否使用双模式（标准+收缩预警并行）
        price_limit: 高位过滤，收盘价 ≤ 慢速均线 × 该倍数（默认不限制）
    """
    close = price_df['close']
    mid   = close.rolling(period).mean()
    std   = close.rolling(period).std()
    upper = mid + std_mult * std
    lower = mid - std_mult * std
    bandwidth = (upper - lower) / mid          # 标准化带宽
    bw_short  = bandwidth.rolling(short_ma).mean()
    bw_long   = bandwidth.rolling(long_ma).mean()

    # 股价在中轨上方（基本要求）
    above_mid = close > mid

    # 中轨方向判断
    if require_mid_up:
        mid_up = mid >= mid.shift(1).rolling(mid_dir_period).mean()
        base_cond = above_mid & mid_up
    else:
        base_cond = above_mid

    # 标准扩张模式：带宽上穿 + 连续上升
    expanding_standard = (bw_short > bw_long) & (bw_short.diff(short_dir_period) > 0)

    # 收缩预警模式（仅当 pre_expand=True 时生效）
    if pre_expand and use_dual_mode:
        # 双模式：标准扩张 或 收缩预警扩张
        contracted = bw_short < bw_long * contraction_ratio
        expanding_contraction = contracted & (bw_short.diff(1) > 0)
        expanding = expanding_standard | expanding_contraction
    elif pre_expand:
        # 仅收缩预警
        expanding = bw_short < bw_long * contraction_ratio
        expanding = expanding & (bw_short.diff(1) > 0)
    else:
        expanding = expanding_standard

    cond = base_cond & expanding

    # 高位过滤（若设置）
    if price_limit is not None:
        ma_slow = close.rolling(period).mean()
        cond = cond & (close <= ma_slow * price_limit)

    # 超买限制（已取消）
    if overbought_limit is not None:
        cond = cond & (close <= upper * overbought_limit)
    return cond


# ===================== 财务筛选辅助 =====================
def apply_financial_filter(fin_codes_base, df_fin, target_date):
    """
    根据财务数据过滤股票代码列表，返回通过财务条件的代码列表
    若关闭财务开关，则直接返回原始列表
    """
    if not USE_FINANCIAL_FILTER:
        return fin_codes_base

    fin_before = df_fin[df_fin['effective_date'] <= target_date]
    if fin_before.empty:
        return []

    fin_latest = fin_before.sort_values('effective_date').groupby('code').tail(FIN_CONSEC)
    fin_pass = fin_latest.groupby('code').filter(
        lambda x: (len(x) == FIN_CONSEC) and
                  all(x['net_profit_yoy'] > MIN_PROFIT_YOY) and
                  all((x['yoy_pni'].isna()) | (x['yoy_pni'] > MIN_PNI_YOY)) and
                  all((x['net_profit'].isna()) | (x['net_profit'] >= MIN_NET_PROFIT))
    )
    return fin_pass['code'].unique().tolist()