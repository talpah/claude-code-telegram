#!/usr/bin/env python3
"""
Log monitor for claude-telegram-bot + claude-watchdog.

Watches journald output, classifies new error patterns with Claude Haiku,
sends immediate Telegram alerts on first occurrence, and posts a 2-hour
summary of recurring/new errors.

State: ~/.claude-code-telegram/monitor_state.json
Logs:  ~/.claude-code-telegram/errors_YYYY-MM-DD.txt
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import select
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# ── Paths & constants ─────────────────────────────────────────────────────────

PROJECT_DIR = Path(__file__).parent.parent
DATA_DIR = Path.home() / ".claude-code-telegram"
STATE_FILE = DATA_DIR / "monitor_state.json"

WATCHED_UNITS = ["claude-telegram-bot", "claude-telegram-watchdog"]
SUMMARY_INTERVAL = 2 * 60 * 60   # 2 hours between summaries
SELECT_TIMEOUT = 30               # seconds; summary checked at least this often
CLASSIFY_MODEL = "claude-haiku-4-5"

# journald priority: 0=emerg … 4=warning, 5=notice, 6=info, 7=debug
# The bot writes structured JSON to stdout; journald assigns all stdout lines
# PRIORITY=6 (info) regardless of the embedded "level" field.  We must capture
# at INFO level and then filter by the JSON "level" field ourselves.
MAX_PRIORITY = 6  # capture INFO and above, filter by embedded level below

# Minimum structlog level to process (case-insensitive)
_PROCESS_LEVELS = frozenset({"warning", "warn", "error", "critical"})

# ── Normalization & hashing ───────────────────────────────────────────────────

_STRIP = [
    re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I),
    re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.\d]+)?(?:Z|[+-]\d{2}:?\d{2})?\b"),
    re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),   # IPv4
    re.compile(r"session_[A-Za-z0-9_-]+"),
    re.compile(r"0x[0-9a-fA-F]+"),                # hex addresses
    re.compile(r"\b\d+\b"),                        # bare numbers
    re.compile(r"/[^\s:,\"']+"),                   # file paths
]

# Metadata fields to strip from JSON when deduplicating (cascading errors)
_METADATA_FIELDS = {
    "user_id", "session_id", "timestamp", "logger", "level",
    "event", "first_seen", "last_seen", "count", "severity",
    "request_id", "trace_id", "span_id", "correlation_id",
}


def extract_core_error(msg: str) -> str:
    """Extract core error message from JSON, stripping metadata fields.
    
    For cascading errors (e.g. same error from 3 layers), extract the
    'error' field to group related errors together.
    """
    try:
        data = json.loads(msg)
        if isinstance(data, dict):
            # If there's an "error" field, prioritize that for deduplication
            if "error" in data:
                return str(data["error"])
            
            # Otherwise, rebuild JSON with only non-metadata fields
            core = {k: v for k, v in data.items() if k not in _METADATA_FIELDS}
            if core:
                return json.dumps(core, sort_keys=True)
    except (json.JSONDecodeError, TypeError):
        pass
    
    return msg


def normalize(msg: str) -> str:
    # First extract core error from JSON to group cascading errors
    msg = extract_core_error(msg)
    
    for pat in _STRIP:
        msg = pat.sub("<X>", msg)
    return re.sub(r"\s+", " ", msg).strip().lower()


def err_hash(normalized: str) -> str:
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


# ── Config helpers ────────────────────────────────────────────────────────────

def _read_env() -> dict[str, str]:
    # Priority: ~/.claude-code-telegram/config/.env → project-root .env
    candidates = [
        DATA_DIR / "config" / ".env",
        PROJECT_DIR / ".env",
    ]
    env_file = next((p for p in candidates if p.exists()), None)
    env: dict[str, str] = {}
    if not env_file:
        return env
    for line in env_file.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"')
    return env


def telegram_config() -> tuple[str, int] | tuple[None, None]:
    env = _read_env()
    token = env.get("TELEGRAM_BOT_TOKEN")
    raw_users = env.get("ALLOWED_USERS", "")
    chat_id: int | None = None
    for part in raw_users.split(","):
        part = part.strip()
        if part.isdigit():
            chat_id = int(part)
            break
    if token and chat_id:
        return token, chat_id
    return None, None


def anthropic_key() -> str | None:
    return _read_env().get("ANTHROPIC_API_KEY")


# ── State ─────────────────────────────────────────────────────────────────────

def load_state() -> dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"seen": {}, "last_summary_ts": 0.0}


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


# ── Telegram ──────────────────────────────────────────────────────────────────

def send_telegram(text: str) -> bool:
    import httpx

    token, chat_id = telegram_config()
    if not token or not chat_id:
        _log("warn: Telegram not configured — skipping notification")
        return False
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        return resp.status_code == 200
    except Exception as exc:
        _log(f"warn: Telegram send failed: {exc}")
        return False


# ── Classification ────────────────────────────────────────────────────────────

_HEURISTIC_CRITICAL = re.compile(
    r"fatal|crash|panic|segfault|oom|out of memory|security|"
    r"unauthorized|reverted|data.?loss|corrupt",
    re.I,
)
_HEURISTIC_IGNORE = re.compile(
    r"starting|started|stopping|stopped|reloading|loaded|"
    r"listening|connected|heartbeat|ping|typing|"
    r"shutdown|cleanup|initializ|"
    r"HTTP/1\.[01]|getUpdates|200 OK",
    re.I,
)

# Patterns that only matter if the service is still down after a grace period.
# On a normal restart the service recovers within seconds — ignore those.
_RECOVERY_CHECK_PATTERN = re.compile(r"bot is not running", re.I)
_RECOVERY_GRACE_SECONDS = 30

# Structured shutdown messages emitted by the bot before intentional restarts.
# When seen, suppress recovery-check alerts for _EXPECTED_SHUTDOWN_WINDOW seconds.
_EXPECTED_SHUTDOWN_PATTERN = re.compile(
    r"shutting down(?:\s+due\s+to)?[\s:]+(?:sigterm|user\s+requested|restart|config\s+change)",
    re.I,
)
_EXPECTED_SHUTDOWN_WINDOW = 120  # seconds after which suppression expires


def _is_service_active(unit: str) -> bool:
    """Return True if the systemd user unit is currently active."""
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", unit],
            capture_output=True,
            text=True,
            timeout=5,
            env={**os.environ, "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"},
        )
        return result.returncode == 0
    except Exception:
        return False  # assume down if check fails


def flush_recovery_checks(state: dict[str, Any]) -> None:
    """Fire alerts for 'not running' lines if the service is still down after grace period.

    Suppressed when an expected-shutdown marker was recorded recently (e.g. /reload,
    SIGTERM from config change) — those restarts are expected to recover on their own.
    """
    pending: list[dict[str, Any]] = state.get("pending_recovery_checks", [])
    if not pending:
        return

    now = time.time()
    remaining: list[dict[str, Any]] = []

    for check in pending:
        if now - check["ts"] < _RECOVERY_GRACE_SECONDS:
            remaining.append(check)
            continue

        # Grace period elapsed.  First check for expected-shutdown suppression.
        since_expected = now - state.get("last_expected_shutdown_ts", 0)
        if since_expected <= _EXPECTED_SHUTDOWN_WINDOW:
            _log(
                f"recovery: expected shutdown {int(since_expected)}s ago — "
                f"suppressing alert [{check['h']}]"
            )
            continue

        # Check if the service has already recovered on its own.
        unit = check.get("unit", WATCHED_UNITS[0])
        if _is_service_active(unit):
            _log(f"recovery: {unit} is active — suppressing alert [{check['h']}]")
            continue

        # Service still down and no expected shutdown: fire the alert.
        _log(f"recovery check: {unit} still down — alerting [{check['h']}]")
        line = check["line"]
        norm = check["norm"]
        h = check["h"]
        seen: dict[str, Any] = state.setdefault("seen", {})

        if h not in seen:
            severity = classify(line)
            if severity != "IGNORE":
                entry: dict[str, Any] = {
                    "first_seen": datetime.now(UTC).isoformat(),
                    "first_seen_ts": now,
                    "last_seen": datetime.now(UTC).isoformat(),
                    "last_seen_ts": now,
                    "count": 1,
                    "severity": severity,
                    "sample": line[:300],
                    "normalized": norm,
                }
                seen[h] = entry
                log_file = append_error_log(severity, line, norm, h)
                _log(f"new {severity} [{h}]: {line[:80]}")
                icon = "🔴" if severity == "CRITICAL" else "🟡"
                send_telegram(
                    f"{icon} <b>New {severity} error</b>\n\n"
                    f"<code>{line[:400]}</code>\n\n"
                    f"ID: <code>{h}</code>  •  📄 <code>{log_file}</code>"
                )

    state["pending_recovery_checks"] = remaining


def classify(line: str) -> str:
    """Return CRITICAL, WARNING, or IGNORE via Claude Haiku (heuristic fallback)."""
    api_key = anthropic_key()
    if not api_key:
        return _heuristic_classify(line)
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=CLASSIFY_MODEL,
            max_tokens=10,
            messages=[{
                "role": "user",
                "content": (
                    "You are classifying a Telegram bot service log line.\n"
                    "Reply with exactly one word:\n"
                    "  CRITICAL — service-breaking, security issue, data loss risk\n"
                    "  WARNING  — needs attention, not immediately urgent\n"
                    "  IGNORE   — expected behaviour, informational, noise\n\n"
                    f"Log: {line[:500]}"
                ),
            }],
        )
        verdict = resp.content[0].text.strip().upper()
        return verdict if verdict in ("CRITICAL", "WARNING", "IGNORE") else "WARNING"
    except Exception as exc:
        _log(f"warn: Claude classify failed: {exc}")
        return _heuristic_classify(line)


def _heuristic_classify(line: str) -> str:
    if _HEURISTIC_IGNORE.search(line):
        return "IGNORE"
    if _HEURISTIC_CRITICAL.search(line):
        return "CRITICAL"
    return "WARNING"


# ── Error log file ────────────────────────────────────────────────────────────

def append_error_log(severity: str, line: str, normalized: str, h: str) -> Path:
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    log_file = DATA_DIR / f"errors_{today}.txt"
    ts = datetime.now(UTC).isoformat()
    with log_file.open("a") as f:
        f.write(f"[{ts}] [{severity}] [{h}]\n{line}\n  norm: {normalized}\n\n")
    return log_file


# ── Summary ───────────────────────────────────────────────────────────────────

def maybe_send_summary(state: dict[str, Any]) -> bool:
    now = time.time()
    if now - state.get("last_summary_ts", 0) < SUMMARY_INTERVAL:
        return False

    seen: dict[str, Any] = state.get("seen", {})
    window_start = now - SUMMARY_INTERVAL
    recent = [(h, e) for h, e in seen.items() if e.get("last_seen_ts", 0) >= window_start]

    state["last_summary_ts"] = now

    if not recent:
        return False

    new_count = sum(1 for _, e in recent if e.get("first_seen_ts", 0) >= window_start)
    top5 = sorted(recent, key=lambda x: x[1].get("count", 0), reverse=True)[:5]

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    log_path = DATA_DIR / f"errors_{today}.txt"

    parts = ["<b>📊 Error Summary</b> — last 2 hours\n"]
    parts.append(f"• <b>{new_count}</b> new pattern(s)  •  <b>{len(recent)}</b> total active\n")

    if top5:
        parts.append("\n<b>Top recurring:</b>")
        for _, e in top5:
            icon = "🔴" if e.get("severity") == "CRITICAL" else "🟡"
            cnt = e.get("count", 1)
            sample = (e.get("sample") or "")[:100]
            parts.append(f"{icon} [{cnt}×] <code>{sample}</code>")

    parts.append(f"\n📄 <code>{log_path}</code>")
    send_telegram("\n".join(parts))
    return True


# ── Journal tail ──────────────────────────────────────────────────────────────

def _journal_cmd() -> list[str]:
    unit_args = [arg for u in WATCHED_UNITS for arg in ("-u", u)]
    return [
        "journalctl", "--user",
        "--follow", "--output=json",
        "--priority", str(MAX_PRIORITY),
        "-n", "0",
        *unit_args,
    ]


def _parse_journal_line(raw: str) -> tuple[str, str] | None:
    """Return (message_text, level) or None if line should be skipped.

    The bot writes structured JSON to stdout; journald wraps it in its own JSON
    envelope.  We parse the outer envelope to get MESSAGE, then attempt to parse
    MESSAGE itself as JSON to extract the embedded structlog "level" field.

    Returns None for unparseable lines or lines below the minimum log level.
    """
    raw = raw.strip()
    if not raw:
        return None
    try:
        outer = json.loads(raw)
        message = outer.get("MESSAGE") or ""
    except json.JSONDecodeError:
        message = raw

    if not message:
        return None

    # Ensure message is a string (journald sometimes includes non-string types)
    if not isinstance(message, str):
        try:
            message = json.dumps(message)
        except Exception:
            message = str(message)

    # Try to parse the bot's embedded JSON payload to extract log level
    level = "unknown"
    try:
        inner = json.loads(message)
        if isinstance(inner, dict):
            level_val = inner.get("level") or "unknown"
            # Handle case where level itself might be a list or other type
            if isinstance(level_val, str):
                level = level_val.lower()
            else:
                level = str(level_val).lower()
    except (json.JSONDecodeError, TypeError):
        pass  # plain-text message — pass through without level filtering

    # Structured bot log: only process warning / error / critical
    if level in _PROCESS_LEVELS:
        return message, level

    # Plain-text / third-party library line (httpx, etc.): only surface if it
    # looks like a hard failure — skip routine noise like "200 OK" poll lines.
    if level == "unknown":
        if _HEURISTIC_CRITICAL.search(message):
            return message, "unknown"
        return None

    # info / debug from structured logs → skip
    return None


# ── Main ──────────────────────────────────────────────────────────────────────

def _log(msg: str) -> None:
    print(f"[log-monitor {datetime.now(UTC).strftime('%H:%M:%S')}] {msg}", flush=True)


def process_line(line: str, state: dict[str, Any]) -> None:
    # Ensure line is a string (sometimes JSON fields are lists/dicts, not strings)
    if not isinstance(line, str):
        line = str(line)
    
    norm = normalize(line)
    h = err_hash(norm)
    now = time.time()
    seen: dict[str, Any] = state.setdefault("seen", {})

    # Expected-shutdown marker: record timestamp and skip alerting.
    if _EXPECTED_SHUTDOWN_PATTERN.search(line):
        state["last_expected_shutdown_ts"] = now
        _log(f"expected shutdown recorded: {line[:80]}")
        return

    # "Not running" warning: defer — only alert if service stays down after grace period.
    if _RECOVERY_CHECK_PATTERN.search(line):
        pending: list[dict[str, Any]] = state.setdefault("pending_recovery_checks", [])
        if not any(c["h"] == h for c in pending):
            pending.append({
                "ts": now,
                "h": h,
                "line": line,
                "norm": norm,
                "unit": WATCHED_UNITS[0],
            })
            _log(f"recovery: deferred check for [{h}]: {line[:60]}")
        return

    if h in seen:
        entry = seen[h]
        entry["count"] = entry.get("count", 1) + 1
        entry["last_seen_ts"] = now
        entry["last_seen"] = datetime.now(UTC).isoformat()
        append_error_log(entry.get("severity", "WARNING"), line, norm, h)
        return

    # New pattern — classify and notify
    severity = classify(line)
    if severity == "IGNORE":
        return

    entry: dict[str, Any] = {
        "first_seen": datetime.now(UTC).isoformat(),
        "first_seen_ts": now,
        "last_seen": datetime.now(UTC).isoformat(),
        "last_seen_ts": now,
        "count": 1,
        "severity": severity,
        "sample": line[:300],
        "normalized": norm,
    }
    seen[h] = entry

    log_file = append_error_log(severity, line, norm, h)
    _log(f"new {severity} [{h}]: {line[:80]}")

    icon = "🔴" if severity == "CRITICAL" else "🟡"
    send_telegram(
        f"{icon} <b>New {severity} error</b>\n\n"
        f"<code>{line[:400]}</code>\n\n"
        f"ID: <code>{h}</code>  •  📄 <code>{log_file}</code>"
    )


def run() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    state = load_state()
    if state.get("last_summary_ts", 0) == 0:
        state["last_summary_ts"] = time.time()

    _log(f"Started. Watching: {WATCHED_UNITS}")

    while True:
        proc = subprocess.Popen(
            _journal_cmd(),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            env={**os.environ, "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"},
        )
        assert proc.stdout is not None

        try:
            while proc.poll() is None:
                readable, _, _ = select.select([proc.stdout], [], [], SELECT_TIMEOUT)

                if readable:
                    raw = proc.stdout.readline()
                    if not raw:
                        break
                    parsed = _parse_journal_line(raw)
                    if parsed:
                        msg, _level = parsed
                        process_line(msg, state)

                if maybe_send_summary(state):
                    _log("Summary sent")

                flush_recovery_checks(state)
                save_state(state)

        except KeyboardInterrupt:
            proc.terminate()
            save_state(state)
            _log("Stopped.")
            sys.exit(0)
        except Exception as exc:
            _log(f"error in main loop: {exc}")
        finally:
            proc.terminate()

        _log("journalctl exited — restarting in 10s")
        time.sleep(10)
        state = load_state()


if __name__ == "__main__":
    run()
