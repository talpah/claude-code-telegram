# Handoff: claude-code-telegram
**Created:** 2026-02-19T142316 (pre-compaction)
**Branch:** main

## Compact Focus
(auto-compaction, no manual focus provided)

## Git State

### Recent commits
```
bcac937 feat: port voice transcription, semantic memory, check-ins, and user profile from claude-telegram-relay
858f0cd fix: resolve startup KeyError and FK constraint on first auth
73136eb fix: resolve all 463 ty type errors across the codebase
0c3c81f docs: update README to reflect fork status, uv/ruff/nox toolchain, Python 3.11+
ff5e422 feat: merge improvements from gitwithuli/claude-code-telegram
```

### Modified files (unstaged)
```
src/bot/orchestrator.py
uv.lock
```

### Staged files
```

```

### Untracked files
```
.claude/handoffs/claude-code-telegram-2026-02-19T124911.md
.claude/handoffs/claude-code-telegram-2026-02-19T130449.md
.claude/handoffs/claude-code-telegram-2026-02-19T132301.md
```

## Recent Commands
```
Feature 4 — Proactive check-ins (CheckInService):
- src/scheduler/checkin.py: APScheduler interval job, Claude decides YES/NO
- Uses active goals + hours-since-last-message as context for the decision
- Publishes AgentResponseEvent via EventBus → NotificationService → Telegram
- Activated with ENABLE_SCHEDULER=true + ENABLE_CHECKINS=true + NOTIFICATION_CHAT_IDS

Architecture:
- ClaudeIntegration.run_command() now enriches every prompt with profile + memory + time
- agentic_text() refactored into _run_agentic_prompt() shared helper (also used by voice)
- quick_query() added to facade for one-shot calls (check-in decisions) without session mgmt
- All features are opt-in and off by default; graceful degradation if deps missing
- Add httpx>=0.27, numpy>=1.26, sentence-transformers>=3.0 to dependencies

Fix pre-existing test failure:
- test_real_auth_middleware_rejection: storage mock was plain MagicMock; auth_middleware
  calls await storage.get_or_create_user() so the mock must be AsyncMock
EOF
)"
[2026-02-19T14:21:16Z] find /home/cosmin/Projects/claude-code-telegram/docs -type f | sort
[2026-02-19T14:22:38Z] grep -n "async def agentic_repo\|async def agentic_text\|async def _run_agentic" /home/cosmin/Projects/claude-code-telegram/src/bot/orchestrator.py
```

## Notes
_Auto-generated before context compaction. Verify current state with git diff._
