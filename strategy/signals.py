"""
信号检测函数（纯算法，无状态依赖）

从 strategy 模块拆分出来的独立信号检测逻辑：
  - detect_retest_with_gap — 均线回踩信号（含间隔计数）
  - detect_bb_expand       — 布林带扩张信号（双模式）
"""

import pandas as pd
import numpy as np

# 引用 strategy 模块的常量（在导入时由 strategy.__init__.py 保证已定义）
MA_DIR_PERIOD = 3  # 默认值，会被 strategy 模块覆盖


def detect_retest_with_gap(price_df, ma_period, tolerance_down, tolerance_near,
                           window, min_gap, min_touches=1, require_ma_up=True):
    """
    均线回踩信号检测。

    回踩定义：价格在MA附近（下探≤tolerance_down 或 靠近≤tolerance_near）
               且均线方向向上，且当前收盘价站上均线。
    min_gap 保证两次回踩之间有足够间隔，避免密集毛刺。
    min_touches：月线1次即可，周线需要2次（二次回踩确认）。

    返回：布尔Series，True表示当天触发信号。
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

    # 标记事件开端（0→1 跳变）
    touch_int = touch.astype(int)
    event_start = (touch_int.diff() == 1)
    if len(touch_int) > 0 and touch_int.iloc[0] == 1:
        event_start.iloc[0] = True

    # ★ v4.1 修复：应用 min_gap 过滤
    if min_gap > 1 and event_start.any():
        positions = np.where(event_start.values)[0]
        filtered = np.zeros(len(event_start), dtype=bool)
        last_kept = -min_gap - 1
        for pos in positions:
            if pos - last_kept >= min_gap:
                filtered[pos] = True
                last_kept = pos
        event_start_filtered = pd.Series(filtered, index=event_start.index)
    else:
        event_start_filtered = event_start

    # 滚动窗口内事件数量 ≥ min_touches
    event_count = event_start_filtered.rolling(window, min_periods=1).sum()
    has_event = event_count >= min_touches

    # 当前收盘价必须站上均线
    close_ok = close >= ma
    return has_event & close_ok


def detect_bb_expand(price_df, period=20, std_mult=2, short_ma=5, long_ma=20,
                     require_mid_up=True, mid_dir_period=3, short_dir_period=2,
                     overbought_limit=None, pre_expand=False, contraction_ratio=0.9,
                     use_dual_mode=False, price_limit=None):
    """
    布林带扩张信号检测。

    两种模式（use_dual_mode=True 时二者取或）：
      A. 标准扩张：短期带宽均值 > 长期带宽均值，且短期带宽连续上升
      B. 收缩预警：带宽收缩到基线以下后开始反弹（pre_expand=True 启用）

    月线布林：require_mid_up=False（周期长，容忍中轨短暂下行）
    周线布林：require_mid_up=True + 双模式（标准扩张+收缩预警并行）
    """
    close = price_df['close']
    mid   = close.rolling(period).mean()
    std   = close.rolling(period).std()
    upper = mid + std_mult * std
    lower = mid - std_mult * std
    bandwidth = (upper - lower) / mid          # 标准化带宽
    bw_short  = bandwidth.rolling(short_ma).mean()
    bw_long   = bandwidth.rolling(long_ma).mean()

    above_mid = close > mid

    if require_mid_up:
        mid_up = mid >= mid.shift(1).rolling(mid_dir_period).mean()
        base_cond = above_mid & mid_up
    else:
        base_cond = above_mid

    # 标准扩张模式
    expanding_standard = (bw_short > bw_long) & (bw_short.diff(short_dir_period) > 0)

    if pre_expand and use_dual_mode:
        contracted = bw_short < bw_long * contraction_ratio
        expanding_contraction = contracted & (bw_short.diff(1) > 0)
        expanding = expanding_standard | expanding_contraction
    elif pre_expand:
        expanding = bw_short < bw_long * contraction_ratio
        expanding = expanding & (bw_short.diff(1) > 0)
    else:
        expanding = expanding_standard

    cond = base_cond & expanding

    if price_limit is not None:
        ma_slow = close.rolling(period).mean()
        cond = cond & (close <= ma_slow * price_limit)

    if overbought_limit is not None:
        cond = cond & (close <= upper * overbought_limit)
    return cond
