"""Interactive /settings UI — declarative field registry + inline keyboard builders.

Callback data scheme (prefix ``set:``):

    set:menu                        Show category grid
    set:cat:<cat_key>               Show fields in category
    set:toggle:<field>              Flip boolean field
    set:choose:<field>              Show choice picker
    set:val:<field>:<value>         Apply chosen value
    set:inc:<field>                 Increment int/float by step (clamped)
    set:dec:<field>                 Decrement int/float by step (clamped)
    set:noop                        Non-interactive display button (no-op)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import tomlkit
from dotenv import set_key
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from ..config.settings import Settings
from ..config.toml_source import FIELD_TO_SECTION
from ..utils.constants import APP_HOME

# ── Type aliases ──────────────────────────────────────────────────────────────

_FieldDef = dict[str, Any]
_CategoryDef = dict[str, Any]

# ── Model choices for the picker (alias → full model ID) ─────────────────────

MODEL_CHOICES: dict[str, str] = {
    "opus": "claude-opus-4-6",
    "sonnet": "claude-sonnet-4-6",
    "sonnet45": "claude-sonnet-4-5",
    "haiku": "claude-haiku-4-5",
    "opus3": "claude-3-opus-20240229",
    "sonnet3": "claude-3-5-sonnet-20241022",
    "haiku3": "claude-3-5-haiku-20241022",
}

# ── Declarative settings registry ─────────────────────────────────────────────

SETTINGS_CATEGORIES: dict[str, _CategoryDef] = {
    "claude": {
        "label": "🤖 Claude",
        "fields": {
            "claude_model": {
                "label": "Model",
                "type": "choice",
                "choices": MODEL_CHOICES,
                "env_key": "CLAUDE_MODEL",
            },
            "claude_max_turns": {
                "label": "Max turns",
                "type": "int",
                "min": 1,
                "max": 50,
                "step": 5,
                "env_key": "CLAUDE_MAX_TURNS",
            },
            "claude_timeout_seconds": {
                "label": "Timeout (s)",
                "type": "int",
                "min": 30,
                "max": 900,
                "step": 30,
                "env_key": "CLAUDE_TIMEOUT_SECONDS",
            },
            "verbose_level": {
                "label": "Verbose",
                "type": "int",
                "min": 0,
                "max": 2,
                "step": 1,
                "env_key": "VERBOSE_LEVEL",
            },
        },
    },
    "features": {
        "label": "🔧 Features",
        "fields": {
            "enable_mcp": {
                "label": "MCP",
                "type": "bool",
                "env_key": "ENABLE_MCP",
            },
            "enable_git_integration": {
                "label": "Git integration",
                "type": "bool",
                "env_key": "ENABLE_GIT_INTEGRATION",
            },
            "enable_file_uploads": {
                "label": "File uploads",
                "type": "bool",
                "env_key": "ENABLE_FILE_UPLOADS",
            },
            "agentic_mode": {
                "label": "Agentic mode",
                "type": "bool",
                "env_key": "AGENTIC_MODE",
            },
        },
    },
    "limits": {
        "label": "⚡ Limits",
        "fields": {
            "rate_limit_requests": {
                "label": "Rate limit (req)",
                "type": "int",
                "min": 1,
                "max": 100,
                "step": 5,
                "env_key": "RATE_LIMIT_REQUESTS",
            },
            "rate_limit_window": {
                "label": "Rate window (s)",
                "type": "int",
                "min": 10,
                "max": 300,
                "step": 10,
                "env_key": "RATE_LIMIT_WINDOW",
            },
            "claude_max_cost_per_user": {
                "label": "Max cost/user ($)",
                "type": "float",
                "min": 1.0,
                "max": 100.0,
                "step": 1.0,
                "env_key": "CLAUDE_MAX_COST_PER_USER",
            },
        },
    },
    "security": {
        "label": "🔒 Security",
        "fields": {
            "sandbox_enabled": {
                "label": "Sandbox",
                "type": "bool",
                "env_key": "SANDBOX_ENABLED",
            },
            "disable_security_patterns": {
                "label": "Disable security patterns",
                "type": "bool",
                "env_key": "DISABLE_SECURITY_PATTERNS",
            },
            "disable_tool_validation": {
                "label": "Disable tool validation",
                "type": "bool",
                "env_key": "DISABLE_TOOL_VALIDATION",
            },
        },
    },
    "memory": {
        "label": "🧠 Memory",
        "fields": {
            "enable_memory": {
                "label": "Enable memory",
                "type": "bool",
                "env_key": "ENABLE_MEMORY",
            },
            "memory_max_facts": {
                "label": "Max facts",
                "type": "int",
                "min": 10,
                "max": 200,
                "step": 10,
                "env_key": "MEMORY_MAX_FACTS",
            },
            "memory_max_context_items": {
                "label": "Max context items",
                "type": "int",
                "min": 1,
                "max": 30,
                "step": 5,
                "env_key": "MEMORY_MAX_CONTEXT_ITEMS",
            },
        },
    },
    "checkins": {
        "label": "📋 Check-ins",
        "fields": {
            "enable_checkins": {
                "label": "Enable check-ins",
                "type": "bool",
                "env_key": "ENABLE_CHECKINS",
            },
            "checkin_interval_minutes": {
                "label": "Interval (min)",
                "type": "int",
                "min": 5,
                "max": 120,
                "step": 5,
                "env_key": "CHECKIN_INTERVAL_MINUTES",
            },
            "checkin_max_per_day": {
                "label": "Max per day",
                "type": "int",
                "min": 0,
                "max": 20,
                "step": 1,
                "env_key": "CHECKIN_MAX_PER_DAY",
            },
        },
    },
}


# ── Helpers ───────────────────────────────────────────────────────────────────


def find_field(field_name: str) -> _FieldDef | None:
    """Return the field definition for *field_name*, or None if not found."""
    for cat in SETTINGS_CATEGORIES.values():
        if field_name in cat["fields"]:
            return cat["fields"][field_name]
    return None


def find_category(field_name: str) -> str:
    """Return the category key that contains *field_name* (fallback: 'claude')."""
    for cat_key, cat in SETTINGS_CATEGORIES.items():
        if field_name in cat["fields"]:
            return cat_key
    return "claude"


def resolve_env_file() -> Path | None:
    """Return path to the .env for legacy dotenv writes (prefer resolve_config_file)."""
    new_path = APP_HOME / "config" / ".env"
    if new_path.exists():
        return new_path
    legacy = Path(".env")
    if legacy.exists():
        return legacy
    return None


def resolve_config_file() -> tuple[Path | None, str]:
    """Return (path, format) for the writable config file.

    Returns:
        (path, "toml")   — settings.toml exists (preferred)
        (path, "dotenv") — .env exists (legacy)
        (None, "none")   — no config file found
    """
    toml_path = APP_HOME / "config" / "settings.toml"
    if toml_path.exists():
        return toml_path, "toml"

    env_path = APP_HOME / "config" / ".env"
    if env_path.exists():
        return env_path, "dotenv"

    legacy = Path(".env")
    if legacy.exists():
        return legacy, "dotenv"

    return None, "none"


def is_owner(user_id: int, settings: Settings) -> bool:
    """Return True if *user_id* is the first entry in ALLOWED_USERS."""
    if not settings.allowed_users:
        return False
    return settings.allowed_users[0] == user_id


# ── Persistence ───────────────────────────────────────────────────────────────


def apply_setting(
    settings: Settings,
    env_path: Path | None,  # kept for backward-compat; ignored when TOML exists
    field: str,
    value: Any,
) -> str:
    """Persist *value* for *field* and update settings in-memory.

    Writes to settings.toml (preferred, preserves comments) or falls back to
    .env via python-dotenv. Returns a human-readable change description.
    """
    field_def = find_field(field)
    label = field_def["label"] if field_def else field
    field_type = field_def.get("type", "str") if field_def else "str"
    env_key = field_def["env_key"] if field_def else field.upper()

    # Coerce to correct Python type
    if field_type == "bool":
        typed_value: Any = value if isinstance(value, bool) else str(value).lower() in ("true", "1", "yes")
        str_value = "true" if typed_value else "false"
    elif field_type == "int":
        typed_value = int(value)
        str_value = str(typed_value)
    elif field_type == "float":
        typed_value = float(value)
        str_value = str(typed_value)
    else:
        typed_value = str(value)
        str_value = typed_value

    old_value = getattr(settings, field, None)

    # Persist: TOML preferred, dotenv fallback
    config_path, config_fmt = resolve_config_file()
    if config_fmt == "toml" and config_path:
        _write_toml_value(config_path, field, typed_value)
    elif config_path:
        set_key(str(config_path), env_key, str_value)

    # Update in-memory immediately
    setattr(settings, field, typed_value)

    return f"{label}: {old_value} → {typed_value}"


def _write_toml_value(toml_path: Path, field_name: str, value: Any) -> None:
    """Update a single field in settings.toml, preserving all other content."""
    section = FIELD_TO_SECTION.get(field_name)
    if section is None:
        return  # field not managed by TOML — skip

    text = toml_path.read_text(encoding="utf-8")
    doc = tomlkit.parse(text)

    if section not in doc:
        doc.add(section, tomlkit.table())

    doc[section][field_name] = value  # type: ignore[index]
    toml_path.write_text(tomlkit.dumps(doc), encoding="utf-8")


def toggle_setting(settings: Settings, env_path: Path | None, field: str) -> str:
    """Flip a boolean field and persist."""
    current = getattr(settings, field, False)
    return apply_setting(settings, env_path, field, not current)


def increment_setting(settings: Settings, env_path: Path | None, field: str, direction: int) -> str:
    """Increment (direction=+1) or decrement (direction=-1) an int/float field."""
    field_def = find_field(field)
    if not field_def:
        return f"Unknown field: {field}"

    current = getattr(settings, field, 0)
    step = field_def.get("step", 1)
    min_val = field_def.get("min", 0)
    max_val = field_def.get("max", 100)

    new_val = current + direction * step
    # Clamp to [min, max]
    new_val = max(min_val, min(max_val, new_val))

    return apply_setting(settings, env_path, field, new_val)


# ── Keyboard builders ─────────────────────────────────────────────────────────


def build_menu_keyboard() -> InlineKeyboardMarkup:
    """Return the top-level category grid (2 buttons per row)."""
    rows: list[list[InlineKeyboardButton]] = []
    cat_keys = list(SETTINGS_CATEGORIES.keys())
    for i in range(0, len(cat_keys), 2):
        row: list[InlineKeyboardButton] = []
        for j in range(2):
            if i + j < len(cat_keys):
                key = cat_keys[i + j]
                label = SETTINGS_CATEGORIES[key]["label"]
                row.append(InlineKeyboardButton(label, callback_data=f"set:cat:{key}"))
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def build_category_keyboard(cat_key: str, settings: Settings) -> InlineKeyboardMarkup:
    """Return field rows for *cat_key* with edit controls and a Back button."""
    cat = SETTINGS_CATEGORIES.get(cat_key, {})
    fields: dict[str, _FieldDef] = cat.get("fields", {})
    rows: list[list[InlineKeyboardButton]] = []

    for field_name, field_def in fields.items():
        current_val = getattr(settings, field_name, None)
        field_type = field_def["type"]
        label = field_def["label"]

        if field_type == "bool":
            icon = "✅" if current_val else "❌"
            rows.append([InlineKeyboardButton(f"{icon} {label}", callback_data=f"set:toggle:{field_name}")])

        elif field_type in ("int", "float"):
            fmt = f"{current_val:.1f}" if field_type == "float" else str(current_val)
            rows.append(
                [
                    InlineKeyboardButton("−", callback_data=f"set:dec:{field_name}"),
                    InlineKeyboardButton(f"{label}: {fmt}", callback_data="set:noop"),
                    InlineKeyboardButton("+", callback_data=f"set:inc:{field_name}"),
                ]
            )

        elif field_type == "choice":
            # Show short alias if available, else full value
            display = str(current_val or "")
            choices: dict[str, str] = field_def.get("choices", {})
            for alias, full_id in choices.items():
                if full_id == display:
                    display = alias
                    break
            rows.append(
                [
                    InlineKeyboardButton(f"{label}: {display}", callback_data="set:noop"),
                    InlineKeyboardButton("Change", callback_data=f"set:choose:{field_name}"),
                ]
            )

    rows.append([InlineKeyboardButton("← Back", callback_data="set:menu")])
    return InlineKeyboardMarkup(rows)


def build_choice_keyboard(field_name: str, choices: dict[str, str]) -> InlineKeyboardMarkup:
    """Return a picker keyboard for *choices* with a Cancel button."""
    rows: list[list[InlineKeyboardButton]] = []
    items = list(choices.items())
    for i in range(0, len(items), 3):
        row: list[InlineKeyboardButton] = []
        for j in range(3):
            if i + j < len(items):
                alias, full_id = items[i + j]
                row.append(InlineKeyboardButton(alias, callback_data=f"set:val:{field_name}:{full_id}"))
        rows.append(row)

    cat_key = find_category(field_name)
    rows.append([InlineKeyboardButton("← Cancel", callback_data=f"set:cat:{cat_key}")])
    return InlineKeyboardMarkup(rows)
