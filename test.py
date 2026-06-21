import baostock as bs
import time, random

bs.login()
code = "sh.600000"   # 随便一只股票
year, quarter = 2025, 1
time.sleep(random.uniform(0.2, 1.0))
rs = bs.query_growth_data(code, year=year, quarter=quarter)
print("error_code:", rs.error_code)
print("error_msg:", rs.error_msg)
if rs.error_code == '0':
    while rs.next():
        print(rs.get_row_data())
bs.logout()