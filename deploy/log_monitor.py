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
MAX_PRIORITY = 4  # WARNING and above

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


def normalize(msg: str) -> str:
    for pat in _STRIP:
        msg = pat.sub("<X>", msg)
    return re.sub(r"\s+", " ", msg).strip().lower()


def err_hash(normalized: str) -> str:
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


# ── Config helpers ────────────────────────────────────────────────────────────

def _read_env() -> dict[str, str]:
    env: dict[str, str] = {}
    env_file = PROJECT_DIR / ".env"
    if not env_file.exists():
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
    r"listening|connected|heartbeat|ping|typing",
    re.I,
)


def classify(line: str) -> str:
    """Return CRITICAL, TODO, or IGNORE via Claude Haiku (heuristic fallback)."""
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
                    "  TODO     — needs attention, not immediately urgent\n"
                    "  IGNORE   — expected behaviour, informational, noise\n\n"
                    f"Log: {line[:500]}"
                ),
            }],
        )
        verdict = resp.content[0].text.strip().upper()
        return verdict if verdict in ("CRITICAL", "TODO", "IGNORE") else "TODO"
    except Exception as exc:
        _log(f"warn: Claude classify failed: {exc}")
        return _heuristic_classify(line)


def _heuristic_classify(line: str) -> str:
    if _HEURISTIC_IGNORE.search(line):
        return "IGNORE"
    if _HEURISTIC_CRITICAL.search(line):
        return "CRITICAL"
    return "TODO"


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


def _parse_journal_line(raw: str) -> str | None:
    """Return the MESSAGE string or None if line is unparseable."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        obj = json.loads(raw)
        return obj.get("MESSAGE") or None
    except json.JSONDecodeError:
        return raw if raw else None


# ── Main ──────────────────────────────────────────────────────────────────────

def _log(msg: str) -> None:
    print(f"[log-monitor {datetime.now(UTC).strftime('%H:%M:%S')}] {msg}", flush=True)


def process_line(line: str, state: dict[str, Any]) -> None:
    norm = normalize(line)
    h = err_hash(norm)
    now = time.time()
    seen: dict[str, Any] = state.setdefault("seen", {})

    if h in seen:
        entry = seen[h]
        entry["count"] = entry.get("count", 1) + 1
        entry["last_seen_ts"] = now
        entry["last_seen"] = datetime.now(UTC).isoformat()
        append_error_log(entry.get("severity", "TODO"), line, norm, h)
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
                    msg = _parse_journal_line(raw)
                    if msg:
                        process_line(msg, state)

                if maybe_send_summary(state):
                    _log("Summary sent")

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
