
-- 日线表基础信息
SELECT COUNT(*) FROM daily;
SELECT COUNT(DISTINCT code) FROM daily;
SELECT MIN(date), MAX(date) FROM daily;

-- 查看某只股票最近日线（如茅台）
SELECT * FROM daily WHERE code='sh.600519' ORDER BY date DESC LIMIT 5;

-- 财务表基础信息
SELECT COUNT(*) FROM financial;
SELECT COUNT(DISTINCT code) FROM financial;

-- 某只股票最近几期净利润增长率
SELECT code, stat_date, pub_date, net_profit_yoy, revenue_yoy
FROM financial WHERE code='sh.600519' ORDER BY stat_date DESC LIMIT 5;

-- 连续两期净利润>0的股票数
SELECT COUNT(*) FROM (
                         SELECT code FROM financial WHERE net_profit_yoy > 0
                         GROUP BY code HAVING COUNT(*) >= 2
                     );


-- 日线表：增加成交额、涨跌幅、换手率、前收盘价
ALTER TABLE daily ADD COLUMN amount REAL DEFAULT 0;
ALTER TABLE daily ADD COLUMN pct_chg REAL DEFAULT 0;
ALTER TABLE daily ADD COLUMN turn REAL DEFAULT 0;
ALTER TABLE daily ADD COLUMN pre_close REAL DEFAULT 0;

-- 财务表：增加净资产增长率、总资产增长率、每股收益增长率、扣非净利润增长率
ALTER TABLE financial ADD COLUMN yoy_equity REAL;
ALTER TABLE financial ADD COLUMN yoy_asset REAL;
ALTER TABLE financial ADD COLUMN yoy_eps REAL;
ALTER TABLE financial ADD COLUMN yoy_pni REAL;
EOF