.PHONY: install dev test lint format clean help run \
        install-service uninstall-service start stop restart status logs watchdog-logs monitor-logs all-logs

# Default target
SYSTEMD_USER_DIR=$(HOME)/.config/systemd/user
DEPLOY_DIR=$(CURDIR)/deploy

help:
	@echo "Dev:"
	@echo "  install          Install production dependencies"
	@echo "  dev              Install development dependencies"
	@echo "  test             Run tests"
	@echo "  lint             Run linting checks"
	@echo "  format           Format code"
	@echo "  clean            Clean up generated files"
	@echo "  run              Run the bot (foreground)"
	@echo ""
	@echo "Service (systemd --user):"
	@echo "  install-service  Install + enable bot and watchdog services"
	@echo "  uninstall-service Remove services"
	@echo "  start            Start both services"
	@echo "  stop             Stop both services"
	@echo "  restart          Restart bot (watchdog stays up)"
	@echo "  status           Show service status"
	@echo "  logs             Tail bot logs"
	@echo "  watchdog-logs    Tail watchdog logs"
	@echo "  monitor-logs     Tail log monitor logs"
	@echo "  all-logs         Tail all three logs interleaved"

install:
	uv sync --no-group dev

dev:
	uv sync
	uv run pre-commit install --install-hooks || echo "pre-commit not configured yet"

test:
	uv run pytest

lint:
	uv run ruff check src tests
	uv run ruff format --check src tests
	uv run ty check src

format:
	uv run ruff format src tests
	uv run ruff check --fix src tests

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	rm -rf .coverage htmlcov/ .pytest_cache/ dist/ build/

run:
	uv run claude-telegram-bot

# For debugging
run-debug:
	uv run claude-telegram-bot --debug

# ── Systemd service management ────────────────────────────────────────────────

install-service:
	@echo "Installing systemd user services..."
	mkdir -p $(SYSTEMD_USER_DIR)
	mkdir -p $(CURDIR)/data/config-backups/failed
	sed "s|/home/cosmin/Projects/claude-code-telegram|$(CURDIR)|g; s|/home/cosmin/.local/bin/uv|$$(which uv)|g" \
		$(DEPLOY_DIR)/bot.service > $(SYSTEMD_USER_DIR)/claude-telegram-bot.service
	sed "s|/home/cosmin/Projects/claude-code-telegram|$(CURDIR)|g" \
		$(DEPLOY_DIR)/watchdog.service > $(SYSTEMD_USER_DIR)/claude-telegram-watchdog.service
	sed "s|/home/cosmin/Projects/claude-code-telegram|$(CURDIR)|g; s|/home/cosmin/.local/bin/uv|$$(which uv)|g" \
		$(DEPLOY_DIR)/log-monitor.service > $(SYSTEMD_USER_DIR)/claude-log-monitor.service
	systemctl --user daemon-reload
	systemctl --user enable claude-telegram-bot claude-telegram-watchdog claude-log-monitor
	@echo "Done. Run 'make start' to launch, or 'loginctl enable-linger $$USER' for boot persistence."

uninstall-service:
	systemctl --user disable --now claude-telegram-bot claude-telegram-watchdog claude-log-monitor 2>/dev/null || true
	rm -f $(SYSTEMD_USER_DIR)/claude-telegram-bot.service
	rm -f $(SYSTEMD_USER_DIR)/claude-telegram-watchdog.service
	rm -f $(SYSTEMD_USER_DIR)/claude-log-monitor.service
	systemctl --user daemon-reload
	@echo "Services removed."

start:
	systemctl --user start claude-telegram-bot claude-telegram-watchdog claude-log-monitor

stop:
	systemctl --user stop claude-telegram-bot claude-telegram-watchdog claude-log-monitor

restart:
	systemctl --user restart claude-telegram-bot

status:
	systemctl --user status claude-telegram-bot claude-telegram-watchdog claude-log-monitor

logs:
	journalctl --user -fu claude-telegram-bot

watchdog-logs:
	journalctl --user -fu claude-telegram-watchdog

monitor-logs:
	journalctl --user -fu claude-log-monitor

all-logs:
	journalctl --user -fu claude-telegram-bot -u claude-telegram-watchdog -u claude-log-monitor
