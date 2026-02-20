"""Build rich status dashboard for the bot owner.

Non-owner status is a single line produced inline in agentic_status().
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

from ..config.settings import Settings
from .utils.html_format import escape_html


def _uptime(start_monotonic: float) -> str:
    """Format uptime from a monotonic start timestamp."""
    elapsed = int(time.monotonic() - start_monotonic)
    h, rem = divmod(elapsed, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _model_short(model: str) -> str:
    """Shorten a model ID for display."""
    aliases = {
        "claude-opus-4-6": "opus-4-6",
        "claude-sonnet-4-6": "sonnet-4-6",
        "claude-sonnet-4-5": "sonnet-4-5",
        "claude-haiku-4-5": "haiku-4-5",
        "claude-opus-4-5": "opus-4-5",
    }
    return aliases.get(model, model.replace("claude-", ""))


async def build_owner_status(
    settings: Settings,
    storage: Any | None,
    rate_limiter: Any | None,
    user_id: int,
    current_dir: Path,
    start_monotonic: float,
    version: str,
) -> str:
    """Return HTML dashboard string for the bot owner."""
    lines: list[str] = ["<b>Bot Status</b>"]

    # ── System ────────────────────────────────────────────────────────────────
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}"
    uptime = _uptime(start_monotonic)
    model = _model_short(settings.claude_model)
    lines.append(
        f"\n<b>System</b>\n"
        f"  Uptime: {uptime} · Version: {version}\n"
        f"  Model: <code>{escape_html(model)}</code> · Python {py_ver}"
    )

    # ── Session / cost ─────────────────────────────────────────────────────────
    cost_str = "n/a"
    if rate_limiter:
        try:
            user_status = rate_limiter.get_user_status(user_id)
            cost_usage = user_status.get("cost_usage", {})
            current_cost = cost_usage.get("current", 0.0)
            cost_str = f"${current_cost:.3f}"
        except Exception:
            pass
    lines.append(f"\n<b>Your Cost</b>\n  {cost_str}")

    # ── Global totals ──────────────────────────────────────────────────────────
    if storage:
        try:
            dashboard = await storage.get_admin_dashboard()
            total_users = dashboard.get("total_users", "?")
            total_messages = dashboard.get("total_messages", "?")
            total_cost = dashboard.get("total_cost", 0.0)
            lines.append(
                f"\n<b>Totals</b>\n  Users: {total_users} · Messages: {total_messages}\n  Total cost: ${total_cost:.3f}"
            )
        except Exception:
            pass

    # ── Config ─────────────────────────────────────────────────────────────────
    sandbox_status = "on" if settings.sandbox_enabled else "off"
    mcp_status = "on" if settings.enable_mcp else "off"
    if settings.enable_mcp and settings.mcp_config_path:
        try:
            import json

            mcp_data = json.loads(settings.mcp_config_path.read_text())
            server_count = len(mcp_data.get("mcpServers", {}))
            mcp_status = f"on ({server_count} servers)"
        except Exception:
            pass

    verbose_labels = {0: "quiet", 1: "normal", 2: "detailed"}
    verbose_str = verbose_labels.get(settings.verbose_level, str(settings.verbose_level))
    mode = "agentic" if settings.agentic_mode else "classic"

    allowed_paths = settings.all_allowed_paths
    paths_str = ", ".join(str(p) for p in allowed_paths)
    if len(paths_str) > 80:
        paths_str = paths_str[:77] + "..."

    lines.append(
        f"\n<b>Config</b>\n"
        f"  Mode: {mode} · Sandbox: {sandbox_status} · MCP: {mcp_status}\n"
        f"  Verbose: {verbose_str}\n"
        f"  Paths: <code>{escape_html(paths_str)}</code>"
    )

    # ── Working directory ──────────────────────────────────────────────────────
    lines.append(f"\n<b>Directory</b>\n  <code>{escape_html(str(current_dir))}</code>")

    return "\n".join(lines)
