import baostock as bs
bs.login()
rs = bs.query_growth_data("sh.600519", year=2025, quarter=4)
print("error_code:", rs.error_code)
print("fields:", rs.fields)
rows = []
while rs.next():
    rows.append(rs.get_row_data())
print("rows:", rows[:2])   # 只打印前两行
bs.logout()

import sqlite3
print("SQLite 版本:", sqlite3.sqlite_version)