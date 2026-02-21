# User Profile

This file is injected into Claude's context at the start of every session.
Edit it to tell Claude about yourself, your preferences, and your workflow.

## About Me

- **Name:** Alex
- **Location:** Berlin, Germany
- **Timezone:** Europe/Berlin (GMT+1)
- **Hardware:** Ubuntu 24.04, 32GB RAM, NVIDIA GPU

## Communication Style

- **Concise by default** — direct answer first, details only if needed
- **No fluff** — skip filler phrases like "Great question!" or "Certainly!"
- **Tone:** casual and technical; skip basics unless asked
- **Language:** English

## Technical Profile

### Primary Stack
- **Python** (primary) — type hints, uv, ruff, pytest
- **TypeScript** (frontend)
- **Shell:** bash/zsh

### Tooling Preferences
- `uv` for Python package management
- `ruff` for linting + formatting
- `pytest` for tests (fixtures over setup/teardown, parametrize when possible)
- `docker compose` v2
- `pathlib.Path` over `os.path`
- `pydantic` over raw dicts for structured data

## Current Projects

- **my-saas-app** — SaaS backend (FastAPI + PostgreSQL + Redis)
- **data-pipeline** — ETL pipeline for analytics (Python, Airflow)

## Code Quality Standards

- Functions: max 50 lines; files: max 400 lines
- No dead code or commented-out blocks in commits
- Conventional commits: feat/fix/refactor/docs/test/chore
- Type hints on all functions

## Work Style

- Deep focus in mornings, async/review work in afternoons
- Prefer reading existing code before suggesting changes
- Test locally before pushing
