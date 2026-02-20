"""Interactive setup wizard for first-time configuration.

Prompts the user for required settings and writes them to settings.toml.
Invoked automatically from main() when required fields are empty and
stdin is a TTY, or via `claude-telegram-setup` CLI entry point.
"""

from __future__ import annotations

import sys
from pathlib import Path

import tomlkit

from src.utils.constants import APP_HOME

TOML_PATH = APP_HOME / "config" / "settings.toml"


def needs_wizard(toml_path: Path = TOML_PATH) -> bool:
    """Return True if required settings appear empty in toml_path."""
    if not toml_path.exists():
        return True
    try:
        text = toml_path.read_text(encoding="utf-8")
        doc = tomlkit.parse(text)
        required = doc.get("required", {})
        token = str(required.get("telegram_bot_token", "")).strip()
        username = str(required.get("telegram_bot_username", "")).strip()
        return not token or not username
    except Exception:
        return False


def run_wizard(toml_path: Path = TOML_PATH) -> None:
    """Run the interactive setup wizard and write answers to toml_path."""
    print("\n" + "=" * 60)
    print("Claude Code Telegram Bot — First-time Setup")
    print("=" * 60)
    print("Press Enter to accept defaults shown in [brackets].\n")

    def ask(prompt: str, default: str = "") -> str:
        suffix = f" [{default}]" if default else ""
        try:
            value = input(f"{prompt}{suffix}: ").strip()
        except EOFError:
            return default
        return value or default

    def ask_yn(prompt: str, default: bool = True) -> bool:
        suffix = "[Y/n]" if default else "[y/N]"
        try:
            value = input(f"{prompt} {suffix}: ").strip().lower()
        except EOFError:
            return default
        if not value:
            return default
        return value.startswith("y")

    # Required
    token = ask("Telegram bot token (from @BotFather)")
    username = ask("Bot username (without @)")
    workspace = ask("Workspace directory", str(Path.home() / "projects"))

    # Optional
    api_key = ask("Anthropic API key (optional, press Enter to skip)")
    allowed_ids = ask("Your Telegram user ID (optional, for auth)")
    enable_mcp = ask_yn("Enable MCP (Model Context Protocol)?", default=False)
    enable_memory = ask_yn("Enable persistent semantic memory?", default=False)

    # Build TOML doc
    if toml_path.exists():
        text = toml_path.read_text(encoding="utf-8")
        doc = tomlkit.parse(text)
    else:
        from src.config.toml_template import _TEMPLATE

        doc = tomlkit.parse(_TEMPLATE)

    def _set(section: str, key: str, value: object) -> None:
        if section not in doc:
            doc.add(section, tomlkit.table())
        doc[section][key] = value  # type: ignore[index]

    _set("required", "telegram_bot_token", token)
    _set("required", "telegram_bot_username", username)
    _set("sandbox", "approved_directory", workspace)

    if api_key:
        _set("claude", "anthropic_api_key", api_key)
    if allowed_ids:
        try:
            ids = [int(x.strip()) for x in allowed_ids.split(",") if x.strip()]
            _set("required", "allowed_users", ids)
        except ValueError:
            print(f"  Warning: could not parse user IDs '{allowed_ids}', skipping.")
    if enable_mcp:
        _set("features", "enable_mcp", True)
    if enable_memory:
        _set("memory", "enable_memory", True)

    toml_path.parent.mkdir(parents=True, exist_ok=True)
    toml_path.write_text(tomlkit.dumps(doc), encoding="utf-8")

    print(f"\nConfiguration written to {toml_path}")
    print("Edit it any time, or use /settings in Telegram.\n")


def main() -> None:
    """CLI entry point: claude-telegram-setup."""
    from src.config.toml_template import ensure_toml_config

    ensure_toml_config()
    run_wizard()
    sys.exit(0)
