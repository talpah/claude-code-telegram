#!/usr/bin/env bash
# Backs up .env before each bot start so watchdog has a restore point.
# Called via ExecStartPre in bot.service.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$PROJECT_DIR/.env"
BACKUP_DIR="$PROJECT_DIR/data/config-backups"

mkdir -p "$BACKUP_DIR"

if [[ -f "$ENV_FILE" ]]; then
    TIMESTAMP=$(date +%Y%m%dT%H%M%S)
    cp "$ENV_FILE" "$BACKUP_DIR/.env.$TIMESTAMP"

    # Keep only the last 20 backups
    mapfile -t old_backups < <(ls -t "$BACKUP_DIR"/.env.* 2>/dev/null | tail -n +21)
    if [[ "${#old_backups[@]}" -gt 0 ]]; then
        rm -- "${old_backups[@]}"
    fi
fi
