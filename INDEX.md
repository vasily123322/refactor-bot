# refactor_bot — Index

Channel AI / Telegram posting bot (Hermes-like channel features). Read this before searching.

> **Текущий план работ:** [`docs/AUDIT_2026-09.md`](docs/AUDIT_2026-09.md) — аудит, найденные дефекты (CI/тесты/сборка Studio) и приоритезированный чеклист. Выполнять по порядку, отмечать чекбоксы.

## Identity

| | |
|---|---|
| Path | `/home/refactor_bot` |
| Product | Telegram bot for channels: posting, grab sources, AI generation, editor |
| Entry | `python -m app.bot.dispatcher` |
| Service | `refactor-bot.service` |
| Branch | `master` |

## Production (this host)

| Layer | Where |
|---|---|
| Bot process | systemd `refactor-bot` · cwd `/home/refactor_bot` · `.venv` |
| Code | `app/` package |
| Data / sessions | `data/`, `userbot.session` (secrets — do not commit) |
| Logs | `logs/`, journald |

## Folder Map

| Path | Purpose |
|---|---|
| `app/bot/` | Aiogram dispatcher, routers, keyboards, FSM |
| `app/core/` | Config, DB, runner, logging |
| `app/domain/` | Models / UI settings |
| `app/repositories/` | Persistence |
| `app/services/` | Posting, scheduling, AI, HTTP, extractors |
| `app/services/llm/` | **AI feature modules** (profiles, memory, prompts) |
| `app/services/ai_generation.py` | Unified LLM pipeline |
| `app/userbot/` | Telethon listener / client |
| `app/workers/` | Scheduler, grab poll, AI auto tasks |
| `alembic/` | Migrations |
| `tests/` | pytest (esp. LLM feature tests) |
| `specs/` | Speckit + module specs |
| `scripts/` | One-off ops |

## Canonical Files (AI / channel settings)

| File | Purpose |
|---|---|
| `AGENTS.md` | Agent rules |
| `app/services/ai_generation.py` | `AIGenerationService(session)` only — no `ai_repo=` ctor arg |
| `app/services/llm/channel_memory.py` | Channel memory = `filters["memory"]` **nested dict** |
| `app/services/llm/model_profiles.py` | `filters["ai_model_profile"]` = economy/balanced/quality/custom |
| `app/services/llm/publication_profiles.py` | `filters["publication_profile"]` = default/news/sales/analysis/meme |
| `app/services/llm/ai_skills.py` | AI skills/presets wiring |
| `app/services/llm/draft_editor.py` | AI editor |
| `app/bot/dispatcher.py` | Process entry |
| `app/core/config.py` | Settings |
| `app/domain/models.py` | ORM models |
| `.cursor/AI_RULES.md` | Safe edit rules |
| `tests/test_llm_*.py` | Behavior locks for AI features |

## Where To Go

| Task | Open first |
|---|---|
| AI generation pipeline | `ai_generation.py` + `prompt_builder.py` + `openrouter_client.py` |
| Channel memory | `channel_memory.py` + `tests/test_llm_channel_memory.py` |
| Model quality profiles | `model_profiles.py` + `tests/test_ai_model_profiles.py` |
| Publication profiles | `publication_profiles.py` + UI tests |
| Bot screens / settings | `app/bot/routers/settings.py`, related routers |
| Grab sources / auto tasks | `app/workers/`, `app/services/` |
| Restart prod | `systemctl restart refactor-bot.service` |

## Do not treat as source of truth

| Path | Why |
|---|---|
| `AI_FINAL_SUMMARY.md`, `AI_GENERATION_*.md`, `AI_SETUP.md` | Overlapping docs; **code + tests** win |
| `venv/` vs `.venv/` | Prefer **`.venv`** (systemd uses it) |
| `data/`, `*.session`, `.env` | Runtime secrets |
| `var/`, caches | Generated |
| Specs under `specs/001-auto-refactor-pr` | Historical planning unless task says so |

## Update rule

If AI filter keys, service ctors, or systemd entry change — update this file in the same change.
