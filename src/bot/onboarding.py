"""Telegram onboarding wizard.

A ConversationHandler that guides the owner through initial bot setup:
  Step 1: Workspace directory
  Step 2: Claude model selection
  Step 3: Anthropic API key (skipped when already configured)
  Step 4: Feature toggles + output verbosity
  Step 5: Personalization (name, timezone, profile path)
  Step 6: Voice transcription
  Step 7: Summary + save

Registration: add to group 5 so text input is captured before the
              agentic_text handler in group 10.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import structlog
import tomlkit
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telegram.warnings import PTBUserWarning

from ..config.settings import Settings
from .utils.html_format import escape_html

logger = structlog.get_logger()

# Conversation states
(
    WORKSPACE,
    WORKSPACE_INPUT,
    MODEL,
    API_KEY,
    FEATURES,
    PERSONALIZATION,
    PERSONALIZATION_INPUT,
    VOICE,
    VOICE_INPUT,
) = range(9)

# (field_name, display_label, one-line description)
_FEATURE_FIELDS: list[tuple[str, str, str]] = [
    ("agentic_mode", "Agentic mode", "Conversational AI (default). Disable for classic /cmd mode."),
    ("enable_mcp", "MCP tools", "Connect external tools via Model Context Protocol."),
    ("enable_memory", "Memory", "Remember facts across conversations."),
    ("enable_git_integration", "Git", "Run git commands; inject repo context into Claude."),
    ("enable_file_uploads", "File uploads", "Accept file uploads in chat."),
    ("enable_quick_actions", "Quick actions", "Show shortcut buttons after responses."),
    ("enable_project_threads", "Project threads", "Route messages to Telegram topic threads per project."),
    ("enable_checkins", "Check-ins", "Proactive check-ins via Claude (requires scheduler)."),
    ("development_mode", "Dev mode", "Enable development features and verbose debug output."),
]

_MODEL_CHOICES: list[tuple[str, str]] = [
    ("claude-haiku-4-5", "Haiku 4.5 — fastest, cheapest"),
    ("claude-sonnet-4-5", "Sonnet 4.5 — balanced (recommended)"),
    ("claude-sonnet-4-6", "Sonnet 4.6 — latest balanced"),
    ("claude-opus-4-6", "Opus 4.6 — most capable"),
]

_VERBOSE_LABELS = {0: "Quiet", 1: "Normal", 2: "Detailed"}
_VOICE_LABELS = {"": "disabled", "groq": "Groq (cloud)", "local": "whisper.cpp (local)"}
_TZ_PRESETS = ["UTC", "US/Eastern", "US/Pacific", "Europe/London", "Europe/Berlin", "Asia/Tokyo"]


# ── Context accessors ─────────────────────────────────────────────────────────


def _bd(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any]:
    return cast(dict[str, Any], context.bot_data)


def _ud(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any]:
    return cast(dict[str, Any], context.user_data)


def _s(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return _bd(context)["settings"]


# ── Keyboard builders ─────────────────────────────────────────────────────────


def _workspace_keyboard(s: Settings) -> InlineKeyboardMarkup:
    current = str(s.approved_directory)
    short = (current[:37] + "...") if len(current) > 40 else current
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"Keep: {short}", callback_data="wiz:ws:keep")],
            [
                InlineKeyboardButton("~/projects", callback_data="wiz:ws:projects"),
                InlineKeyboardButton("~/code", callback_data="wiz:ws:code"),
            ],
            [InlineKeyboardButton("Custom path...", callback_data="wiz:ws:custom")],
            [InlineKeyboardButton("Skip wizard", callback_data="wiz:skip")],
        ]
    )


def _model_keyboard(current_model: str) -> InlineKeyboardMarkup:
    rows = []
    for model_id, label in _MODEL_CHOICES:
        mark = "● " if model_id == current_model else "○ "
        rows.append([InlineKeyboardButton(f"{mark}{label}", callback_data=f"wiz:model:{model_id}")])
    rows.append([InlineKeyboardButton("Skip wizard", callback_data="wiz:skip")])
    return InlineKeyboardMarkup(rows)


def _features_keyboard(context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    features = _ud(context).get("wiz_features", {})
    s = _s(context)
    verbose = _ud(context).get("wiz_verbose", s.verbose_level)

    def icon(key: str) -> str:
        return "✅" if features.get(key, getattr(s, key, False)) else "❌"

    vlabel = _VERBOSE_LABELS.get(verbose, "Normal")
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"{icon('agentic_mode')} Agentic mode", callback_data="wiz:feat:agentic_mode")],
            [
                InlineKeyboardButton(f"{icon('enable_mcp')} MCP", callback_data="wiz:feat:enable_mcp"),
                InlineKeyboardButton(f"{icon('enable_memory')} Memory", callback_data="wiz:feat:enable_memory"),
            ],
            [
                InlineKeyboardButton(
                    f"{icon('enable_git_integration')} Git",
                    callback_data="wiz:feat:enable_git_integration",
                ),
                InlineKeyboardButton(
                    f"{icon('enable_file_uploads')} Files",
                    callback_data="wiz:feat:enable_file_uploads",
                ),
            ],
            [
                InlineKeyboardButton(
                    f"{icon('enable_quick_actions')} Quick actions",
                    callback_data="wiz:feat:enable_quick_actions",
                ),
                InlineKeyboardButton(
                    f"{icon('enable_project_threads')} Threads",
                    callback_data="wiz:feat:enable_project_threads",
                ),
            ],
            [
                InlineKeyboardButton(
                    f"{icon('enable_checkins')} Check-ins",
                    callback_data="wiz:feat:enable_checkins",
                ),
                InlineKeyboardButton(
                    f"{icon('development_mode')} Dev mode",
                    callback_data="wiz:feat:development_mode",
                ),
            ],
            [InlineKeyboardButton(f"📢 Output: {vlabel} (tap to cycle)", callback_data="wiz:feat:verbose_cycle")],
            [InlineKeyboardButton("Continue →", callback_data="wiz:feat:done")],
            [InlineKeyboardButton("Skip wizard", callback_data="wiz:skip")],
        ]
    )


def _personalization_keyboard(context: ContextTypes.DEFAULT_TYPE, s: Settings) -> InlineKeyboardMarkup:
    name = _ud(context).get("wiz_user_name", s.user_name or "not set")
    tz = _ud(context).get("wiz_user_timezone", s.user_timezone)
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"👤 Name: {name}", callback_data="wiz:pz:set:user_name")],
            [InlineKeyboardButton(f"🕐 Timezone: {tz}", callback_data="wiz:pz:set:user_timezone")],
            [InlineKeyboardButton("📄 Set profile path...", callback_data="wiz:pz:set:user_profile_path")],
            [InlineKeyboardButton("Continue →", callback_data="wiz:pz:done")],
            [InlineKeyboardButton("Skip wizard", callback_data="wiz:skip")],
        ]
    )


def _tz_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, len(_TZ_PRESETS), 2):
        pair = _TZ_PRESETS[i : i + 2]
        rows.append([InlineKeyboardButton(tz, callback_data=f"wiz:pz:tz:{tz}") for tz in pair])
    rows.append([InlineKeyboardButton("← Back", callback_data="wiz:pz:back")])
    return InlineKeyboardMarkup(rows)


def _voice_keyboard(context: ContextTypes.DEFAULT_TYPE, s: Settings) -> InlineKeyboardMarkup:
    provider = _ud(context).get("wiz_voice_provider", s.voice_provider or "")
    plabel = _VOICE_LABELS.get(provider, provider)
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(f"🎤 Provider: {plabel} (tap to cycle)", callback_data="wiz:voice:cycle")],
    ]
    if provider == "local":
        binary = _ud(context).get("wiz_whisper_binary", s.whisper_binary)
        rows.append([InlineKeyboardButton(f"🔧 Binary: {binary}", callback_data="wiz:voice:binary")])
    rows.append([InlineKeyboardButton("Save & finish →", callback_data="wiz:voice:done")])
    rows.append([InlineKeyboardButton("Skip wizard", callback_data="wiz:skip")])
    return InlineKeyboardMarkup(rows)


# ── TOML helper ───────────────────────────────────────────────────────────────


def _toml_write(section: str, field: str, value: Any) -> None:
    from .settings_ui import resolve_config_file

    config_path, fmt = resolve_config_file()
    if fmt != "toml" or not config_path:
        return
    text = config_path.read_text(encoding="utf-8")
    doc = tomlkit.parse(text)
    if section not in doc:
        doc.add(tomlkit.nl())
        doc.add(section, tomlkit.table())
    doc[section][field] = value  # type: ignore[index]
    config_path.write_text(tomlkit.dumps(doc), encoding="utf-8")


# ── Step text helpers ─────────────────────────────────────────────────────────


def _model_step_text(s: Settings) -> str:
    return (
        "Step 2/6 — Claude model\n\n"
        "Which AI model should the bot use? Changeable anytime via /settings.\n\n"
        "  Haiku 4.5 — fastest & cheapest; good for simple tasks\n"
        "  Sonnet 4.5 — best balance of speed and quality (recommended)\n"
        "  Sonnet 4.6 — latest Sonnet, improved reasoning\n"
        "  Opus 4.6 — most capable; slower and more expensive\n\n"
        f"Current: {s.claude_model}"
    )


def _features_step_text() -> str:
    return (
        "Step 4/6 — Features & output\n\n"
        "Toggle features on/off. Tap 📢 to cycle output verbosity.\n\n"
        "  Agentic — conversational AI (default) vs classic /cmd mode\n"
        "  MCP — connect external tools (requires mcp.json config)\n"
        "  Memory — remember facts across conversations\n"
        "  Git — run git commands; inject repo context\n"
        "  Files — accept file uploads in chat\n"
        "  Quick actions — shortcut buttons after responses\n"
        "  Threads — route by Telegram forum topic per project\n"
        "  Check-ins — proactive check-ins (requires scheduler)\n"
        "  Dev mode — enable development features\n"
        "  Output: Quiet=final only · Normal=show tools · Detailed=show inputs"
    )


def _personalization_step_text() -> str:
    return (
        "Step 5/6 — Personalization\n\n"
        "Name: how Claude addresses you in conversation.\n"
        "Timezone: used for scheduling and context (e.g. Europe/Berlin, US/Eastern, UTC).\n"
        "Profile path: a markdown file injected into Claude's system context each session. "
        "Auto-created at ~/.claude-code-telegram/config/profile.md if not set."
    )


def _voice_step_text() -> str:
    return (
        "Step 6/6 — Voice transcription\n\n"
        "Send voice messages to have them transcribed before Claude processes them.\n\n"
        "  disabled — no voice transcription\n"
        "  Groq — cloud transcription via Groq API (fast; requires GROQ_API_KEY in config)\n"
        "  whisper.cpp — local transcription (private; requires binary install)"
    )


# ── Handler functions ─────────────────────────────────────────────────────────


async def _wiz_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    s = _s(context)
    user = update.effective_user
    if not user:
        return ConversationHandler.END

    from .settings_ui import is_owner

    if not is_owner(user.id, s):
        if update.message:
            await update.message.reply_text("Owner only.")
        elif update.callback_query:
            await update.callback_query.answer("Owner only", show_alert=True)
        return ConversationHandler.END

    # Clear stale wizard state on each /setup entry
    for key in (
        "wiz_workspace",
        "wiz_model",
        "wiz_api_key",
        "wiz_features",
        "wiz_verbose",
        "wiz_user_name",
        "wiz_user_timezone",
        "wiz_user_profile_path",
        "wiz_voice_provider",
        "wiz_whisper_binary",
        "pz_field",
    ):
        _ud(context).pop(key, None)

    api_status = "configured ✓" if s.anthropic_api_key_str else "not set"
    active = [lbl for fld, lbl, _ in _FEATURE_FIELDS if getattr(s, fld, False)]
    voice_label = _VOICE_LABELS.get(s.voice_provider or "", "disabled")
    text = (
        "Claude Code Telegram — Setup Wizard\n\n"
        "Current config:\n\n"
        f"  Workspace:  {s.approved_directory}\n"
        f"  Model:      {s.claude_model}\n"
        f"  API key:    {api_status}\n"
        f"  Features:   {', '.join(active) or 'none enabled'}\n"
        f"  Verbosity:  {_VERBOSE_LABELS.get(s.verbose_level, 'Normal')}\n"
        f"  Name:       {s.user_name or 'not set'}\n"
        f"  Timezone:   {s.user_timezone}\n"
        f"  Voice:      {voice_label}\n\n"
        "Run /setup anytime to reconfigure. Use /settings for live tweaks."
    )
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Configure →", callback_data="wiz:go")],
            [InlineKeyboardButton("Skip", callback_data="wiz:skip")],
        ]
    )
    if update.message:
        await update.message.reply_text(text, reply_markup=kb)
    elif update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, reply_markup=kb)
    return WORKSPACE


async def _wiz_go(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    s = _s(context)
    query = update.callback_query
    assert query is not None
    await query.answer()
    await query.edit_message_text(
        "Step 1/6 — Workspace\n\n"
        "This is the root directory Claude is allowed to read and write. "
        "It acts as a sandbox boundary — Claude cannot access paths outside it.\n\n"
        f"Current: {s.approved_directory}",
        reply_markup=_workspace_keyboard(s),
    )
    return WORKSPACE


async def _wiz_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = "Setup skipped. Run /setup anytime to configure."
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(msg)
    elif update.message:
        await update.message.reply_text(msg)
    return ConversationHandler.END


async def _wiz_workspace_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    s = _s(context)
    query = update.callback_query
    assert query is not None
    await query.answer()

    data = query.data or ""
    choice = data.split(":", 2)[2]

    if choice == "keep":
        _ud(context)["wiz_workspace"] = str(s.approved_directory)
    elif choice == "custom":
        await query.edit_message_text(
            "Step 1/6 — Custom workspace\n\n"
            "Send the full absolute path to your workspace directory.\n"
            "Example: /home/user/projects",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data="wiz:ws:back")]]),
        )
        return WORKSPACE_INPUT
    elif choice == "projects":
        _ud(context)["wiz_workspace"] = str(Path("~/projects").expanduser())
    elif choice == "code":
        _ud(context)["wiz_workspace"] = str(Path("~/code").expanduser())

    return await _to_model(query, context, s)


async def _wiz_workspace_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    s = _s(context)
    query = update.callback_query
    assert query is not None
    await query.answer()
    await query.edit_message_text(
        f"Step 1/6 — Workspace\n\nWhere can Claude work?\n\nCurrent: {s.approved_directory}",
        reply_markup=_workspace_keyboard(s),
    )
    return WORKSPACE


async def _wiz_workspace_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    s = _s(context)
    assert update.message is not None
    path_str = (update.message.text or "").strip()
    path = Path(path_str).expanduser().resolve()

    if not path.is_dir():
        await update.message.reply_text(
            f"Directory not found: {escape_html(path_str)}\nSend a valid absolute path or press Back.",
            parse_mode="HTML",
        )
        raise ApplicationHandlerStop(WORKSPACE_INPUT)

    _ud(context)["wiz_workspace"] = str(path)
    await update.message.reply_text(f"Workspace set to {path}")
    await update.message.reply_text(
        _model_step_text(s),
        reply_markup=_model_keyboard(_ud(context).get("wiz_model", s.claude_model)),
    )
    raise ApplicationHandlerStop(MODEL)


async def _to_model(query: Any, context: ContextTypes.DEFAULT_TYPE, s: Settings) -> int:
    await query.edit_message_text(
        _model_step_text(s),
        reply_markup=_model_keyboard(_ud(context).get("wiz_model", s.claude_model)),
    )
    return MODEL


async def _wiz_model_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    s = _s(context)
    query = update.callback_query
    assert query is not None
    await query.answer()
    _ud(context)["wiz_model"] = (query.data or "").split(":", 2)[2]
    return await _to_api_key(query, context, s)


async def _to_api_key(query: Any, context: ContextTypes.DEFAULT_TYPE, s: Settings) -> int:
    if not s.anthropic_api_key_str:
        await query.edit_message_text(
            "Step 3/6 — Anthropic API key\n\n"
            "The bot calls the Anthropic API to run Claude. Paste your key here, "
            "or skip if the Claude CLI is already authenticated on this machine.\n\n"
            "Get yours at console.anthropic.com → API Keys.\n"
            "Keys start with sk-ant-",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Skip (use CLI auth)", callback_data="wiz:key:skip")]]
            ),
        )
        return API_KEY

    masked = "sk-ant-***" + s.anthropic_api_key_str[-4:]
    _init_features(context, s)
    await query.edit_message_text(
        f"API key already configured ({masked}).\n\n" + _features_step_text(),
        reply_markup=_features_keyboard(context),
    )
    return FEATURES


async def _wiz_api_key_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    s = _s(context)
    query = update.callback_query
    assert query is not None
    await query.answer()
    _init_features(context, s)
    await query.edit_message_text(
        "API key skipped.\n\n" + _features_step_text(),
        reply_markup=_features_keyboard(context),
    )
    return FEATURES


async def _wiz_api_key_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    s = _s(context)
    assert update.message is not None
    key = (update.message.text or "").strip()

    if not key.startswith("sk-ant-"):
        await update.message.reply_text(
            "That doesn't look like an Anthropic API key.\nKeys start with sk-ant-. Try again or press Skip."
        )
        raise ApplicationHandlerStop(API_KEY)

    _ud(context)["wiz_api_key"] = key
    _init_features(context, s)
    await update.message.reply_text(
        "API key saved.\n\n" + _features_step_text(),
        reply_markup=_features_keyboard(context),
    )
    raise ApplicationHandlerStop(FEATURES)


async def _wiz_feature_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    s = _s(context)
    query = update.callback_query
    assert query is not None
    await query.answer()

    key = (query.data or "").split(":", 2)[2]

    if key == "done":
        await query.edit_message_text(
            _personalization_step_text(),
            reply_markup=_personalization_keyboard(context, s),
        )
        return PERSONALIZATION

    if key == "verbose_cycle":
        current = _ud(context).get("wiz_verbose", s.verbose_level)
        _ud(context)["wiz_verbose"] = (current + 1) % 3
    else:
        _init_features(context, s)
        features = _ud(context)["wiz_features"]
        features[key] = not features.get(key, getattr(s, key, False))

    await query.edit_message_text(_features_step_text(), reply_markup=_features_keyboard(context))
    return FEATURES


async def _wiz_personalization_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    s = _s(context)
    query = update.callback_query
    assert query is not None
    await query.answer()

    # data: wiz:pz:<action>  — action is "done", "set:user_name", etc.
    action = (query.data or "")[len("wiz:pz:") :]

    if action == "done":
        await query.edit_message_text(_voice_step_text(), reply_markup=_voice_keyboard(context, s))
        return VOICE

    if action.startswith("set:"):
        field = action[4:]
        _ud(context)["pz_field"] = field
        if field == "user_timezone":
            await query.edit_message_text(
                "Set timezone\n\nPick a preset or type a custom value (e.g. America/New_York):",
                reply_markup=_tz_keyboard(),
            )
        elif field == "user_name":
            await query.edit_message_text(
                "Set name\n\nSend your name as text (e.g. Cosmin):",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data="wiz:pz:back")]]),
            )
        else:  # user_profile_path
            await query.edit_message_text(
                "Set profile path\n\n"
                "Path to a markdown file injected into Claude's system context each session.\n"
                "Leave blank (send a space) to keep the default path.\n\n"
                "Send the full path or press Back:",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data="wiz:pz:back")]]),
            )
        return PERSONALIZATION_INPUT

    # Unknown action / stale back button — re-show keyboard
    await query.edit_message_text(_personalization_step_text(), reply_markup=_personalization_keyboard(context, s))
    return PERSONALIZATION


async def _wiz_personalization_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    s = _s(context)
    assert update.message is not None
    field = _ud(context).get("pz_field", "user_name")
    value = (update.message.text or "").strip()

    if field == "user_profile_path":
        path = Path(value).expanduser().resolve() if value else None
        _ud(context)["wiz_user_profile_path"] = str(path) if path else ""
        await update.message.reply_text(f"Profile path set to: {path or 'default'}")
    elif field == "user_name":
        _ud(context)["wiz_user_name"] = value
        await update.message.reply_text(f"Name set to: {value}")
    else:
        _ud(context)["wiz_user_timezone"] = value
        await update.message.reply_text(f"Timezone set to: {value}")

    await update.message.reply_text(_personalization_step_text(), reply_markup=_personalization_keyboard(context, s))
    raise ApplicationHandlerStop(PERSONALIZATION)


async def _wiz_personalization_preset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle timezone preset buttons inside PERSONALIZATION_INPUT state."""
    s = _s(context)
    query = update.callback_query
    assert query is not None
    await query.answer()

    tz = (query.data or "")[len("wiz:pz:tz:") :]
    _ud(context)["wiz_user_timezone"] = tz
    await query.edit_message_text(_personalization_step_text(), reply_markup=_personalization_keyboard(context, s))
    return PERSONALIZATION


async def _wiz_personalization_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    s = _s(context)
    query = update.callback_query
    assert query is not None
    await query.answer()
    await query.edit_message_text(_personalization_step_text(), reply_markup=_personalization_keyboard(context, s))
    return PERSONALIZATION


async def _wiz_voice_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    s = _s(context)
    query = update.callback_query
    assert query is not None
    await query.answer()

    action = (query.data or "")[len("wiz:voice:") :]

    if action == "done":
        return await _wiz_save_and_done(query, context, s)

    if action == "cycle":
        providers = ["", "groq", "local"]
        current = _ud(context).get("wiz_voice_provider", s.voice_provider or "")
        idx = providers.index(current) if current in providers else 0
        _ud(context)["wiz_voice_provider"] = providers[(idx + 1) % 3]
    elif action == "binary":
        binary = _ud(context).get("wiz_whisper_binary", s.whisper_binary)
        await query.edit_message_text(
            f"Set whisper binary path\n\nCurrent: {binary}\n\n"
            "Send the full path to your whisper-cpp binary.\n"
            "Example: /usr/local/bin/whisper-cpp",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data="wiz:voice:back")]]),
        )
        return VOICE_INPUT

    await query.edit_message_text(_voice_step_text(), reply_markup=_voice_keyboard(context, s))
    return VOICE


async def _wiz_voice_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    s = _s(context)
    assert update.message is not None
    value = (update.message.text or "").strip()
    _ud(context)["wiz_whisper_binary"] = value
    await update.message.reply_text(f"Binary path set to: {value}")
    await update.message.reply_text(_voice_step_text(), reply_markup=_voice_keyboard(context, s))
    raise ApplicationHandlerStop(VOICE)


async def _wiz_voice_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    s = _s(context)
    query = update.callback_query
    assert query is not None
    await query.answer()
    await query.edit_message_text(_voice_step_text(), reply_markup=_voice_keyboard(context, s))
    return VOICE


async def _wiz_save_and_done(
    query: Any,
    context: ContextTypes.DEFAULT_TYPE,
    s: Settings,
) -> int:
    from .settings_ui import apply_setting, resolve_env_file

    ud = _ud(context)
    changes: list[str] = []
    env_path = resolve_env_file()

    def _try(field: str, value: Any, label: str) -> None:
        try:
            apply_setting(s, env_path, field, value)
            changes.append(label)
        except Exception as exc:
            logger.warning("Wizard: failed to save field", field=field, error=str(exc))

    workspace = ud.get("wiz_workspace")
    if workspace and workspace != str(s.approved_directory):
        try:
            apply_setting(s, env_path, "approved_directory", workspace)
            s.approved_directory = Path(workspace).expanduser().resolve()
            changes.append(f"Workspace → {workspace}")
        except Exception as exc:
            logger.warning("Wizard: failed to save workspace", error=str(exc))

    api_key = ud.get("wiz_api_key")
    if api_key:
        try:
            from pydantic import SecretStr

            apply_setting(s, env_path, "anthropic_api_key", api_key)
            s.anthropic_api_key = SecretStr(api_key)
            changes.append("API key set")
        except Exception as exc:
            logger.warning("Wizard: failed to save API key", error=str(exc))

    model = ud.get("wiz_model")
    if model and model != s.claude_model:
        _try("claude_model", model, f"Model → {model}")

    verbose = ud.get("wiz_verbose")
    if verbose is not None and verbose != s.verbose_level:
        _try("verbose_level", verbose, f"Output → {_VERBOSE_LABELS.get(verbose, verbose)}")

    for field, label, _ in _FEATURE_FIELDS:
        new_val = ud.get("wiz_features", {}).get(field)
        if new_val is not None and new_val != getattr(s, field, None):
            _try(field, new_val, f"{label}: {'on' if new_val else 'off'}")

    user_name = ud.get("wiz_user_name")
    if user_name is not None and user_name != (s.user_name or ""):
        _try("user_name", user_name, f"Name → {user_name}")

    user_tz = ud.get("wiz_user_timezone")
    if user_tz and user_tz != s.user_timezone:
        _try("user_timezone", user_tz, f"Timezone → {user_tz}")

    user_profile = ud.get("wiz_user_profile_path")
    if user_profile is not None:
        _try("user_profile_path", user_profile, "Profile path set")

    voice_provider = ud.get("wiz_voice_provider")
    if voice_provider is not None and voice_provider != (s.voice_provider or ""):
        vlabel = _VOICE_LABELS.get(voice_provider, voice_provider)
        _try("voice_provider", voice_provider, f"Voice → {vlabel}")

    whisper_binary = ud.get("wiz_whisper_binary")
    if whisper_binary and whisper_binary != s.whisper_binary:
        _try("whisper_binary", whisper_binary, f"Whisper binary → {whisper_binary}")

    try:
        s.setup_completed = True
        _toml_write("onboarding", "setup_completed", True)
    except Exception as exc:
        logger.warning("Wizard: failed to mark setup_completed", error=str(exc))

    if changes:
        summary = "\n".join(f"  • {c}" for c in changes)
        text = f"All done!\n\nSaved:\n{summary}\n\nUse /settings to adjust anytime."
    else:
        text = "All done! No changes made.\n\nUse /settings to configure anytime."

    await query.edit_message_text(text)
    return ConversationHandler.END


async def _wiz_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message:
        await update.message.reply_text("Setup cancelled. Run /setup anytime.")
    return ConversationHandler.END


# ── Private helpers ───────────────────────────────────────────────────────────


def _init_features(context: ContextTypes.DEFAULT_TYPE, s: Settings) -> None:
    if "wiz_features" not in _ud(context):
        _ud(context)["wiz_features"] = {field: getattr(s, field, False) for field, _, _ in _FEATURE_FIELDS}


# ── Public factory ────────────────────────────────────────────────────────────


def build_conversation_handler(settings: Settings, deps: dict[str, Any]) -> ConversationHandler:
    """Build the onboarding ConversationHandler ready for registration."""

    def inject(func: Any) -> Any:
        async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Any:
            bd = cast(dict[str, Any], context.bot_data)
            for key, value in deps.items():
                bd[key] = value
            bd["settings"] = settings
            return await func(update, context)

        wrapped.__name__ = getattr(func, "__name__", "wrapped")
        return wrapped

    import warnings

    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=".*per_message=False.*", category=PTBUserWarning)
        return ConversationHandler(
            entry_points=[
                CommandHandler("setup", inject(_wiz_welcome)),
                CallbackQueryHandler(inject(_wiz_welcome), pattern=r"^wiz:start$"),
            ],
            states={
                WORKSPACE: [
                    CallbackQueryHandler(inject(_wiz_go), pattern=r"^wiz:go$"),
                    CallbackQueryHandler(inject(_wiz_skip), pattern=r"^wiz:skip$"),
                    CallbackQueryHandler(inject(_wiz_workspace_choice), pattern=r"^wiz:ws:(?!back$)"),
                ],
                WORKSPACE_INPUT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, inject(_wiz_workspace_input)),
                    CallbackQueryHandler(inject(_wiz_workspace_back), pattern=r"^wiz:ws:back$"),
                ],
                MODEL: [
                    CallbackQueryHandler(inject(_wiz_model_choice), pattern=r"^wiz:model:"),
                    CallbackQueryHandler(inject(_wiz_skip), pattern=r"^wiz:skip$"),
                ],
                API_KEY: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, inject(_wiz_api_key_input)),
                    CallbackQueryHandler(inject(_wiz_api_key_skip), pattern=r"^wiz:key:skip$"),
                ],
                FEATURES: [
                    CallbackQueryHandler(inject(_wiz_feature_toggle), pattern=r"^wiz:feat:"),
                    CallbackQueryHandler(inject(_wiz_skip), pattern=r"^wiz:skip$"),
                ],
                PERSONALIZATION: [
                    CallbackQueryHandler(inject(_wiz_personalization_action), pattern=r"^wiz:pz:"),
                    CallbackQueryHandler(inject(_wiz_skip), pattern=r"^wiz:skip$"),
                ],
                PERSONALIZATION_INPUT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, inject(_wiz_personalization_input)),
                    CallbackQueryHandler(inject(_wiz_personalization_preset), pattern=r"^wiz:pz:tz:"),
                    CallbackQueryHandler(inject(_wiz_personalization_back), pattern=r"^wiz:pz:back$"),
                ],
                VOICE: [
                    CallbackQueryHandler(inject(_wiz_voice_action), pattern=r"^wiz:voice:"),
                    CallbackQueryHandler(inject(_wiz_skip), pattern=r"^wiz:skip$"),
                ],
                VOICE_INPUT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, inject(_wiz_voice_input)),
                    CallbackQueryHandler(inject(_wiz_voice_back), pattern=r"^wiz:voice:back$"),
                ],
            },
            fallbacks=[
                CommandHandler("cancel", inject(_wiz_cancel)),
            ],
            per_message=False,
            per_user=True,
            per_chat=True,
            name="onboarding_wizard",
            allow_reentry=True,
        )
