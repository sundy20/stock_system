#!/usr/bin/env python3
"""
统一数据库 Schema 与工具模块

所有下载脚本和策略模块共用此文件定义的 schema，
避免多个脚本各自建表导致的表结构漂移。

提供:
  - 三张表的 DDL（daily / stock_basic / financial）
  - init_all_tables(conn)      — 一键建表 + 索引
  - init_db_pragmas(conn)       — 性能优化 pragma
  - safe_batch_write(conn, df_list, table, columns) — 临时表批量写入
  - checkpoint_db(conn)         — WAL checkpoint 防止文件膨胀
  - get_db_connection(db_path)  — 带重试的连接
"""

import sqlite3
import time
import logging
import pandas as pd

logger = logging.getLogger("db.schema")

# ===================== DDL =====================

DAILY_DDL = """
CREATE TABLE IF NOT EXISTS daily (
    code     TEXT,
    date     TEXT,
    name     TEXT,
    open     REAL,
    high     REAL,
    low      REAL,
    close    REAL,
    volume   REAL,
    amount   REAL,
    pct_chg  REAL,
    turn     REAL,
    pre_close REAL,
    PRIMARY KEY (code, date)
)
"""

DAILY_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_daily_code_date ON daily(code, date)
"""

STOCK_BASIC_DDL = """
CREATE TABLE IF NOT EXISTS stock_basic (
    code      TEXT PRIMARY KEY,
    name      TEXT,
    industry  TEXT,
    list_date TEXT
)
"""

FINANCIAL_DDL = """
CREATE TABLE IF NOT EXISTS financial (
    code              TEXT,
    name              TEXT,
    pub_date          TEXT,
    stat_date         TEXT,
    net_profit_yoy    REAL,
    revenue_yoy       REAL,
    yoy_equity        REAL,
    yoy_asset         REAL,
    yoy_eps           REAL,
    yoy_pni           REAL,
    net_profit        REAL,
    roe_avg           REAL,
    gp_margin         REAL,
    express_gryoy     REAL,
    express_opyoy     REAL,
    express_pub_date  TEXT,
    express_stat_date TEXT,
    PRIMARY KEY (code, stat_date)
)
"""

# ===================== 列定义（供 safe_batch_write 使用） =====================

DAILY_COLUMNS = [
    'code', 'date', 'name',
    'open', 'high', 'low', 'close',
    'volume', 'amount', 'pct_chg', 'turn', 'pre_close'
]

FINANCIAL_COLUMNS = [
    'code', 'name', 'pub_date', 'stat_date',
    'net_profit_yoy', 'revenue_yoy',
    'yoy_equity', 'yoy_asset', 'yoy_eps', 'yoy_pni',
    'net_profit', 'roe_avg', 'gp_margin',
    'express_gryoy', 'express_opyoy',
    'express_pub_date', 'express_stat_date'
]

# ===================== 初始化 =====================

def init_db_pragmas(conn):
    """设置 SQLite 性能优化 pragma"""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size = -20000")   # 20MB cache
    conn.execute("PRAGMA busy_timeout=5000")     # 5s 忙等待


def init_all_tables(conn):
    """建表 + 索引（幂等，IF NOT EXISTS）"""
    conn.execute(DAILY_DDL)
    conn.execute(DAILY_INDEX_DDL)
    conn.execute(STOCK_BASIC_DDL)
    conn.execute(FINANCIAL_DDL)
    conn.commit()
    logger.debug("数据库表结构已确认")


# ===================== 批量写入 =====================

def safe_batch_write(conn, df_list, table_name, columns):
    """
    使用临时表 + INSERT OR REPLACE 批量写入，避免主键冲突。
    自动补齐缺失列（填 None）。
    """
    if not df_list:
        return
    all_df = pd.concat(df_list, ignore_index=True)
    for col in columns:
        if col not in all_df.columns:
            all_df[col] = None
    all_df = all_df[columns]

    temp_table = f"{table_name}_temp"
    all_df.to_sql(temp_table, conn, if_exists='replace', index=False)

    col_str   = ', '.join(columns)
    placeholder = ', '.join(columns)
    conn.execute(f"""
        INSERT OR REPLACE INTO {table_name} ({col_str})
        SELECT {placeholder} FROM {temp_table}
    """)
    conn.execute(f"DROP TABLE IF EXISTS {temp_table}")
    conn.commit()


# ===================== 维护 =====================

def checkpoint_db(conn):
    """执行 WAL checkpoint，防止 WAL 文件无限膨胀"""
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    logger.debug("WAL checkpoint 完成")


# ===================== 连接 =====================

def get_db_connection(db_path, max_retries=3):
    """
    带重试的数据库连接。设置 WAL 模式和忙等待超时。
    """
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            conn = sqlite3.connect(db_path)
            init_db_pragmas(conn)
            return conn
        except sqlite3.Error as e:
            last_error = e
            logger.warning("数据库连接失败 (attempt %s/%s): %s", attempt, max_retries, e)
            if attempt < max_retries:
                time.sleep(1.0 * attempt)
    raise sqlite3.Error(f"数据库连接失败（已重试{max_retries}次）: {last_error}")
