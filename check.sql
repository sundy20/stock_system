-- 验证WAL模式
PRAGMA journal_mode;

-- 日线数据校验
SELECT COUNT(*) FROM daily;                     -- 总行数
SELECT COUNT(DISTINCT code) FROM daily;         -- 股票+指数总数
SELECT MIN(date), MAX(date) FROM daily;         -- 日期范围

-- 沪深300基准校验
SELECT COUNT(*) FROM daily WHERE code='sh.000300';

-- 股票基础信息
SELECT COUNT(*) FROM stock_basic;
SELECT code, name FROM stock_basic ORDER BY RANDOM() LIMIT 10;

-- 单只股票样例
SELECT * FROM daily WHERE code='sh.600519' ORDER BY date DESC LIMIT 5;

-- 财务数据校验
SELECT COUNT(*) FROM financial;                 -- 总行数
SELECT COUNT(DISTINCT code) FROM financial;     -- 有财务数据的股票数
SELECT MIN(stat_date), MAX(stat_date) FROM financial;

-- 查看单只股票的连续季度财务
SELECT stat_date, pub_date, net_profit_yoy
FROM financial
WHERE code='sh.600519'
ORDER BY stat_date DESC;

-- 连续两期净利润正增长的股票数量
SELECT code, COUNT(*) cnt
FROM financial
WHERE net_profit_yoy > 0
GROUP BY code
HAVING cnt >= 2
ORDER BY cnt DESC
    LIMIT 10;