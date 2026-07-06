#!/usr/bin/env bash
# backup.sh — copia il DB clima dal router alla macchina locale
# Cron: 0 * * * * /home/s4lv0/Downloads/domototica/clima-router/backup.sh

ROUTER="192.168.178.72"
REMOTE_DB="/mnt/usb/clima.db"
BACKUP_DIR="/home/s4lv0/Downloads/domototica/clima-router/backup"
SSH_OPTS="-o HostKeyAlgorithms=+ssh-rsa -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10"

mkdir -p "$BACKUP_DIR"

LATEST="$BACKUP_DIR/clima.db"
TMP="$BACKUP_DIR/clima.db.tmp"

# Dump via Python (sqlite3 non disponibile su OpenWrt, Python sì)
# iterdump() usa una transazione read: sicuro anche con scritture in corso
PYDUMP="import sqlite3,sys; c=sqlite3.connect('$REMOTE_DB'); [print(l) for l in c.iterdump()]; c.close()"
if ssh $SSH_OPTS "root@${ROUTER}" "python3 -c \"$PYDUMP\"" | sqlite3 "$TMP"; then
    mv "$TMP" "$LATEST"
    # Snapshot mensile (una copia per mese)
    MONTHLY="$BACKUP_DIR/clima-$(date +%Y%m).db"
    [ -f "$MONTHLY" ] || cp "$LATEST" "$MONTHLY"
else
    rm -f "$TMP"
    exit 1
fi
