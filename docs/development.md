# Development Guide

## Getting Started

### Prerequisites

- **Python 3.11+** -- [Download here](https://www.python.org/downloads/)
- **uv** -- `curl -LsSf https://astral.sh/uv/install.sh | sh`
- **Claude authentication** -- one of:
  - Claude Code CLI installed and authenticated (`claude auth login`)
  - Anthropic API key (`ANTHROPIC_API_KEY=sk-ant-...`)

### Initial Setup

```bash
git clone https://github.com/talpah/claude-code-telegram.git
cd claude-code-telegram
make dev         # installs all deps via uv (including dev/test extras)
cp .env.example .env
# Edit .env with your development settings
```

`make dev` runs `uv sync --extra dev`, which installs the project and all
optional dev dependencies into a project-local `.venv`.

## Development Workflow

```bash
make test          # pytest with coverage
make lint          # ruff check + ruff format --check + ty check
make format        # ruff format + ruff check --fix (auto-fix safe issues)
make run-debug     # run bot with DEBUG=true + console logging

# Run a single test
uv run pytest tests/unit/test_config.py -k test_name -v

# Type checking only
uv run ty check src

# nox (isolated Python-version matrix)
uv run nox              # lint + typecheck + tests (3.11, 3.12)
uv run nox -s lint
uv run nox -s tests-3.12
```

## Package Structure

```
src/
├── config/           # Pydantic Settings v2 + feature flags
│   ├── settings.py   # All env-var settings
│   ├── features.py   # FeatureFlags helper
│   ├── loader.py     # Environment detection and loading
│   ├── environments.py # Per-environment overrides
│   └── profile.py    # ProfileManager (mtime-cached markdown profile)
├── bot/              # Telegram bot
│   ├── core.py       # ClaudeCodeBot, middleware wiring
│   ├── orchestrator.py # MessageOrchestrator (agentic + classic routing)
│   ├── handlers/     # Command and message handlers (classic mode)
│   ├── middleware/   # Auth, rate-limit, security middleware
│   └── features/     # Git, file upload, quick actions, voice, session export
├── claude/           # Claude integration
│   ├── facade.py     # ClaudeIntegration façade (prompt enrichment)
│   ├── sdk_integration.py # Primary SDK backend
│   ├── integration.py # Legacy CLI subprocess backend
│   ├── session.py    # SessionManager + SQLite-backed persistence
│   └── monitor.py    # ToolMonitor (allowlist enforcement)
├── memory/           # Semantic memory
│   ├── manager.py    # MemoryManager (store/search/process)
│   └── embeddings.py # EmbeddingService (sentence-transformers, lazy-loaded)
├── storage/          # SQLite persistence
│   ├── database.py   # Schema + migrations (versioned)
│   ├── models.py     # Typed dataclass models
│   ├── repositories.py # Repository pattern data access
│   └── facade.py     # Storage façade
├── security/         # Auth and security
│   ├── auth.py       # AuthenticationManager + providers
│   ├── validators.py # SecurityValidator (injection/path traversal)
│   ├── rate_limiter.py # Token bucket rate limiter
│   └── audit.py      # AuditLogger + SQLiteAuditStorage
├── events/           # Event bus
│   ├── bus.py        # EventBus (async pub/sub)
│   ├── types.py      # Typed event dataclasses
│   ├── handlers.py   # AgentHandler (events → Claude)
│   └── middleware.py # EventSecurityMiddleware
├── api/              # FastAPI webhook server
├── scheduler/        # APScheduler cron jobs + check-ins
│   ├── scheduler.py  # JobScheduler
│   └── checkin.py    # CheckInService (proactive messages)
├── notifications/    # Rate-limited Telegram delivery
├── projects/         # Multi-project thread routing
└── main.py           # Entry point: wires all components
```

## Code Standards

### Toolchain

| Tool | Purpose | Command |
|------|---------|---------|
| **ruff** | Linting + formatting (replaces black/isort/flake8) | `uv run ruff check src tests` |
| **ty** | Static type checking (replaces mypy) | `uv run ty check src` |
| **pytest** | Test runner | `uv run pytest tests/` |
| **nox** | Multi-Python test matrix | `uv run nox` |

Line length: **120 chars**. Rules: E, W, F, I (isort), UP (pyupgrade).

### Type Hints

Python 3.11+ syntax -- use built-in generics and union syntax:

```python
# Good
def process(items: list[str], config: Settings | None = None) -> dict[str, int]: ...

# Avoid (legacy)
from typing import Optional, List, Dict
def process(items: List[str], config: Optional[Settings] = None) -> Dict[str, int]: ...
```

### Datetime Convention

Always use timezone-aware UTC:

```python
from datetime import UTC, datetime

now = datetime.now(UTC)   # correct
now = datetime.utcnow()   # deprecated -- do not use
```

### Logging

Use `structlog` throughout (JSON in prod, console in dev):

```python
import structlog
logger = structlog.get_logger()

logger.info("Session started", user_id=user_id, directory=str(directory))
logger.error("Claude call failed", error=str(e), session_id=session_id)
```

## Testing

### Running Tests

```bash
uv run pytest tests/ -v                      # all tests
uv run pytest tests/unit/ -v                 # unit tests only
uv run pytest tests/ --cov=src --cov-report=term-missing
```

### Test Config Helper

```python
from src.config import create_test_config

config = create_test_config(debug=True, enable_memory=True)
```

`create_test_config()` produces an in-memory, fully valid `Settings` object
without touching the filesystem or `.env` files.

### Async Tests

The project uses `pytest-asyncio` with `asyncio_mode = "auto"` (set in
`pyproject.toml`). All `async def test_*` functions run without a decorator:

```python
async def test_something():
    result = await async_function()
    assert result is not None
```

### Fixtures

Prefer fixtures over setup/teardown. Use `AsyncMock` for async dependencies:

```python
from unittest.mock import AsyncMock, MagicMock
import pytest

@pytest.fixture
def auth_manager():
    m = MagicMock()
    m.is_authenticated.return_value = True
    m.get_session.return_value = MagicMock(auth_provider="whitelist")
    return m

@pytest.fixture
def storage():
    return AsyncMock()   # async methods automatically return awaitables
```

## Common Development Tasks

### Adding a New Agentic Command

1. Add handler method to `MessageOrchestrator` in `src/bot/orchestrator.py`
2. Register it in `_register_agentic_handlers()`
3. Add to `get_bot_commands()` for Telegram autocomplete
4. Add audit logging

### Adding a Configuration Setting

1. Add field to `Settings` in `src/config/settings.py`
2. Add to `.env.example` with a comment
3. Write tests in `tests/unit/test_config.py`
4. Update `docs/configuration.md`

### Adding a Database Migration

Migrations live in `src/storage/database.py` as numbered SQL blocks. Increment
the schema version constant and append the migration SQL. Migrations run once on
startup and are idempotent (`CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`).

### Debugging Configuration

```bash
# Print all resolved settings
uv run python -c "from src.config import load_config; import json; c = load_config(); print(c.model_dump_json(indent=2))"

# Check which features are active
uv run python -c "from src.config import load_config, FeatureFlags; f = FeatureFlags(load_config()); print(f.get_enabled_features())"
```

## Contributing

1. Fork the repo, create a feature branch
2. Make changes with tests (`make test && make lint`)
3. Conventional commit message (`feat:`, `fix:`, `refactor:`, `docs:`, `test:`, `chore:`)
4. Submit a Pull Request

**Code standards:** Python 3.11+, ruff (120 chars), type hints required, `ty` clean, pytest coverage maintained.
