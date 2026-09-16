# AGENTS.md — refactor_bot

Rules for agents in `/home/refactor_bot`. Read `INDEX.md` first.

## Language

- Prefer **Russian** for user-facing ops updates.
- Code/commits: English conventional style is fine.

## What this is

Telegram channel bot with AI features similar to a “channel Hermes”:

- AI skills / presets  
- Channel memory  
- Model quality profiles  
- Publication profiles  
- AI draft editor  
- Grab sources + scheduled posting  

## Key conventions (do not break)

These are **stable product contracts** (also covered by tests):

1. **Model profile** lives in `filters["ai_model_profile"]`  
   values: `economy` | `balanced` | `quality` | `custom`  
   → `app/services/llm/model_profiles.py`

2. **Channel memory** is `filters["memory"]` as a **nested dict**, not flat filter keys  
   → `app/services/llm/channel_memory.py`

3. **Publication profile** is flat `filters["publication_profile"]`  
   values: `default` | `news` | `sales` | `analysis` | `meme`  
   → `app/services/llm/publication_profiles.py`

4. **`AIGenerationService(session)`** takes **only** `session`  
   (it constructs repos internally — do not add `ai_repo=` to the constructor)

5. Preserve unrelated keys when updating `filters` (memory/profile helpers already do this).

## Architecture

```text
app.bot.dispatcher
  routers (admin, posting, settings, sources, ...)
app.services.*          business logic
app.services.llm.*      AI surface
app.repositories.*      DB
app.workers.*           background
app.userbot.*           Telethon side
```

Run:

```bash
source .venv/bin/activate   # systemd uses /home/refactor_bot/.venv
python -m app.bot.dispatcher
```

## Working rules

### Before coding

1. `INDEX.md` → canonical module for the task.  
2. If changing AI filters/memory/profiles — read the matching `tests/test_llm_*.py` first.  
3. Prefer small PRs; no drive-by reformat of large routers.

### Edits

- Follow `.cursor/AI_RULES.md`: minimal diffs, async-safe, settings via `app.core.config`.  
- Archived `docs/archive/AI_*.md` files are historical and **not** authoritative — code/tests win.  
- Don't invent second storage for memory/profile outside `filters`.

### External bots / tokens / sessions

Conservative side effects (user preference):

- Before deactivating external bots, revoking/replacing tokens, or reusing userbot sessions:  
  identify linked channels/projects and explain blast radius.

## Deploy (this host)

```bash
systemctl restart refactor-bot.service
systemctl is-active refactor-bot.service
journalctl -u refactor-bot.service -n 50 --no-pager
```

No confirmation loops on routine restarts when the user asks.

## Checks

```bash
source .venv/bin/activate
pytest -q --maxfail=1 --disable-warnings
# focused:
pytest -q tests/test_llm_channel_memory.py tests/test_ai_model_profiles.py tests/test_llm_publication_profiles.py
mypy app   # if env supports it
```

## Safety

- Never commit `.env`, `userbot.session`, tokens, OpenRouter keys.  
- Don't log full prompts with secrets.  
- Avoid broad refactors of `posting.py` / dispatcher in the same change as a small AI fix.

## Definition of done

- [ ] Conventions above still hold (profiles / nested memory / service ctor)  
- [ ] Relevant `tests/test_llm_*.py` pass  
- [ ] Service restarted + smoke if prod change  
- [ ] `INDEX.md` still accurate if layout changed  
