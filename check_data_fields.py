#!/usr/bin/env python3
"""
数据字段查看脚本（修复版）
- 打印 baostock 日线、财务成长数据字段
- 打印 tushare 日线、财务（利润表）字段
- 用于确认数据源提供的完整字段，方便扩展本地数据库
"""

import baostock as bs
import pandas as pd
import os, sys

# ================= 配置区 =================
TEST_CODE_BS = "sh.600519"          # baostock 格式
TEST_CODE_TS = "600519.SH"          # tushare 格式（用于 pro.daily）
TEST_CODE_TS_INCOME = "600519.SH"   # tushare 利润表

# 日期范围（可根据需要修改）
START_DATE = "2023-01-01"
END_DATE   = "2024-12-31"

# 是否尝试 tushare（需要 token）
TRY_TUSHARE = True
TUSHARE_TOKEN = os.getenv("TUSHARE_TOKEN", "")


def print_baostock_daily():
    """打印 baostock 日线数据字段"""
    print("=" * 60)
    print(">> baostock 日线数据 (query_history_k_data_plus)")
    bs.login()
    try:
        rs = bs.query_history_k_data_plus(
            TEST_CODE_BS,
            "date,open,high,low,close,preclose,volume,amount,adjustflag,turn,tradestatus,pctChg,isST",
            start_date=START_DATE.replace("-", ""),
            end_date=END_DATE.replace("-", ""),
            frequency="d",
            adjustflag="2"     # 前复权
        )
        if rs is None:
            print("查询返回 None，可能是日期格式错误或参数有误，请检查。")
            return
        if rs.error_code != '0':
            print(f"错误: {rs.error_msg}")
            return
        data = []
        while rs.next():
            data.append(rs.get_row_data())
        df = pd.DataFrame(data, columns=rs.fields)
        print("字段列表:", rs.fields)
        print("样本数据 (前5行):")
        print(df.head())
    except Exception as e:
        print(f"日线查询异常: {e}")
    finally:
        bs.logout()
    print("=" * 60)


def print_baostock_finance():
    """打印 baostock 季度成长数据字段"""
    print(">> baostock 财务成长数据 (query_growth_data)")
    bs.login()
    try:
        # 查询 2024Q1
        rs = bs.query_growth_data(TEST_CODE_BS, year=2024, quarter=1)
        if rs is None:
            print("查询返回 None")
            return
        if rs.error_code != '0':
            print(f"错误: {rs.error_msg}")
            return
        data = []
        while rs.next():
            data.append(rs.get_row_data())
        if not data:
            print("无数据返回")
            return
        df = pd.DataFrame(data, columns=rs.fields)
        print("字段列表:", rs.fields)
        print("样本数据 (全部行):")
        print(df.to_string())
    except Exception as e:
        print(f"财务数据查询异常: {e}")
    finally:
        bs.logout()
    print("=" * 60)


def print_tushare_daily(token):
    """打印 tushare 日线数据字段"""
    print(">> tushare 日线数据 (pro.daily)")
    import tushare as ts
    ts.set_token(token)
    pro = ts.pro_api()
    try:
        df = pro.daily(ts_code=TEST_CODE_TS, start_date=START_DATE.replace("-", ""), end_date=END_DATE.replace("-", ""))
        if df.empty:
            print("无数据返回")
            return
        print("字段列表:", list(df.columns))
        print("样本数据 (前5行):")
        print(df.head())
    except Exception as e:
        print(f"tushare 日线获取失败: {e}")
    print("=" * 60)


def print_tushare_income(token):
    """打印 tushare 利润表字段"""
    print(">> tushare 利润表 (pro.income)")
    import tushare as ts
    ts.set_token(token)
    pro = ts.pro_api()
    try:
        df = pro.income(ts_code=TEST_CODE_TS_INCOME, period='20240331',
                        fields='ts_code,ann_date,f_ann_date,end_date,report_type,comp_type,'
                               'total_revenue,revenue,oper_cost,n_income')
        if df.empty:
            df = pro.income(ts_code=TEST_CODE_TS_INCOME, period='20231231')
        if df.empty:
            print("无数据返回")
            return
        print("字段列表:", list(df.columns))
        print("样本数据 (前3行):")
        print(df.head(3))
    except Exception as e:
        print(f"tushare 利润表获取失败: {e}")
    print("=" * 60)


def print_tushare_fina_indicator(token):
    """打印 tushare 财务指标字段"""
    print(">> tushare 财务指标 (pro.fina_indicator)")
    import tushare as ts
    ts.set_token(token)
    pro = ts.pro_api()
    try:
        df = pro.fina_indicator(ts_code=TEST_CODE_TS_INCOME, period='20240331')
        if df.empty:
            df = pro.fina_indicator(ts_code=TEST_CODE_TS_INCOME, period='20231231')
        if df.empty:
            print("无数据返回")
            return
        print("字段列表:", list(df.columns))
        print("样本数据 (前3行):")
        print(df.head(3))
    except Exception as e:
        print(f"tushare 财务指标获取失败: {e}")
    print("=" * 60)


if __name__ == "__main__":
    # baostock 测试（不需要 token）
    print_baostock_daily()
    print_baostock_finance()

    # tushare 测试（需要 token）
    if TRY_TUSHARE:
        if not TUSHARE_TOKEN:
            print("未检测到 TUSHARE_TOKEN 环境变量，跳过 tushare 测试。")
            print("如需测试，请先执行: export TUSHARE_TOKEN='你的token'")
        else:
            print("\n" + "=" * 60)
            print("tushare 测试开始，使用 token:", TUSHARE_TOKEN[:8] + "****")
            print_tushare_daily(TUSHARE_TOKEN)
            print_tushare_income(TUSHARE_TOKEN)
            print_tushare_fina_indicator(TUSHARE_TOKEN)

    print("\n测试完成，根据上方字段可判断是否需要扩展数据库表结构。")