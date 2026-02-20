#!/usr/bin/env bash
# Monitors the bot service and reverts .env if a config change caused a failure.
#
# State machine:
#   - Bot running for HEALTHY_SECS → current .env saved as "last-good"
#   - Bot enters failed state AND .env differs from last-good → revert + restart
#   - Revert loop guard: won't revert again within REVERT_COOLDOWN seconds
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BOT_SERVICE="claude-telegram-bot"
ENV_FILE="$PROJECT_DIR/.env"
LAST_GOOD="$PROJECT_DIR/data/config-backups/.env.last-good"
FAILED_DIR="$PROJECT_DIR/data/config-backups/failed"

CHECK_INTERVAL=10   # seconds between health checks
HEALTHY_SECS=30     # uptime required before config is marked stable
REVERT_COOLDOWN=60  # minimum seconds between successive reverts

mkdir -p "$FAILED_DIR"

log() {
    local msg="[$(date -Is)] $*"
    echo "$msg"
    logger -t "claude-watchdog" -- "$*" 2>/dev/null || true
}

sc() { systemctl --user "$@"; }

bot_up_since=0
last_revert=0

log "Watchdog started (service=$BOT_SERVICE, healthy_secs=$HEALTHY_SECS)"

while true; do
    sleep "$CHECK_INTERVAL"
    now=$(date +%s)

    if sc is-active --quiet "$BOT_SERVICE"; then
        # ── Bot is running ──────────────────────────────────────────────────
        if [[ "$bot_up_since" -eq 0 ]]; then
            bot_up_since="$now"
            log "Bot came up — waiting ${HEALTHY_SECS}s to confirm config is stable"
        fi

        uptime=$(( now - bot_up_since ))

        if (( uptime >= HEALTHY_SECS )); then
            if [[ -f "$ENV_FILE" ]] && ! cmp -s "$ENV_FILE" "$LAST_GOOD" 2>/dev/null; then
                cp "$ENV_FILE" "$LAST_GOOD"
                log "Config stable after ${uptime}s — saved as last-good"
            fi
        fi

    else
        # ── Bot is not active ───────────────────────────────────────────────
        bot_up_since=0

        state=$(sc show -p ActiveState --value "$BOT_SERVICE" 2>/dev/null || echo "unknown")
        [[ "$state" == "failed" ]] || continue

        # Revert loop guard
        if (( now - last_revert < REVERT_COOLDOWN )); then
            log "Bot failed (state=$state) but revert cooldown active — skipping"
            continue
        fi

        # Need a last-good config to revert to
        if [[ ! -f "$LAST_GOOD" ]]; then
            log "Bot failed but no last-good config saved yet — manual intervention needed"
            continue
        fi

        # Only revert if the current config actually differs
        if cmp -s "$ENV_FILE" "$LAST_GOOD" 2>/dev/null; then
            log "Bot failed but config matches last-good — not a config regression"
            continue
        fi

        # ── Revert ──────────────────────────────────────────────────────────
        failed_snap="$FAILED_DIR/.env.$(date +%Y%m%dT%H%M%S)"
        log "Config regression detected — reverting (bad config saved to $failed_snap)"
        cp "$ENV_FILE" "$failed_snap"
        cp "$LAST_GOOD" "$ENV_FILE"
        last_revert="$now"

        sleep 2
        if sc restart "$BOT_SERVICE"; then
            log "Bot restarted with last-good config"
        else
            log "ERROR: failed to restart bot after config revert"
        fi
    fi
done
