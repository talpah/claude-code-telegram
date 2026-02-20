#!/usr/bin/env bash
# Backs up .env before each bot start so watchdog has a restore point.
# Called via ExecStartPre in bot.service.
#
# Searches for .env in priority order:
#   1. ~/.claude-code-telegram/config/.env  (consolidated home)
#   2. <project-root>/.env                  (legacy)
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_HOME="${HOME}/.claude-code-telegram"
BACKUP_DIR="${APP_HOME}/backups"

# Resolve which .env to back up
if [[ -f "${APP_HOME}/config/.env" ]]; then
    ENV_FILE="${APP_HOME}/config/.env"
else
    ENV_FILE="$PROJECT_DIR/.env"
fi

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
