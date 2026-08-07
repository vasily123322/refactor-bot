## Руководство для контрибьюторов

Спасибо за вклад в проект!

### Требования
- Python 3.10+
- Виртуальное окружение (`python -m venv venv`)
- Установите зависимости:
```bash
pip install -r requirements.txt
```
- Настройте переменные окружения (пример):
```bash
export OPENROUTER_API_KEY=sk-or-...
# LLM (используется одна модель):
export OPENROUTER_MODEL="openai/gpt-4o-mini"
export OPENROUTER_TEMPERATURE=0.7
export OPENROUTER_TOP_P=0.7
export OPENROUTER_BASE_URL="https://openrouter.ai/api/v1"
# Политики сети LLM:
export OPENROUTER_TIMEOUT_SECONDS=60
export OPENROUTER_MAX_RETRIES=3
export OPENROUTER_BACKOFF_INITIAL=0.5
export OPENROUTER_BACKOFF_MAX=5.0
# Загрузка HTML:
export HTTP_FETCH_TIMEOUT_SECONDS=30
export HTTP_FETCH_MAX_RETRIES=2
export HTTP_FETCH_BACKOFF_INITIAL=0.4
export HTTP_FETCH_BACKOFF_MAX=3.0
export HTTP_FETCH_USER_AGENT="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0 Safari/537.36"
# Ограничение размера извлечённого текста перед LLM:
export CONTENT_EXTRACT_MAX_LEN=8000
```

### Запуск
```bash
cd /home/refactor_bot
python -m app.bot.dispatcher
```
- База SQLite создаётся автоматически; простые миграции применяются при старте.

### Ветки и коммиты
- Ветки: `feature/<кратко>`, `fix/<кратко>`
- Коммиты (conventional): `feat: ...`, `fix: ...`, `refactor: ...`, `docs: ...`

### Код-стайл
- Асинхронность: не блокируйте event loop; используйте `await`, `httpx.AsyncClient`, короткие `AsyncSession` из `AsyncSessionLocal`.
- Ошибки: не глушите исключения; ловите и обрабатывайте предметно.
- Имена: понятные, полные слова; ранние return; избегайте глубокой вложенности.
- Типы: явные сигнатуры публичных API; избегайте `Any`.
- Форматирование: не меняйте несвязанные участки; сохраняйте текущие отступы и стиль.

### Спеки и правила для ИИ
- Перед изменениями в логике обновите спецификации:
  - `specs/MODULES_SPEC.md`, `specs/MODULES_IO.yaml`
  - предложения: `specs/REFACTOR_PROPOSALS.md`
- Соблюдайте `/.cursor/AI_RULES.md` при автогенерации в Cursor.

### Модули (важное)
- `app/services/llm/openrouter_client.py` — клиент OpenRouter с ретраями/backoff, `request_id`, типизированным результатом
- `app/services/http/fetcher.py` — `fetch_html()` с ретраями/backoff и User-Agent
- `app/services/extractors/html.py` — извлечение текста (fallback `html.parser` при проблемах с `lxml`)
- `app/services/llm/prompt_builder.py` — сборка промптов
- `app/services/notifier.py` — уведомления о 80% месячной квоты токенов
- `app/services/ai_generation.py` — единый пайплайн LLM; логирование и `request_id`

### Pull Request
- Описание: цель, изменения, как тестировать
- Чек‑лист: покрытие кейсов, обновлены доки/спеки, нет лишнего форматирования
- Линкните связанные issue (`Closes #123`).

### Тестирование
- Запустите локально функционал, проверьте логи на ошибки
- По возможности добавьте автотесты (если применимо)

### Безопасность
- Не коммитите ключи и секреты, используйте переменные окружения
- Проверяйте внешние запросы/входные данные

Добро пожаловать с идеями и PR! 🚀
