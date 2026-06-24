#!/usr/bin/env python3
"""
公共选股策略模块 v4.6

由 app/backtest_runner.py 和 app/stock_checker.py 共同导入。
修改策略参数：编辑项目根目录的 config.yaml。

v4.4 改进：
  - 信号标签体系重构：月回踩信号回归可见，Tier2 拼装补全四信号
  - 过滤单独周回踩/月回踩（不产生独立标签）

v4.3 改进：
  - 全信号共振从五重交集改为四重+月布林要求，扩大样本量
  - 新增动态权重优化模块
"""

import warnings
warnings.filterwarnings('ignore', message=".*'M' is deprecated.*")
warnings.filterwarnings('ignore', message=".*'W' is deprecated.*")

import sqlite3
import pandas as pd
import numpy as np
import logging
import time
import os
import hashlib
import json
import pickle
from datetime import datetime, timedelta

from db import schema as db_schema
from .signals import detect_retest_with_gap, detect_bb_expand, detect_squeeze_breakout

# ===================== 日志配置 =====================
logger = logging.getLogger("strategy")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter(
        '%(asctime)s [%(levelname)-5s] %(message)s', datefmt='%H:%M:%S'))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)

# ===================== 可调参数（默认值，可被 config.yaml 覆盖） =====================

# -- 流动性（万元） --
MIN_20D_AMOUNT = 2000
MIN_120D_AMOUNT = 1500

# -- 年线趋势 --
ROLLING_DAYS = 250             # 滚动周期≈12个月
ROLLING_PRICE_UP = 0.15        # 滚动涨幅 ≥ 15%
ROLLING_VOL_UP = 0.30          # 滚动量增 ≥ 30%
MA_SLOPE_THRESHOLD = 0         # 年线斜率 ≥ 0

# -- 月线回踩（1次即可） --
MONTHLY_RETEST_DOWN = 0.12
MONTHLY_RETEST_NEAR = 0.08
MONTHLY_RETEST_WINDOW = 18
MONTHLY_RETEST_MIN_GAP = 3
MONTHLY_RETEST_MIN_TOUCHES = 1

# -- 周线回踩（二次确认，必须2次） --
WEEKLY_RETEST_DOWN = 0.12
WEEKLY_RETEST_NEAR = 0.08
WEEKLY_RETEST_WINDOW = 50
WEEKLY_RETEST_MIN_GAP = 5
WEEKLY_RETEST_MIN_TOUCHES = 2

# -- 布林带：趋势延续（Trend Continuation） --
BB_TC_ENABLED = True
BB_PERIOD = 20
BB_STD_MULT = 2
BB_SHORT_MA = 5                # 带宽短期MA（检测扩张加速）
BB_LONG_MA = 20                # 带宽长期MA（基线）
BB_SHORT_DIR_PERIOD = 2        # 连续扩张期数
BB_MID_DIR_PERIOD = 3          # 中轨方向判定周期
MONTHLY_BB_REQUIRE_MID_UP = False   # 月布林不要求中轨向上
WEEKLY_BB_REQUIRE_MID_UP = True     # 周布林要求中轨走平向上

# -- 布林带：挤压爆发（Squeeze Breakout，v4.6 新增） --
BB_SQ_ENABLED = True
BB_SQ_REQUIRE_MID_UP = False        # 不等均线拐头，爆发即确认
BB_SQ_CONTRACTION_PCT = 10          # 带宽历史分位阈值（%）
BB_SQ_CONTRACTION_LOOKBACK = 50     # 回溯 K 线根数
BB_SQ_EXPANSION_CONFIRM = 2         # 带宽连续回升确认期数

MA_DIR_PERIOD = 3              # 均线方向判定周期

# -- 财务筛选 --
USE_FINANCIAL_FILTER = True
FIN_CONSEC = 2                 # 连续季度数
MIN_PROFIT_YOY = 0.0           # 归母净利同比 > 0%
MIN_PNI_YOY = 0.0              # 扣非净利同比 > 0%
MIN_NET_PROFIT = 10000000      # 单季度净利 ≥ 1000万
PROFIT_ACCELERATION = True     # 盈利加速度：最新季度增速 ≥ 前一季度

# -- 交易成本 --
COMMISSION = 0.0001            # 佣金万1
SLIPPAGE = 0.001               # 滑点千1
STAMP_DUTY = 0.001             # 印花税千1（卖出）
REBALANCE_FREQ = 'M'           # 调仓频率：M=月 W=周

# -- 数据 --
DB_PATH = 'stocks_2y.db'
BENCH_CODE = 'sh.000300'
DB_MAX_RETRIES = 3
DB_RETRY_DELAY = 1.0
CACHE_ENABLED = True
CACHE_FILE = '.signal_cache.pkl'

# ===================== 加载配置文件 =====================

def _load_config():
    """尝试加载 config.yaml 覆盖默认值"""
    # config.yaml 在项目根目录
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    config_path = os.path.join(root, 'config.yaml')
    if not os.path.exists(config_path):
        return

    try:
        import yaml
    except ImportError:
        logger.debug("pyyaml 未安装，使用默认参数")
        return

    try:
        with open(config_path, 'r') as f:
            cfg = yaml.safe_load(f)
    except Exception as e:
        logger.warning("无法加载 config.yaml: %s，使用默认参数", e)
        return

    if cfg is None:
        return

    # --- 数据库 ---
    db_cfg = cfg.get('database', {})
    if db_cfg.get('path'):
        globals()['DB_PATH'] = db_cfg['path']

    # --- 策略参数 ---
    stg = cfg.get('strategy', {})
    if stg:
        if stg.get('benchmark'):
            globals()['BENCH_CODE'] = stg['benchmark']

        liq = stg.get('liquidity', {})
        for k, g in [('min_20d_amount', 'MIN_20D_AMOUNT'), ('min_120d_amount', 'MIN_120D_AMOUNT')]:
            if k in liq: globals()[g] = liq[k]

        at = stg.get('annual_trend', {})
        for k, g in [('rolling_days', 'ROLLING_DAYS'), ('rolling_price_up', 'ROLLING_PRICE_UP'),
                     ('rolling_vol_up', 'ROLLING_VOL_UP'), ('ma_slope_threshold', 'MA_SLOPE_THRESHOLD')]:
            if k in at: globals()[g] = at[k]

        mr = stg.get('monthly_retest', {})
        for k, g in [('down_tolerance', 'MONTHLY_RETEST_DOWN'), ('near_tolerance', 'MONTHLY_RETEST_NEAR'),
                     ('window', 'MONTHLY_RETEST_WINDOW'), ('min_gap', 'MONTHLY_RETEST_MIN_GAP'),
                     ('min_touches', 'MONTHLY_RETEST_MIN_TOUCHES')]:
            if k in mr: globals()[g] = mr[k]

        wr = stg.get('weekly_retest', {})
        for k, g in [('down_tolerance', 'WEEKLY_RETEST_DOWN'), ('near_tolerance', 'WEEKLY_RETEST_NEAR'),
                     ('window', 'WEEKLY_RETEST_WINDOW'), ('min_gap', 'WEEKLY_RETEST_MIN_GAP'),
                     ('min_touches', 'WEEKLY_RETEST_MIN_TOUCHES')]:
            if k in wr: globals()[g] = wr[k]

        bb = stg.get('bollinger', {})
        for k, g in [('period', 'BB_PERIOD'), ('std_mult', 'BB_STD_MULT')]:
            if k in bb: globals()[g] = bb[k]

        tc = bb.get('trend_continuation', {})
        for k, g in [('enabled', 'BB_TC_ENABLED'),
                     ('short_ma', 'BB_SHORT_MA'), ('long_ma', 'BB_LONG_MA'),
                     ('consecutive_periods', 'BB_SHORT_DIR_PERIOD'),
                     ('mid_dir_period', 'BB_MID_DIR_PERIOD'),
                     ('require_mid_up', 'WEEKLY_BB_REQUIRE_MID_UP'),
                     ('monthly_require_mid_up', 'MONTHLY_BB_REQUIRE_MID_UP')]:
            if k in tc: globals()[g] = tc[k]

        sq = bb.get('squeeze_breakout', {})
        for k, g in [('enabled', 'BB_SQ_ENABLED'),
                     ('require_mid_up', 'BB_SQ_REQUIRE_MID_UP'),
                     ('contraction_percentile', 'BB_SQ_CONTRACTION_PCT'),
                     ('contraction_lookback', 'BB_SQ_CONTRACTION_LOOKBACK'),
                     ('expansion_confirm', 'BB_SQ_EXPANSION_CONFIRM')]:
            if k in sq: globals()[g] = sq[k]

        fin = stg.get('financial_filter', {})
        for k, g in [('enabled', 'USE_FINANCIAL_FILTER'), ('consec_quarters', 'FIN_CONSEC'),
                     ('min_profit_yoy', 'MIN_PROFIT_YOY'), ('min_pni_yoy', 'MIN_PNI_YOY'),
                     ('min_net_profit', 'MIN_NET_PROFIT'),
                     ('profit_acceleration', 'PROFIT_ACCELERATION')]:
            if k in fin: globals()[g] = fin[k]

    bt = cfg.get('backtest', {})
    for k, g in [('commission', 'COMMISSION'), ('slippage', 'SLIPPAGE'),
                 ('stamp_duty', 'STAMP_DUTY'), ('rebalance_freq', 'REBALANCE_FREQ')]:
        if k in bt: globals()[g] = bt[k]

    cache = cfg.get('cache', {})
    if 'enabled' in cache: globals()['CACHE_ENABLED'] = cache['enabled']
    if 'file' in cache: globals()['CACHE_FILE'] = cache['file']

    logger.info("已加载 config.yaml")


_load_config()

# 同步 MA_DIR_PERIOD 到 signals 模块
import strategy.signals as _sig
_sig.MA_DIR_PERIOD = MA_DIR_PERIOD

# ===================== Numba 加速（可选） =====================
try:
    from numba import jit, prange
    NUMBA_AVAILABLE = True
except ImportError:
    NUMBA_AVAILABLE = False
    def jit(f): return f


# ===================== 数据库连接 =====================

def get_db_connection(db_path=None, max_retries=None):
    """委托给 db.schema.get_db_connection"""
    path = db_path or DB_PATH
    retries = max_retries if max_retries is not None else DB_MAX_RETRIES
    return db_schema.get_db_connection(path, retries)


# ===================== 数据加载 =====================

def load_all_data(conn):
    """加载2018年至今的日线数据，返回 multi-index (code, date) 的 DataFrame"""
    logger.info("加载日线数据...")
    query = """SELECT code, date, open, high, low, close, volume, amount, pct_chg
               FROM daily WHERE date >= '2018-01-01' ORDER BY code, date"""
    df = pd.read_sql(query, conn, parse_dates=['date'])
    result = df.set_index(['code', 'date']).sort_index()
    logger.info("日线数据加载完成: %s 条, %s 只股票",
                len(df), result.index.get_level_values('code').nunique())
    return result


def load_financial_data(conn):
    """加载财务数据，计算生效日期（pub_date + 10天）"""
    logger.info("加载财务数据...")
    query = """SELECT code, stat_date, pub_date, net_profit_yoy, revenue_yoy,
                      yoy_pni, net_profit, roe_avg, gp_margin
               FROM financial WHERE net_profit_yoy IS NOT NULL ORDER BY code, stat_date"""
    df = pd.read_sql(query, conn, parse_dates=['stat_date', 'pub_date'])
    if df.empty:
        logger.warning("财务数据为空，财务筛选将不生效")
        df['effective_date'] = pd.NaT
        return df
    df['effective_date'] = df['pub_date'].fillna(
        df['stat_date'] + timedelta(days=30)) + timedelta(days=10)
    logger.info("财务数据加载完成: %s 条记录", len(df))
    return df


def load_basic_info(conn):
    """加载 stock_basic 表，供 get_valid_codes 复用"""
    logger.info("加载基础信息...")
    basic = pd.read_sql("SELECT code, list_date FROM stock_basic",
                        conn, parse_dates=['list_date'])
    logger.info("基础信息: %s 只股票", len(basic))
    return basic


# ===================== 数据校验 =====================

def validate_data(df_daily, df_fin=None, min_trading_days=120):
    """启动前数据完整性校验。返回 (pass: bool, messages: list[str])"""
    messages = []
    if df_daily is None or df_daily.empty:
        return False, ["日线数据为空"]

    dates = df_daily.index.get_level_values('date')
    n_dates = dates.nunique()
    n_codes = df_daily.index.get_level_values('code').nunique()
    date_range = f"{dates.min().strftime('%Y-%m-%d')} ~ {dates.max().strftime('%Y-%m-%d')}"
    messages.append(f"日线数据: {n_codes} 只股票, {n_dates} 个交易日, 范围 {date_range}")

    ok = True
    if n_dates < min_trading_days:
        ok = False
        messages.append(f"交易日不足 {min_trading_days} 天（实际 {n_dates} 天）")
    if n_codes < 100:
        ok = False
        messages.append(f"股票数量过少（{n_codes} 只），建议检查数据库")
    if dates.max() < pd.Timestamp.now() - timedelta(days=7):
        messages.append(f"最新数据日期 {dates.max().strftime('%Y-%m-%d')}，距今超过7天")
    if 'close' not in df_daily.columns:
        ok = False
        messages.append("日线数据缺少 close 列")
    if BENCH_CODE not in df_daily.index.get_level_values('code'):
        messages.append(f"基准指数 {BENCH_CODE} 不在数据中")

    if df_fin is not None:
        if df_fin.empty:
            messages.append("财务数据为空，财务筛选将不生效")
        else:
            messages.append(f"财务数据: {len(df_fin)} 条记录")
    return ok, messages


# ===================== 前置剔除 =====================

def get_valid_codes(df_daily, target_date, basic_df=None, conn=None):
    """返回符合前置剔除条件的股票代码列表（上市≥24月 + 近期停牌≤2天）"""
    if basic_df is not None:
        basic = basic_df.copy()
    elif conn is not None:
        basic = pd.read_sql("SELECT code, list_date FROM stock_basic", conn, parse_dates=['list_date'])
    else:
        raise ValueError("get_valid_codes: 必须提供 basic_df 或 conn")

    basic['months'] = ((target_date - basic['list_date']).dt.days / 30.44)
    valid_listed = basic[basic['months'] >= 24]['code'].tolist()

    df_recent = df_daily.loc[df_daily.index.get_level_values('date') >= target_date - timedelta(days=40)]
    trading_days = df_recent.groupby(level='code').size()
    valid_trading = trading_days[trading_days >= 18].index.tolist()

    return list(set(valid_listed) & set(valid_trading))


# ===================== 财务筛选 =====================

def apply_financial_filter(fin_codes_base, df_fin, target_date):
    """
    财务筛选：连续FIN_CONSEC季度满足净利同比>0、扣非>0、净利≥阈值、
    ROE>0、毛利率>0。启用PROFIT_ACCELERATION时额外要求最新季度增速≥前一季度。
    关闭USE_FINANCIAL_FILTER时直接返回原列表。
    """
    if not USE_FINANCIAL_FILTER:
        return fin_codes_base

    fin_before = df_fin[df_fin['effective_date'] <= target_date]
    if fin_before.empty:
        logger.debug("财务筛选: target_date=%s 前无有效财务数据", target_date)
        return []

    fin_latest = fin_before.sort_values('effective_date').groupby('code').tail(FIN_CONSEC)

    def _filter_func(x):
        if len(x) != FIN_CONSEC:
            return False
        if not all(x['net_profit_yoy'] > MIN_PROFIT_YOY):
            return False
        if not all((x['yoy_pni'].isna()) | (x['yoy_pni'] > MIN_PNI_YOY)):
            return False
        if not all((x['net_profit'].isna()) | (x['net_profit'] >= MIN_NET_PROFIT)):
            return False
        if not all((x['roe_avg'].isna()) | (x['roe_avg'] > 0)):
            return False
        if not all((x['gp_margin'].isna()) | (x['gp_margin'] > 0)):
            return False
        # ★ 盈利加速度：最新季度增速 ≥ 前一季度
        if PROFIT_ACCELERATION and FIN_CONSEC >= 2:
            profits = x.sort_values('stat_date')['net_profit_yoy'].dropna().values
            if len(profits) >= 2 and profits[-1] < profits[-2]:
                return False
        return True

    fin_pass = fin_latest.groupby('code').filter(_filter_func)
    return fin_pass['code'].unique().tolist()


# ===================== 快速查询 =====================

def get_latest_value(series, target_date):
    """获取 Series 在 target_date 或之前的最新值（asof 二分查找，O(log n)）"""
    if series is None or series.empty:
        return None
    target_ts = pd.Timestamp(target_date)
    if target_ts < series.index[0]:
        return None
    val = series.asof(target_ts)
    if pd.isna(val):
        return None
    return val


def check_annual_trend_fast(code, cache_entry, yearly, target_date):
    """
    年线趋势检查（无未来函数）。
    通过条件（OR）：rolling_ok（12个月滚动）或 natural_ok（自然年收红）
    natural_ok 作为 rolling_ok 的兜底：当股票上市不足500天无法计算滚动时生效
    """
    target_ts = pd.Timestamp(target_date)

    # A. 滚动12个月：价涨量增+站上年线+斜率≥0
    rolling_ok = False
    rolling_series = cache_entry.get('rolling_ok')
    if rolling_series is not None and not rolling_series.empty:
        val = get_latest_value(rolling_series, target_ts)
        if val is not None:
            rolling_ok = bool(val)

    # B. 自然年收红兜底（上市较晚的股票）
    natural_ok = False
    last_year = target_ts.year - 1
    natural_years = cache_entry.get('natural_years', {})
    if last_year in natural_years:
        natural_ok = natural_years[last_year]
    elif code in yearly.index.get_level_values('code'):
        df_y = yearly.loc[code].sort_index()
        available_years = [y for y in df_y.index if y <= last_year]
        if available_years:
            natural_ok = df_y.loc[available_years[-1]]['last_close'] > df_y.loc[available_years[0]]['first_open']

    return rolling_ok or natural_ok


# ===================== 趋势强度评分 =====================

def compute_trend_strength(code, df_daily, target_date):
    """
    计算趋势强度评分（0-120），用于同信号类型内的排序。
    只依赖 target_date 之前的数据，无未来函数。

    五个维度：
      1. 年线乖离率  (0-40分)：最优区间3%~20%，站上过远扣分
      2. 均线多头排列 (0-40分)：MA20>MA60>MA120>MA250 逐级给分
      3. 近20日涨幅  (0-20分)：正值加分，负值零分
      4. Squeeze预警  (0-10分)：带宽越接近历史低分位，弹性势能越大
      5. 月线回踩确认 (0-10分)：回踩≥2次额外加分，二次确认更可靠
    """
    try:
        df = df_daily.loc[code].sort_index()
    except KeyError:
        return 0.0

    target_ts = pd.Timestamp(target_date)
    df = df[df.index <= target_ts]
    if len(df) < 250:
        return 0.0

    close = df['close']
    last_close = close.iloc[-1]

    # 均线
    ma20 = close.rolling(20).mean().iloc[-1]
    ma60 = close.rolling(60).mean().iloc[-1]
    ma120 = close.rolling(120).mean().iloc[-1]
    ma250 = close.rolling(250).mean().iloc[-1]

    if pd.isna(ma250) or ma250 <= 0:
        return 0.0

    # 1. 年线乖离率 (0-40分)：黄金区间 3%~20%
    deviation = (last_close / ma250 - 1) * 100
    if 3 <= deviation <= 20:
        dev_score = 40.0
    elif 0 <= deviation < 3:
        dev_score = (deviation / 3) * 40  # 刚站上年线，线性给分
    elif deviation > 20:
        dev_score = max(0, 40 - (deviation - 20) * 2)  # 乖离过大扣分
    else:
        dev_score = 0

    # 2. 均线多头排列 (0-40分)：三层对齐各13.3分
    align_score = 0.0
    if not pd.isna(ma20) and not pd.isna(ma60) and ma20 > ma60:
        align_score += 13.3
    if not pd.isna(ma60) and not pd.isna(ma120) and ma60 > ma120:
        align_score += 13.3
    if not pd.isna(ma120) and not pd.isna(ma250) and ma120 > ma250:
        align_score += 13.3

    # 3. 近20日涨幅 (0-20分)
    ret_score = 0.0
    if len(close) >= 20:
        ret_20d = (close.iloc[-1] / close.iloc[-20] - 1) * 100
        if ret_20d > 0:
            ret_score = min(20, ret_20d * 1.33)  # 0~15%涨幅映射到0~20分

    # 4. Squeeze 预警 (0-10分)：日线带宽处于历史低分位说明周线可能在 squeeze 中
    sq_score = 0.0
    if len(close) >= 250:
        roll_mid = close.rolling(20).mean()
        roll_std = close.rolling(20).std()
        bw_daily = (roll_mid + 2 * roll_std - (roll_mid - 2 * roll_std)) / roll_mid
        bw_pct = bw_daily.rolling(250, min_periods=100).rank(pct=True).iloc[-1]
        if not pd.isna(bw_pct) and bw_pct <= 0.30:
            sq_score = round((0.30 - bw_pct) / 0.30 * 10, 1)

    # 5. 月线二次回踩加分 (0-10分)：回踩≥2次加满分，1次加5分
    retest_bonus = 0.0
    if len(close) >= 500:
        df_m = df[['close', 'low']].resample('M').agg({'close': 'last', 'low': 'min'}).dropna()
        if len(df_m) >= 18:
            ma_m = df_m['close'].rolling(20).mean()
            ma_up_m = ma_m > ma_m.shift(1).rolling(MA_DIR_PERIOD).mean()
            touch_down = (df_m['low'] < ma_m) & ((ma_m - df_m['low']) / ma_m <= MONTHLY_RETEST_DOWN)
            touch_near = (df_m['low'] >= ma_m) & ((df_m['low'] - ma_m) / ma_m <= MONTHLY_RETEST_NEAR)
            touch = (touch_down | touch_near) & ma_up_m
            touch_int = touch.astype(int)
            starts = (touch_int.diff() == 1)
            if len(touch_int) > 0 and touch_int.iloc[0] == 1:
                starts.iloc[0] = True
            if starts.any():
                pos = np.where(starts.values)[0]
                filtered = np.zeros(len(starts), dtype=bool)
                gap = MONTHLY_RETEST_MIN_GAP
                last = -gap - 1
                for p in pos:
                    if p - last >= gap:
                        filtered[p] = True
                        last = p
                starts = pd.Series(filtered, index=starts.index)
            touch_count = starts.rolling(MONTHLY_RETEST_WINDOW, min_periods=1).sum().iloc[-1]
            if touch_count >= 2:
                retest_bonus = 10.0
            elif touch_count >= 1:
                retest_bonus = 5.0

    return round(dev_score + align_score + ret_score + sq_score + retest_bonus, 1)


# ===================== 预计算 =====================

def _get_cache_key():
    """生成缓存键：数据库修改时间 + 所有策略参数的 MD5"""
    try:
        db_mtime = os.path.getmtime(DB_PATH)
        params = {
            'min_20d': MIN_20D_AMOUNT, 'min_120d': MIN_120D_AMOUNT,
            'rolling_days': ROLLING_DAYS, 'rolling_price_up': ROLLING_PRICE_UP,
            'rolling_vol_up': ROLLING_VOL_UP, 'ma_slope': MA_SLOPE_THRESHOLD,
            'mr_down': MONTHLY_RETEST_DOWN, 'mr_near': MONTHLY_RETEST_NEAR,
            'mr_window': MONTHLY_RETEST_WINDOW, 'mr_gap': MONTHLY_RETEST_MIN_GAP,
            'mr_touches': MONTHLY_RETEST_MIN_TOUCHES,
            'wr_down': WEEKLY_RETEST_DOWN, 'wr_near': WEEKLY_RETEST_NEAR,
            'wr_window': WEEKLY_RETEST_WINDOW, 'wr_gap': WEEKLY_RETEST_MIN_GAP,
            'wr_touches': WEEKLY_RETEST_MIN_TOUCHES,
            'bb_period': BB_PERIOD, 'bb_std': BB_STD_MULT,
            'bb_short': BB_SHORT_MA, 'bb_long': BB_LONG_MA,
            'bb_short_dir': BB_SHORT_DIR_PERIOD, 'bb_mid_dir': BB_MID_DIR_PERIOD,
            'bb_tc_enabled': BB_TC_ENABLED,
            'mb_mid_up': MONTHLY_BB_REQUIRE_MID_UP, 'wb_mid_up': WEEKLY_BB_REQUIRE_MID_UP,
            'sq_enabled': BB_SQ_ENABLED, 'sq_mid_up': BB_SQ_REQUIRE_MID_UP,
            'sq_pct': BB_SQ_CONTRACTION_PCT, 'sq_lookback': BB_SQ_CONTRACTION_LOOKBACK,
            'sq_confirm': BB_SQ_EXPANSION_CONFIRM,
        }
        param_str = json.dumps(params, sort_keys=True)
        param_hash = hashlib.md5(param_str.encode()).hexdigest()
        return f"{db_mtime:.6f}_{param_hash}"
    except Exception:
        return None


def _load_cache(cache_key):
    """从磁盘加载预计算缓存"""
    try:
        if not os.path.exists(CACHE_FILE):
            return None
        with open(CACHE_FILE, 'rb') as f:
            data = pickle.load(f)
        if data.get('key') == cache_key:
            return data
        logger.debug("缓存键不匹配，重新计算")
        return None
    except Exception as e:
        logger.debug("缓存加载失败: %s，重新计算", e)
        return None


def _save_cache(signal_cache, yearly):
    """保存预计算结果到磁盘"""
    try:
        cache_key = _get_cache_key()
        if cache_key is None:
            return
        with open(CACHE_FILE, 'wb') as f:
            pickle.dump({'key': cache_key, 'signal_cache': signal_cache, 'yearly': yearly}, f)
        size_mb = os.path.getsize(CACHE_FILE) / (1024 * 1024)
        logger.info("预计算结果已缓存至 %s (%.1f MB)", CACHE_FILE, size_mb)
    except Exception as e:
        logger.warning("缓存保存失败: %s", e)


def precompute_all_signals_once(df_daily):
    """
    一次性预计算所有股票的指标和信号，支持磁盘缓存。

    每个信号分月/周两个周期独立检测：
      - rolling_ok:      年线250日趋势（价涨量增+站年线+斜率）
      - natural_years:   自然年收红记录
      - amount_ma20/120: 20/120日均成交额
      - m_retest:        月线均线回踩（min_touches=1）
      - w_retest:        周线均线二次回踩（min_touches=2）
      - m_bb:            月线布林趋势延续（不要求中轨方向）
      - w_bb:            周线布林趋势延续（要求中轨走平向上）
      - w_bb_sq:         周线布林挤压爆发（不等均线确认）

    返回 (signal_cache, yearly)
    """
    t0 = time.time()

    # 磁盘缓存
    if CACHE_ENABLED:
        cache_key = _get_cache_key()
        if cache_key:
            cached = _load_cache(cache_key)
            if cached is not None:
                logger.info("命中预计算缓存（%s），跳过重算", CACHE_FILE)
                return cached['signal_cache'], cached['yearly']

    logger.info("一次性预计算所有指标和信号...")

    # 年度聚合
    df_daily['year'] = df_daily.index.get_level_values('date').year
    yearly = df_daily.groupby(['code', 'year']).agg(
        first_open=('open', 'first'), last_close=('close', 'last'), total_volume=('volume', 'sum')
    ).sort_index()
    logger.info("  年度聚合完成 (%s 条)", len(yearly))

    # 逐股预计算
    all_codes = df_daily.index.get_level_values('code').unique()
    total = len(all_codes)
    signal_cache = {}
    failed_codes = []

    for i, code in enumerate(all_codes):
        if code == BENCH_CODE:
            continue
        if (i + 1) % 500 == 0 or i == total - 1:
            elapsed = time.time() - t0
            eta = (elapsed / (i + 1)) * (total - i - 1) if i > 0 else 0
            logger.info("  信号进度: %s/%s (%.0f%%) 耗时 %.0fs ETA %.0fs",
                       i + 1, total, (i+1)/total*100, elapsed, eta)

        try:
            df_code = df_daily.loc[code].sort_index()
            if len(df_code) < 120:
                continue

            # 年线滚动指标
            rolling_ok = pd.Series(False, index=df_code.index, dtype=bool)
            if len(df_code) >= ROLLING_DAYS * 2:
                ma250 = df_code['close'].rolling(ROLLING_DAYS).mean()
                price_change = (df_code['close'] - df_code['close'].shift(ROLLING_DAYS)) / df_code['close'].shift(ROLLING_DAYS)
                price_up = price_change >= ROLLING_PRICE_UP
                recent_vol = df_code['amount'].rolling(ROLLING_DAYS).mean()
                prev_vol = recent_vol.shift(ROLLING_DAYS)
                vol_up = (recent_vol / prev_vol - 1) >= ROLLING_VOL_UP
                above_ma = df_code['close'] >= ma250
                slope = (ma250 - ma250.shift(20)) / 20
                slope_ok = slope >= MA_SLOPE_THRESHOLD
                rolling_ok = price_up & vol_up & above_ma & slope_ok

            # 自然年收红
            natural_years = {}
            if code in yearly.index.get_level_values('code'):
                df_y = yearly.loc[code]
                for y in df_y.index:
                    natural_years[y] = bool(df_y.loc[y]['last_close'] > df_y.loc[y]['first_open'])

            # 流动性
            amount_wan = df_code['amount'] / 10000
            amount_ma20 = amount_wan.rolling(20).mean()
            amount_ma120 = amount_wan.rolling(120).mean()

            # 周/月 resample + 信号
            df_w = df_code[['close', 'low']].resample('W').agg({'close': 'last', 'low': 'min'}).dropna()
            df_m = df_code[['close', 'low']].resample('M').agg({'close': 'last', 'low': 'min'}).dropna()

            w_retest = m_retest = w_bb = w_bb_sq = m_bb = None
            if len(df_w) >= 20 and len(df_m) >= 18:
                w_retest = detect_retest_with_gap(
                    df_w, 20, WEEKLY_RETEST_DOWN, WEEKLY_RETEST_NEAR,
                    WEEKLY_RETEST_WINDOW, WEEKLY_RETEST_MIN_GAP,
                    WEEKLY_RETEST_MIN_TOUCHES, True)
                m_retest = detect_retest_with_gap(
                    df_m, 20, MONTHLY_RETEST_DOWN, MONTHLY_RETEST_NEAR,
                    MONTHLY_RETEST_WINDOW, MONTHLY_RETEST_MIN_GAP,
                    MONTHLY_RETEST_MIN_TOUCHES, True)
                if BB_TC_ENABLED:
                    w_bb = detect_bb_expand(
                        df_w, BB_PERIOD, BB_STD_MULT, BB_SHORT_MA, BB_LONG_MA,
                        require_mid_up=WEEKLY_BB_REQUIRE_MID_UP,
                        short_dir_period=BB_SHORT_DIR_PERIOD,
                        overbought_limit=None,
                        pre_expand=False,
                        use_dual_mode=False)
                    m_bb = detect_bb_expand(
                        df_m, BB_PERIOD, BB_STD_MULT, BB_SHORT_MA, BB_LONG_MA,
                        require_mid_up=MONTHLY_BB_REQUIRE_MID_UP,
                        short_dir_period=BB_SHORT_DIR_PERIOD,
                        overbought_limit=None,
                        pre_expand=False,
                        use_dual_mode=False)
                if BB_SQ_ENABLED:
                    w_bb_sq = detect_squeeze_breakout(
                        df_w, BB_PERIOD, BB_STD_MULT,
                        BB_SQ_CONTRACTION_PCT, BB_SQ_CONTRACTION_LOOKBACK,
                        BB_SQ_EXPANSION_CONFIRM, BB_SQ_REQUIRE_MID_UP)

            signal_cache[code] = {
                'rolling_ok': rolling_ok,
                'natural_years': natural_years,
                'amount_ma20': amount_ma20,
                'amount_ma120': amount_ma120,
                'w_retest': w_retest,
                'm_retest': m_retest,
                'w_bb': w_bb,
                'w_bb_sq': w_bb_sq,
                'm_bb': m_bb,
            }
        except Exception as e:
            logger.warning("  计算 %s 时出错，跳过: %s", code, e)
            failed_codes.append((code, str(e)))
            continue

    elapsed = time.time() - t0
    logger.info("预计算完成: %s 只成功, %s 只失败, 耗时 %.1fs",
                len(signal_cache), len(failed_codes), elapsed)
    if failed_codes:
        logger.warning("失败股票列表 (前10): %s", [(c, e[:60]) for c, e in failed_codes[:10]])

    if CACHE_ENABLED:
        _save_cache(signal_cache, yearly)

    return signal_cache, yearly
