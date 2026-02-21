#!/usr/bin/env bash
# Notify via Telegram when the bot service starts.
# Called from ExecStartPost in claude-telegram-bot.service.
# Env vars (TELEGRAM_BOT_TOKEN, ALLOWED_USERS) injected by systemd EnvironmentFile.
set -euo pipefail

BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
# First entry in ALLOWED_USERS is the owner
CHAT_ID="${ALLOWED_USERS%%,*}"

if [[ -z "$BOT_TOKEN" || -z "$CHAT_ID" ]]; then
    echo "notify-start: BOT_TOKEN or CHAT_ID missing, skipping"
    exit 0
fi

HOSTNAME_VAL=$(hostname)
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S %Z')

MSG="🤖 *claude-telegram-bot* started on \`${HOSTNAME_VAL}\`
⏰ ${TIMESTAMP}"

curl -s -X POST \
    "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${CHAT_ID}" \
    --data-urlencode "text=${MSG}" \
    -d "parse_mode=Markdown" \
    -o /dev/null

exit 0
