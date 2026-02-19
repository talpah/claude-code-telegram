.PHONY: install dev test lint format clean help run

# Default target
help:
	@echo "Available commands:"
	@echo "  install    - Install production dependencies"
	@echo "  dev        - Install development dependencies"
	@echo "  test       - Run tests"
	@echo "  lint       - Run linting checks"
	@echo "  format     - Format code"
	@echo "  clean      - Clean up generated files"
	@echo "  run        - Run the bot"

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
