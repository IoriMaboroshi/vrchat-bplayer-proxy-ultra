#!/bin/bash
# VRChat BPlayer Proxy - Daily Database Backup
# Saves a dated copy of the SQLite database and cleans up backups older than 7 days.

BACKUP_DIR="/opt/bilibili-proxy/data/backups"
DB_PATH="/opt/bilibili-proxy/data/bilibili_proxy.db"
DATE=$(date +%Y%m%d)

mkdir -p "$BACKUP_DIR"

# Create backup
cp "$DB_PATH" "$BACKUP_DIR/bilibili_proxy_$DATE.db"

# Remove backups older than 7 days
find "$BACKUP_DIR" -name "bilibili_proxy_*.db" -mtime +7 -delete

echo "[$(date)] Database backup completed: bilibili_proxy_$DATE.db"
