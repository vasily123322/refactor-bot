## Modules Spec

### LLM Client: `app/services/llm/openrouter_client.py`
- Purpose: OpenRouter Chat Completions wrapper
- Features: retries (429/5xx/transport), exponential backoff with jitter, request_id logging
- I/O:
  - Input: messages[], model, temperature, top_p, max_tokens, base_url, api_key, request_id?
  - Output: { success, text|null, tokens_used:int, error|null }

### Prompt Builder: `app/services/llm/prompt_builder.py`
- Purpose: build system/user prompts with tone/length/emoji presets and variables
- I/O:
  - Input: ai_settings, topic, extra_vars, mode?
  - Output: (system_prompt, user_prompt)

### Notifier: `app/services/notifier.py`
- Purpose: 80% monthly tokens alert to owner and admin log chat
- I/O:
  - Input: channel_id, used_tokens, month_limit, percentage
  - Output: side effects (Telegram messages)

### HTML Fetcher: `app/services/http/fetcher.py`
- Purpose: `fetch_html(url)` with retries/backoff and User-Agent
- I/O:
  - Input: url, timeouts/retries/backoff, request_id?, user_agent?
  - Output: html text or exception

### HTML Extractor: `app/services/extractors/html.py`
- Purpose: extract plain text from HTML (scripts/styles/nav removed)
- Notes: fallback to `html.parser` if `lxml` fails

### AI Generation Service: `app/services/ai_generation.py`
- Purpose: orchestrate prompts → LLM → post-process → counters/notifications
- Key helpers:
  - `_default_instruction_by_mode(mode)`
  - `_call_openrouter_messages(messages, ...)` → `OpenRouterClient.chat`
- Flows:
  - from_scratch: build → call → postprocess → update tokens
  - improve: use original_text → build → call → update tokens
  - from_link: `fetch_html` → extract → build (instruction by mode) → call → update tokens

### Bot Dispatcher: `app/bot/dispatcher.py`
- Startup: logging, DB init, userbot, optional workers
- Validates `AI_MODELS_JSON` (warnings) and logs entries count

