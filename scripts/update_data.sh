#!/bin/bash
# ============================================================
# 数据更新脚本 v2.0
# cd 到项目根目录后执行
# 1. 备份 → 2. 日线(tushare) → 3. 财务(baostock)
# ============================================================
set -euo pipefail

cd "$(dirname "$0")/.."
source venv/bin/activate

DB="stocks_2y.db"
BACKUP_DIR="backups"
MAX_BACKUPS=5

echo "===== 数据更新 pipeline v2.0 ====="

# --- 1. 备份 ---
if [ -f "$DB" ]; then
    mkdir -p "$BACKUP_DIR"
    BACKUP_FILE="$BACKUP_DIR/stocks_2y.db.backup.$(date +%Y%m%d_%H%M%S)"
    cp "$DB" "$BACKUP_FILE"
    echo "✓ 数据库已备份至 $BACKUP_FILE"

    BACKUP_COUNT=$(ls -1 "$BACKUP_DIR"/stocks_2y.db.backup.* 2>/dev/null | wc -l | tr -d ' ')
    if [ "$BACKUP_COUNT" -gt "$MAX_BACKUPS" ]; then
        ls -1t "$BACKUP_DIR"/stocks_2y.db.backup.* | tail -n +$((MAX_BACKUPS + 1)) | xargs rm -f
        echo "✓ 已清理旧备份，保留最近 $MAX_BACKUPS 个"
    fi
else
    echo "⚠ 数据库 $DB 不存在，跳过备份"
fi

# --- 2. 日线 ---
echo "===== 更新日线数据（tushare） ====="
python3 pipeline/daily_tushare.py
echo "✓ 日线更新完成"

# --- 3. 财务 ---
echo "===== 更新财务数据（baostock） ====="
python3 pipeline/financial_baostock.py
echo "✓ 财务更新完成"

echo "===== 数据更新完毕 ====="
