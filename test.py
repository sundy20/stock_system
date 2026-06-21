import os, tushare as ts

TOKEN = os.getenv('TUSHARE_TOKEN')
if not TOKEN:
    raise RuntimeError("未检测到 TUSHARE_TOKEN 环境变量，请先设置")
ts.set_token(TOKEN)
pro = ts.pro_api()

# 测试按交易日拉取全市场日线数据（不传 ts_code）
df = pro.daily(trade_date='20260619')
if df is not None and not df.empty:
    print(f"成功：拉取到 {len(df)} 条记录")
    print(df.head())
else:
    print("失败：返回空数据或权限不足")