"""
策略模块 — 公共选股策略引擎

用法:
    import strategy as st
    st.precompute_all_signals_once(df_daily)

所有函数和常量从 strategy.py 和 signals.py 重新导出。
"""

# 策略常量与函数
from .strategy import (
    # 参数
    MIN_20D_AMOUNT, MIN_120D_AMOUNT,
    ROLLING_DAYS, ROLLING_PRICE_UP, ROLLING_VOL_UP, MA_SLOPE_THRESHOLD,
    MONTHLY_RETEST_DOWN, MONTHLY_RETEST_NEAR, MONTHLY_RETEST_WINDOW,
    MONTHLY_RETEST_MIN_GAP, MONTHLY_RETEST_MIN_TOUCHES,
    WEEKLY_RETEST_DOWN, WEEKLY_RETEST_NEAR, WEEKLY_RETEST_WINDOW,
    WEEKLY_RETEST_MIN_GAP, WEEKLY_RETEST_MIN_TOUCHES,
    BB_PERIOD, BB_STD_MULT, BB_SHORT_MA, BB_LONG_MA,
    BB_SHORT_DIR_PERIOD, BB_MID_DIR_PERIOD,
    MONTHLY_BB_REQUIRE_MID_UP, WEEKLY_BB_REQUIRE_MID_UP,
    BB_TC_ENABLED, BB_SQ_ENABLED, BB_SQ_REQUIRE_MID_UP,
    BB_SQ_CONTRACTION_PCT, BB_SQ_CONTRACTION_LOOKBACK, BB_SQ_EXPANSION_CONFIRM,
    MA_DIR_PERIOD,
    USE_FINANCIAL_FILTER, FIN_CONSEC, MIN_PROFIT_YOY, MIN_PNI_YOY, MIN_NET_PROFIT,
    PROFIT_ACCELERATION,
    COMMISSION, SLIPPAGE, STAMP_DUTY, REBALANCE_FREQ,
    DB_PATH, BENCH_CODE,
    # 函数
    get_db_connection,
    load_all_data, load_financial_data, load_basic_info,
    validate_data,
    get_valid_codes, apply_financial_filter,
    get_latest_value, check_annual_trend_fast,
    compute_trend_strength,
    precompute_all_signals_once,
)

# 信号检测函数
from .signals import (
    detect_retest_with_gap,
    detect_bb_expand,
    detect_squeeze_breakout,
)
