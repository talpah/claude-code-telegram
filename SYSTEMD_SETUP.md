# Systemd Service Setup

The project ships with a `deploy/` directory containing systemd user services for the bot and a watchdog that automatically reverts bad config.

## Architecture

```
claude-telegram-bot.service      — main bot process
claude-telegram-watchdog.service — monitors bot; reverts .env on config-induced failure
claude-log-monitor.service       — tails logs, classifies errors, sends Telegram alerts
```

**Watchdog behaviour:**
1. On each bot start, `deploy/backup-config.sh` snapshots `.env` to `data/config-backups/`
2. Once the bot has been running for 30 s, the current `.env` is saved as `last-good`
3. If the bot enters `failed` state and `.env` differs from `last-good`, the bad config is archived to `data/config-backups/failed/` and the last-good config is restored
4. Bot is restarted; a 60 s cooldown prevents revert loops

## Quick Setup

```bash
# 1. Install and enable both services
make install-service

# 2. Start them
make start

# 3. (Optional) Keep services running after logout
loginctl enable-linger $USER
```

`make install-service` rewrites the hardcoded paths in `deploy/*.service` to match your checkout location and installs them into `~/.config/systemd/user/`.

## Common Commands

| Command | Description |
|---------|-------------|
| `make start` | Start bot + watchdog + log monitor |
| `make stop` | Stop all three services |
| `make restart` | Restart bot (watchdog + monitor stay up) |
| `make status` | Show status of all three services |
| `make logs` | Tail bot logs |
| `make watchdog-logs` | Tail watchdog logs |
| `make monitor-logs` | Tail log monitor logs |

Raw systemctl equivalents:

```bash
systemctl --user start   claude-telegram-bot claude-telegram-watchdog
systemctl --user stop    claude-telegram-bot claude-telegram-watchdog
systemctl --user restart claude-telegram-bot
systemctl --user status  claude-telegram-bot claude-telegram-watchdog
journalctl --user -fu    claude-telegram-bot
journalctl --user -fu    claude-telegram-watchdog
```

## Config Change & Rollback

The watchdog tracks the last config known to be stable. To roll back manually:

```bash
# See available backups
ls -lt data/config-backups/

# Restore a specific backup
cp data/config-backups/.env.20260220T143000 .env
make restart
```

Failed configs (the ones that caused the bot to crash) are saved separately:

```bash
ls data/config-backups/failed/
```

Keep only the last 20 backups automatically (managed by `deploy/backup-config.sh`).

## Updating the Service Files

If you change `deploy/bot.service` or `deploy/watchdog.service`, reinstall:

```bash
make install-service
make restart
```

## Troubleshooting

**Service won't start:**
```bash
journalctl --user -u claude-telegram-bot -n 100
systemctl --user cat claude-telegram-bot   # verify installed paths
uv run claude-telegram-bot                 # test manually
```

**Service stops after logout:**
```bash
loginctl enable-linger $USER
```

**Watchdog not reverting:**
- Check `make watchdog-logs` — it logs every decision
- The revert only triggers if the bot fails *and* `.env` differs from `last-good`
- If the bot has never been up for 30 s, `last-good` hasn't been written yet

## Files

| Path | Purpose |
|------|---------|
| `deploy/bot.service` | Bot service template |
| `deploy/watchdog.service` | Watchdog service template |
| `deploy/log-monitor.service` | Log monitor service template |
| `deploy/backup-config.sh` | Runs at ExecStartPre — snapshots .env |
| `deploy/watchdog.sh` | Watchdog loop script |
| `deploy/log_monitor.py` | Log monitor script |
| `data/config-backups/` | Rolling .env backups (last 20) |
| `data/config-backups/failed/` | Configs that caused bot failures |
| `data/config-backups/.env.last-good` | Last config confirmed stable (≥30 s uptime) |
| `~/.claude-code-telegram/monitor_state.json` | Log monitor dedup state |
| `~/.claude-code-telegram/errors_YYYY-MM-DD.txt` | Daily error log |
| `~/.config/systemd/user/` | Installed service files |

## Log Monitor

`claude-log-monitor.service` tails journald from both `claude-telegram-bot` and `claude-telegram-watchdog`, watching for WARNING-level and above entries.

**Per new error pattern:**
1. Normalizes the message (strips IDs, timestamps, numbers, paths) and hashes it
2. Calls Claude Haiku to classify: `CRITICAL`, `TODO`, or `IGNORE`
3. Appends to `~/.claude-code-telegram/errors_YYYY-MM-DD.txt`
4. Sends an immediate Telegram message to the primary user

**Every 2 hours** (even during quiet periods):
- Sends a summary: new pattern count + top 5 recurring errors
- Only sent if there were errors in the window

**Deduplication:** same normalized pattern = one entry, count incremented, no repeat notification.

**If the bot is down:** notifications go directly to `api.telegram.org` using `BOT_TOKEN` from `.env`, so alerts still arrive.
