"""Settings registry — declarative field definitions for the /settings UI.

Extracted from settings_ui.py to keep that file under 400 lines.
"""

from __future__ import annotations

from typing import Any

_FieldDef = dict[str, Any]
_CategoryDef = dict[str, Any]

# ── Model choices ──────────────────────────────────────────────────────────────

MODEL_CHOICES: dict[str, str] = {
    "opus": "claude-opus-4-6",
    "sonnet": "claude-sonnet-4-6",
    "sonnet45": "claude-sonnet-4-5",
    "haiku": "claude-haiku-4-5",
    "opus3": "claude-3-opus-20240229",
    "sonnet3": "claude-3-5-sonnet-20241022",
    "haiku3": "claude-3-5-haiku-20241022",
}

LOG_LEVEL_CHOICES: dict[str, str] = {
    "DEBUG": "DEBUG",
    "INFO": "INFO",
    "WARNING": "WARNING",
    "ERROR": "ERROR",
}

VOICE_PROVIDER_CHOICES: dict[str, str] = {
    "groq": "groq",
    "local": "local",
    "none": "",
}

PROJECT_THREAD_MODE_CHOICES: dict[str, str] = {
    "private": "private",
    "group": "group",
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
    "auth": {
        "label": "🔑 Auth",
        "fields": {
            "enable_token_auth": {
                "label": "Token auth",
                "type": "bool",
                "env_key": "ENABLE_TOKEN_AUTH",
            },
        },
    },
    "storage": {
        "label": "💾 Storage",
        "fields": {
            "session_timeout_minutes": {
                "label": "Session timeout (min)",
                "type": "int",
                "min": 10,
                "max": 1440,
                "step": 30,
                "env_key": "SESSION_TIMEOUT_MINUTES",
            },
            "max_sessions_per_user": {
                "label": "Max sessions/user",
                "type": "int",
                "min": 1,
                "max": 20,
                "step": 1,
                "env_key": "MAX_SESSIONS_PER_USER",
            },
        },
    },
    "monitoring": {
        "label": "📊 Monitoring",
        "fields": {
            "log_level": {
                "label": "Log level",
                "type": "choice",
                "choices": LOG_LEVEL_CHOICES,
                "env_key": "LOG_LEVEL",
            },
            "enable_telemetry": {
                "label": "Telemetry",
                "type": "bool",
                "env_key": "ENABLE_TELEMETRY",
            },
        },
    },
    "development": {
        "label": "🛠️ Development",
        "fields": {
            "debug": {
                "label": "Debug mode",
                "type": "bool",
                "env_key": "DEBUG",
            },
            "development_mode": {
                "label": "Development mode",
                "type": "bool",
                "env_key": "DEVELOPMENT_MODE",
            },
        },
    },
    "api": {
        "label": "🌐 API/Webhooks",
        "fields": {
            "enable_api_server": {
                "label": "API server",
                "type": "bool",
                "env_key": "ENABLE_API_SERVER",
            },
            "api_server_port": {
                "label": "API port",
                "type": "int",
                "min": 1024,
                "max": 65535,
                "step": 1,
                "env_key": "API_SERVER_PORT",
            },
            "enable_scheduler": {
                "label": "Scheduler",
                "type": "bool",
                "env_key": "ENABLE_SCHEDULER",
            },
        },
    },
    "projects": {
        "label": "📁 Projects",
        "fields": {
            "enable_project_threads": {
                "label": "Project threads",
                "type": "bool",
                "env_key": "ENABLE_PROJECT_THREADS",
            },
            "project_threads_mode": {
                "label": "Threads mode",
                "type": "choice",
                "choices": PROJECT_THREAD_MODE_CHOICES,
                "env_key": "PROJECT_THREADS_MODE",
            },
        },
    },
    "personal": {
        "label": "👤 Personal",
        "fields": {
            "user_name": {
                "label": "Name",
                "type": "display",
                "env_key": "USER_NAME",
            },
            "user_timezone": {
                "label": "Timezone",
                "type": "display",
                "env_key": "USER_TIMEZONE",
            },
            "user_profile_path": {
                "label": "Profile path",
                "type": "display",
                "env_key": "USER_PROFILE_PATH",
            },
        },
    },
    "voice": {
        "label": "🎤 Voice",
        "fields": {
            "voice_provider": {
                "label": "Voice provider",
                "type": "choice",
                "choices": VOICE_PROVIDER_CHOICES,
                "env_key": "VOICE_PROVIDER",
            },
        },
    },
    "sandbox_paths": {
        "label": "🗂️ Sandbox",
        "fields": {
            "allowed_paths": {
                "label": "Allowed paths",
                "type": "display",
                "env_key": "ALLOWED_PATHS",
            },
            "approved_directory": {
                "label": "Workspace dir",
                "type": "display",
                "env_key": "APPROVED_DIRECTORY",
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
