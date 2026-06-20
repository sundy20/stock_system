
-- 日线表基础信息
SELECT COUNT(*) FROM daily;
SELECT COUNT(DISTINCT code) FROM daily;
SELECT MIN(date), MAX(date) FROM daily;

-- 查看某只股票最近日线（如茅台）
SELECT * FROM daily WHERE code='sh.600519' ORDER BY date DESC LIMIT 5;
SELECT * FROM daily;

-- 财务表基础信息
SELECT COUNT(*) FROM financial;
SELECT COUNT(DISTINCT code) FROM financial;

SELECT * FROM financial;

-- 连续两期净利润>0的股票数
SELECT COUNT(*) FROM (
                         SELECT code FROM financial WHERE net_profit_yoy > 0
                         GROUP BY code HAVING COUNT(*) >= 2
                     );

-- 去年收红放量（以2025为例）
SELECT
    (MAX(CASE WHEN date >= '2025-01-01' AND date <= '2025-12-31' THEN close END) -
     MIN(CASE WHEN date >= '2025-01-01' AND date <= '2025-12-31' THEN open END)) /
    MIN(CASE WHEN date >= '2025-01-01' AND date <= '2025-12-31' THEN open END) AS red_pct
FROM daily WHERE code='sh.603799';

-- 近两季净利润
SELECT stat_date, net_profit_yoy FROM financial
WHERE code='sh.603799' ORDER BY stat_date DESC LIMIT 4;