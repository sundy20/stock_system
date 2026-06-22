
-- 日线表基础信息
SELECT COUNT(*) FROM daily;
SELECT COUNT(DISTINCT code) FROM daily;
SELECT MIN(date), MAX(date) FROM daily;

SELECT code, date, close, amount FROM daily WHERE code='sh.600519' ORDER BY date DESC LIMIT 5;

SELECT code, name, industry, list_date FROM stock_basic LIMIT 10;

-- 查看某只股票最近日线（如茅台）
SELECT * FROM daily WHERE code='sh.000300' ORDER BY date DESC LIMIT 5;
SELECT * FROM daily;

-- 财务表基础信息
SELECT COUNT(*) FROM financial;
SELECT COUNT(DISTINCT code) FROM financial;

SELECT * FROM financial WHERE code='sh.600519' ORDER BY stat_date DESC LIMIT 5;

SELECT * FROM financial where express_gryoy is not null ;

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

DELETE FROM stock_basic WHERE code IN ('sz.001331','sh.600228','sh.600717','sh.603137','sh.603159','sh.603721');

SELECT code, stat_date, net_profit_yoy, net_profit, yoy_pni, express_gryoy FROM financial WHERE code='sh.600519' ORDER BY stat_date DESC LIMIT 4;

