"""Pytest configuration and fixtures."""

import pytest


@pytest.fixture(autouse=True)
def _no_real_toml(monkeypatch, tmp_path):
    """Prevent tests from reading ~/.claude-code-telegram/config/settings.toml."""
    monkeypatch.setattr("src.config.toml_source.TOML_PATH", tmp_path / "settings.toml")


@pytest.fixture
def sample_user_id():
    """Sample Telegram user ID for testing."""
    return 123456789


@pytest.fixture
def sample_config():
    """Sample configuration for testing."""
    return {
        "telegram_bot_token": "test_token",
        "telegram_bot_username": "test_bot",
        "approved_directory": "/tmp/test_projects",
        "allowed_users": [123456789],
    }
