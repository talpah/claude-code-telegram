"""Interactive CLI setup wizard for first-time configuration.

Prompts the user for all key settings and writes them to settings.toml.
Invoked automatically from main() when required fields are empty and
stdin is a TTY, or via `claude-telegram-setup` CLI entry point.

Covers the same settings as the Telegram /setup wizard:
  - Required: bot token, username, allowed users
  - Claude: workspace, API key, model
  - Features: agentic mode, MCP, memory, git, file uploads, quick actions,
              project threads, check-ins, dev mode
  - Output: verbosity level
  - Personalization: name, timezone, profile path
  - Voice: provider, language, model selection, model path, binary
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import tomlkit

from src.utils.constants import APP_HOME

TOML_PATH = APP_HOME / "config" / "settings.toml"

_MODEL_CHOICES = [
    ("claude-haiku-4-5", "fastest, cheapest"),
    ("claude-sonnet-4-5", "balanced (recommended)"),
    ("claude-sonnet-4-6", "latest balanced"),
    ("claude-opus-4-6", "most capable"),
]

_VOICE_CHOICES = [
    ("", "disabled"),
    ("groq", "Groq cloud API (requires GROQ_API_KEY)"),
    ("local", "whisper.cpp local binary"),
]

# (model_name, size, description)
_WHISPER_MODELS_EN: list[tuple[str, str, str]] = [
    ("tiny.en", "75 MiB", "fastest, lower accuracy"),
    ("base.en", "142 MiB", "good balance (recommended)"),
    ("small.en", "466 MiB", "better accuracy"),
    ("medium.en", "1.5 GiB", "high accuracy, slow"),
]
_WHISPER_MODELS_MULTI: list[tuple[str, str, str]] = [
    ("tiny", "75 MiB", "fastest, lower accuracy"),
    ("base", "142 MiB", "good balance (recommended)"),
    ("small", "466 MiB", "better accuracy"),
    ("large-v3-turbo-q5_0", "547 MiB", "best quality, quantized"),
]

_WHISPER_DEFAULT_DIR = Path.home() / ".claude-code-telegram" / "models"


# ── Dependency check ──────────────────────────────────────────────────────────

# package manager → install command for each tool
_PKG_INSTALL: dict[str, dict[str, str]] = {
    "claude": {
        # Claude Code CLI is an npm package regardless of OS
        "npm": "npm install -g @anthropic-ai/claude-code",
    },
    "whisper-cli": {
        # brew formula is still called whisper-cpp but installs whisper-cli binary
        "brew": "brew install whisper-cpp",
    },
    "whisper-cpp": {
        # Legacy binary name; brew formula handles both
        "brew": "brew install whisper-cpp",
    },
    "git": {
        "apt-get": "sudo apt install git",
        "pacman": "sudo pacman -S git",
        "brew": "brew install git",
        "dnf": "sudo dnf install git",
        "yum": "sudo yum install git",
    },
}

_WHISPER_BUILD_HINT = (
    "Build from source:\n"
    "    git clone https://github.com/ggml-org/whisper.cpp\n"
    "    cd whisper.cpp\n"
    "    cmake -B build && cmake --build build -j --config Release\n"
    "    sudo cp build/bin/whisper-cli /usr/local/bin/whisper-cli"
)

_FALLBACK_HINT: dict[str, str] = {
    "claude": "Install Node.js, then: npm install -g @anthropic-ai/claude-code",
    "whisper-cli": _WHISPER_BUILD_HINT,
    "whisper-cpp": _WHISPER_BUILD_HINT,
    "git": "https://git-scm.com/downloads",
}


def _detect_pkg_manager() -> str | None:
    for mgr in ("apt-get", "brew", "pacman", "dnf", "yum"):
        if shutil.which(mgr):
            return mgr
    return None


def _install_cmd(binary: str, pkg_mgr: str | None) -> str | None:
    """Return the install command for *binary* given the detected package manager."""
    cmds = _PKG_INSTALL.get(binary, {})
    # Special case: claude needs npm regardless of system package manager
    if binary == "claude" and shutil.which("npm"):
        return cmds.get("npm")
    # Special case: whisper prefers brew when available (avoids cmake build-from-source)
    if binary in ("whisper-cli", "whisper-cpp") and shutil.which("brew"):
        return cmds.get("brew")
    if pkg_mgr and pkg_mgr in cmds:
        return cmds[pkg_mgr]
    return _FALLBACK_HINT.get(binary)


def check_dependencies(
    voice_provider: str = "",
    whisper_binary: str = "whisper-cli",
    whisper_model_path: str = "",
    enable_git: bool = True,
) -> None:
    """Check for required external binaries and files; offer to install any that are missing."""
    pkg_mgr = _detect_pkg_manager()

    # Build list of (binary, description) to check based on active config
    checks: list[tuple[str, str]] = [
        ("claude", "Claude Code CLI — needed for CLI fallback when SDK fails"),
    ]
    if enable_git:
        checks.append(("git", "git — version control integration"))
    if voice_provider == "local":
        checks.append((whisper_binary, "whisper-cli binary — local voice transcription"))

    missing: list[tuple[str, str]] = []

    print(f"\n──── Checking dependencies {'─' * 32}")
    for binary, desc in checks:
        found = shutil.which(binary)
        status = f"✓  {found}" if found else "✗  not found"
        print(f"  {binary:<22} {status}")
        if not found:
            missing.append((binary, desc))

    # Check whisper model file (separate from binary)
    if voice_provider == "local" and whisper_model_path:
        model_file = Path(whisper_model_path).expanduser()
        status = f"✓  {model_file}" if model_file.exists() else "✗  not found"
        print(f"  {'whisper model':<22} {status}")
        if not model_file.exists():
            missing.append(("__whisper_model__", whisper_model_path))

    if not missing:
        print("  All dependencies found.")
        return

    print()
    for binary, desc in missing:
        if binary == "__whisper_model__":
            _print_model_hint(desc)
            continue
        cmd = _install_cmd(binary, pkg_mgr)
        print(f"  Missing: {binary}")
        print(f"    {desc}")
        if cmd and "\n" not in cmd:
            # Single-line command — offer to run it automatically
            print(f"    Install: {cmd}")
            try:
                run_it = input("    Run now? [y/N]: ").strip().lower()
            except EOFError:
                run_it = "n"
            if run_it == "y":
                result = subprocess.run(cmd, shell=True)  # noqa: S602
                if result.returncode == 0:
                    print(f"    ✓ {binary} installed successfully.")
                else:
                    print(f"    ✗ Install failed (exit {result.returncode}). Run manually.")
        elif cmd:
            # Multi-line instructions — display only, cannot auto-run
            print("    How to install:")
            for line in cmd.splitlines():
                print(f"      {line}")
        else:
            print("    No automatic install available.")
        print()


def _print_model_hint(model_path: str) -> None:
    """Print download instructions for a missing whisper GGML model file."""
    model_file = Path(model_path).expanduser()
    model_name = model_file.stem.replace("ggml-", "")  # e.g. "base.en"
    model_dir = str(model_file.parent)
    hf_url = f"https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-{model_name}.bin"
    print(f"  Missing: whisper model ({model_name})")
    print("    Download directly:")
    print(f"      mkdir -p {model_dir}")
    print(f"      wget -P {model_dir} {hf_url}")
    print("    Or from the whisper.cpp source directory:")
    print(f"      bash ./models/download-ggml-model.sh {model_name}")
    try:
        run_it = input("    Run wget download now? [y/N]: ").strip().lower()
    except EOFError:
        run_it = "n"
    if run_it == "y":
        dl_cmd = f"mkdir -p {model_dir} && wget -P {model_dir} {hf_url}"
        result = subprocess.run(dl_cmd, shell=True)  # noqa: S602
        if result.returncode == 0:
            print(f"    ✓ Model downloaded to {model_dir}")
        else:
            print(f"    ✗ Download failed (exit {result.returncode}). Run manually.")
    print()


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


def _load_existing(toml_path: Path) -> dict[str, Any]:
    """Return a flat dict of current values from settings.toml (best-effort)."""
    if not toml_path.exists():
        return {}
    try:
        doc = tomlkit.parse(toml_path.read_text(encoding="utf-8"))
        flat: dict[str, Any] = {}
        for section in doc.values():
            if isinstance(section, dict):
                flat.update(section)
        return flat
    except Exception:
        return {}


def run_wizard(toml_path: Path = TOML_PATH) -> None:
    """Run the interactive setup wizard and write answers to toml_path."""
    existing = _load_existing(toml_path)

    def _cur(key: str, fallback: Any = "") -> Any:
        return existing.get(key, fallback)

    print("\n" + "=" * 60)
    print("Claude Code Telegram Bot — Setup")
    print("=" * 60)
    if existing:
        print("Existing config loaded. Press Enter to keep current values.\n")
    else:
        print("Press Enter to accept defaults shown in [brackets].\n")

    def ask(prompt: str, default: str = "", secret: bool = False) -> str:
        suffix = f" [{default}]" if default else ""
        if secret and default:
            suffix = " [****]"
        try:
            value = input(f"  {prompt}{suffix}: ").strip()
        except EOFError:
            return default
        return value or default

    def ask_yn(prompt: str, default: bool = True) -> bool:
        suffix = "[Y/n]" if default else "[y/N]"
        try:
            value = input(f"  {prompt} {suffix}: ").strip().lower()
        except EOFError:
            return default
        return value.startswith("y") if value else default

    def ask_choice(prompt: str, choices: list[tuple[str, str]], default_id: str) -> str:
        print(f"\n  {prompt}")
        default_idx = 1
        for i, (cid, desc) in enumerate(choices, 1):
            marker = " (default)" if cid == default_id else ""
            print(f"    {i}) {cid} — {desc}{marker}")
            if cid == default_id:
                default_idx = i
        try:
            raw = input(f"  Choice [{default_idx}]: ").strip()
        except EOFError:
            return default_id
        if not raw:
            return default_id
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(choices):
                return choices[idx][0]
        except ValueError:
            # Accept the value directly if it matches a choice ID
            for cid, _ in choices:
                if raw == cid:
                    return raw
        print(f"  Invalid choice, using default: {default_id}")
        return default_id

    def section(title: str) -> None:
        print(f"\n──── {title} {'─' * max(0, 54 - len(title))}")

    # ── Required ──────────────────────────────────────────────────────────────

    section("Required")
    token = ask("Telegram bot token (from @BotFather)", _cur("telegram_bot_token"), secret=True)
    username = ask("Bot username (without @)", _cur("telegram_bot_username"))

    cur_ids = _cur("allowed_users", [])
    cur_ids_str = ",".join(str(i) for i in cur_ids) if cur_ids else ""
    ids_raw = ask("Your Telegram user ID (comma-separated for multiple)", cur_ids_str)

    # ── Workspace & Claude ─────────────────────────────────────────────────────

    section("Workspace & Claude")
    workspace = ask(
        "Workspace directory (Claude's sandbox root)",
        _cur("approved_directory", str(Path.home() / "projects")),
    )
    api_key = ask(
        "Anthropic API key (optional if Claude CLI is logged in, Enter to skip)",
        _cur("anthropic_api_key"),
        secret=True,
    )

    model = ask_choice(
        "Claude model:",
        _MODEL_CHOICES,
        _cur("claude_model", "claude-sonnet-4-5"),
    )

    # ── Features ──────────────────────────────────────────────────────────────

    section("Features")
    print("  (Enter = keep current / accept default)")
    agentic_mode = ask_yn("Agentic mode — conversational AI (recommended)", _cur("agentic_mode", True))
    enable_mcp = ask_yn("MCP tools — connect external tools (requires config file)", _cur("enable_mcp", False))
    enable_memory = ask_yn("Memory — remember facts across conversations", _cur("enable_memory", False))
    enable_git = ask_yn("Git integration — run git commands, inject repo context", _cur("enable_git_integration", True))
    enable_files = ask_yn("File uploads — accept files directly in chat", _cur("enable_file_uploads", True))
    enable_quick = ask_yn("Quick actions — show shortcut buttons after responses", _cur("enable_quick_actions", True))
    enable_threads = ask_yn("Project threads — route by Telegram forum topic", _cur("enable_project_threads", False))
    enable_checkins = ask_yn(
        "Check-ins — proactive check-ins via Claude (requires scheduler)",
        _cur("enable_checkins", False),
    )
    dev_mode = ask_yn("Development mode — verbose debug output, dev features", _cur("development_mode", False))

    # ── Output ────────────────────────────────────────────────────────────────

    section("Output verbosity")
    print("  0 = quiet (final response only)")
    print("  1 = normal (show tool names)  [default]")
    print("  2 = detailed (show tool inputs)")
    verbose_raw = ask("Verbosity level", str(_cur("verbose_level", 1)))
    try:
        verbose_level = max(0, min(2, int(verbose_raw)))
    except ValueError:
        verbose_level = 1

    # ── Personalization ───────────────────────────────────────────────────────

    section("Personalization")
    user_name = ask("Your name (how Claude addresses you; Enter to skip)", _cur("user_name", ""))
    user_timezone = ask("Timezone (e.g. Europe/Berlin, US/Eastern, UTC)", _cur("user_timezone", "UTC"))
    user_profile = ask(
        "Profile path (markdown injected into Claude's context; Enter for default)",
        _cur("user_profile_path", ""),
    )

    # ── Voice ─────────────────────────────────────────────────────────────────

    section("Voice transcription")
    print("  Send voice messages and have them transcribed before Claude processes them.")
    voice_provider = ask_choice(
        "Voice provider:",
        _VOICE_CHOICES,
        _cur("voice_provider", ""),
    )
    whisper_binary = ""
    whisper_model_path = ""
    if voice_provider == "local":
        # Detect current language from existing model path (e.g. ggml-base.en.bin → English)
        _cur_model_path = _cur("whisper_model_path", "")
        _cur_stem = Path(_cur_model_path).stem.replace("ggml-", "") if _cur_model_path else ""
        _cur_is_en = _cur_stem.endswith(".en") if _cur_stem else True

        lang = ask_choice(
            "Primary language:",
            [
                ("en", "English only — smaller model, better accuracy for English"),
                ("multi", "Multilingual — supports 99 languages"),
            ],
            "en" if _cur_is_en else "multi",
        )
        model_list = _WHISPER_MODELS_EN if lang == "en" else _WHISPER_MODELS_MULTI
        model_choices = [(name, f"{size} — {desc}") for name, size, desc in model_list]
        _default_model = (
            _cur_stem
            if _cur_stem and any(n == _cur_stem for n, _, _ in model_list)
            else model_list[1][0]  # default: base / base.en
        )
        whisper_model = ask_choice("Whisper model:", model_choices, _default_model)

        _cur_model_dir = (
            str(Path(_cur_model_path).expanduser().parent) if _cur_model_path else str(_WHISPER_DEFAULT_DIR)
        )
        model_dir_raw = ask("Model directory", _cur_model_dir)
        whisper_model_path = str(Path(model_dir_raw).expanduser() / f"ggml-{whisper_model}.bin")
        print(f"  → Model path: {whisper_model_path}")

        # Auto-detect binary; offer brew install inline so the path can be pre-filled
        _found_binary = shutil.which("whisper-cli") or shutil.which("whisper-cpp")
        if not _found_binary and shutil.which("brew"):
            print("\n  whisper-cli not found. Install via Homebrew?")
            print("    brew install whisper-cpp")
            try:
                do_install = input("  Install now? [Y/n]: ").strip().lower()
            except EOFError:
                do_install = "y"
            if do_install != "n":
                result = subprocess.run("brew install whisper-cpp", shell=True)  # noqa: S602
                if result.returncode == 0:
                    _found_binary = shutil.which("whisper-cli") or shutil.which("whisper-cpp")
                    if _found_binary:
                        print(f"  ✓ Installed at: {_found_binary}")
                else:
                    print("  ✗ Install failed. Enter binary path manually below.")
        _binary_default = _found_binary or _cur("whisper_binary", "whisper-cli")
        whisper_binary = ask("Path to whisper-cli binary", _binary_default)

    # ── Write ─────────────────────────────────────────────────────────────────

    if toml_path.exists():
        doc = tomlkit.parse(toml_path.read_text(encoding="utf-8"))
    else:
        from src.config.toml_template import _TEMPLATE

        doc = tomlkit.parse(_TEMPLATE)

    def _set(section_name: str, key: str, value: object) -> None:
        if section_name not in doc:
            doc.add(section_name, tomlkit.table())
        doc[section_name][key] = value  # type: ignore[index]

    _set("required", "telegram_bot_token", token)
    _set("required", "telegram_bot_username", username)

    if ids_raw:
        try:
            ids = [int(x.strip()) for x in ids_raw.split(",") if x.strip()]
            _set("required", "allowed_users", ids)
        except ValueError:
            print(f"  Warning: could not parse user IDs '{ids_raw}', skipping.")

    _set("sandbox", "approved_directory", workspace)
    if api_key:
        _set("claude", "anthropic_api_key", api_key)
    _set("claude", "claude_model", model)

    _set("features", "agentic_mode", agentic_mode)
    _set("features", "enable_mcp", enable_mcp)
    _set("memory", "enable_memory", enable_memory)
    _set("features", "enable_git_integration", enable_git)
    _set("features", "enable_file_uploads", enable_files)
    _set("features", "enable_quick_actions", enable_quick)
    _set("projects", "enable_project_threads", enable_threads)
    _set("checkins", "enable_checkins", enable_checkins)
    _set("development", "development_mode", dev_mode)
    _set("output", "verbose_level", verbose_level)

    if user_name:
        _set("personalization", "user_name", user_name)
    _set("personalization", "user_timezone", user_timezone)
    if user_profile:
        _set("personalization", "user_profile_path", user_profile)

    if voice_provider is not None:
        _set("voice", "voice_provider", voice_provider)
    if whisper_binary:
        _set("voice", "whisper_binary", whisper_binary)
    if whisper_model_path:
        _set("voice", "whisper_model_path", whisper_model_path)

    _set("onboarding", "setup_completed", True)

    toml_path.parent.mkdir(parents=True, exist_ok=True)
    toml_path.write_text(tomlkit.dumps(doc), encoding="utf-8")

    print(f"\n{'=' * 60}")
    print(f"Configuration written to {toml_path}")

    check_dependencies(
        voice_provider=voice_provider,
        whisper_binary=whisper_binary or "whisper-cli",
        whisper_model_path=whisper_model_path,
        enable_git=enable_git,
    )

    print(f"{'=' * 60}")
    print("Edit settings.toml any time, or use /settings in Telegram.\n")


def main() -> None:
    """CLI entry point: claude-telegram-setup."""
    from src.config.toml_template import ensure_toml_config

    ensure_toml_config()
    run_wizard()
    sys.exit(0)
