[![Type Check](https://img.shields.io/badge/Type%20check-mypy-blueviolet)](#)
[![Coverage](https://img.shields.io/badge/Coverage-pytest--cov-green)](#)

# refactor-bot

Telegram bot for channel operations: posting and scheduling, source ingestion, AI-assisted generation and editing, and the Studio frontend.

## How to Run

Activate the virtual environment first:

```bash
source .venv/bin/activate
```

From the project root (e.g., `/home/refactor_bot`), run the dispatcher entry point:

```bash
python -m app.bot.dispatcher
```

Alternatively, you can run the file directly:

```bash
python app/bot/dispatcher.py
```

For the supported production deployment, supervision, backup/restore, rollback, and
failed-start recovery contract, see `docs/deployment-recovery.md`.

## Database migrations

Alembic is the only supported schema authority for normal application startup and
future schema changes. It uses the same `DB_URL` and ORM registry as the application.

### Fresh/default database

A truly empty database is migrated to the shipped Alembic `head` automatically
during application startup, before the dispatcher or any background worker starts.
Startup then verifies the Alembic head, required ORM tables/columns, and (for SQLite)
foreign-key integrity/enforcement. There is no `Base.metadata.create_all()` or ad-hoc
schema fallback in the supported runtime path.

You can also initialize a fresh database explicitly before starting the application:

```bash
alembic upgrade head
```

### Existing Alembic-managed database

Application startup does **not** silently upgrade a managed database that is behind
the revision head shipped with the application. Deployments must run:

```bash
alembic upgrade head
```

before starting the new application version. If the database is behind, structurally
incomplete, or fails SQLite foreign-key integrity checks, startup fails closed before
workers start. Schema-changing migration failures are not swallowed or replaced by a
runtime bootstrap.

The first revision (`20260809_0001`) remains the frozen, non-destructive Alembic
baseline; later schema changes remain explicit revisions. No new migration is needed
for the startup-authority change itself because it does not change database schema.

### Health and readiness

The embedded Studio API exposes two unauthenticated operational endpoints with
different contracts:

- `GET /healthz` is **liveness** only. A 200 response means the Studio HTTP process
  can answer requests; it does not assert database or worker readiness.
- `GET /readyz` is **readiness**. It returns 200 only after the supported bot runtime
  has completed startup and the database is reachable at the Alembic head expected by
  the running application. During startup, shutdown, database failure, or schema
  mismatch it returns 503.

Readiness responses expose only coarse check states (`runtime`, `database`,
`schema`) and never connection strings, exception messages, tokens, or migration
details. Use `/healthz` for liveness/restart detection and `/readyz` for traffic
admission/draining. A persistent 503 on `/readyz` should trigger investigation of
application boot logs and database/schema state rather than bypassing the readiness
gate. Studio task termination after startup remains fatal to the parent runtime and is
supervised by the process lifecycle.

### Legacy database without `alembic_version`

A non-empty database without `alembic_version` is never modified by application
startup. This intentionally retires the old indefinite `create_all()`/ad-hoc
production bootstrap.

For a database created by the previous current-ORM `create_all()` startup path:

1. Stop the application and take a verified database backup.
2. Run the guarded one-time adoption command:

   ```bash
   python scripts/adopt_legacy_database.py
   ```

3. Restart the application.

The adoption command compares the unmanaged database against the complete current ORM
metadata, including Alembic-visible table/column/index/constraint differences. It
stamps the database at the current Alembic `head` only when that comparison is clean.
If the schema differs, adoption fails without stamping it. Repair or migrate that
database explicitly from the verified backup; do **not** bypass the guard with a blind
`alembic stamp head`.

After adoption, all future changes use normal Alembic upgrades. The legacy SQLite
repair helper in `app/core/db.py` is retained only for explicit/manual recovery of
historical layouts and is not part of application startup.

Useful checks:

```bash
alembic current --check-heads
alembic history
```

## Python dependencies

Python 3.12 is the supported production/canonical CI version. Install runtime and
test dependencies through the committed lock:

```bash
python -m pip install -r requirements.txt -r requirements-dev.txt
```

See `docs/python-dependencies.md` for the lock format and intentional upgrade
workflow.

## How to Test

Make sure the virtual environment is activated:

```bash
source .venv/bin/activate
```

Then run all tests with coverage:

```bash
pytest -q --maxfail=1 --disable-warnings --cov=app --cov-report=term-missing
```

Run type checks:

```bash
mypy app
```

## Environment variables

`.env.example` is the exhaustive supported operator-facing environment inventory.
The blocking startup-smoke regression keeps it synchronized with the current config
sources. Legacy compatibility inputs such as lowercase `redis_host` / `redis_port` /
`redis_db` are intentionally not part of the supported operator surface, and retired
`POST_TASK_RETENTION_*` settings must not be reintroduced.

Core runtime and storage:

- `BOT_TOKEN`, `API_ID`, and `API_HASH` are required by `Settings`.
- `ADMIN_USER_ID` and `ADMIN_USERNAME` are optional administration identity values.
- `USERBOT_SESSION` and the `USERBOT_PROXY_*` family configure optional Telethon use.
- `DB_URL` defaults to SQLite at `./data/bot.db`; `REDIS_DSN` is optional.
- `DB_SECRET_KEY` is read directly by the DB-secret encryption layer rather than
  Pydantic `Settings`. New encrypted secret writes require at least 32 characters.
  Preserve the same key when restoring a DB that already contains `enc:v1` values.
- `SQLA_NULLPOOL` and `SQLA_STATICPOOL` control SQLAlchemy pooling.

Studio and logging:

- `STUDIO_ENABLED`, `STUDIO_HOST`, `STUDIO_PORT`, `STUDIO_PUBLIC_URL`,
  `STUDIO_INIT_DATA_MAX_AGE_SECONDS`, and `STUDIO_CORS_ORIGINS` configure the
  embedded Studio API. Invalid explicitly supplied boolean/integer values fail closed.
- `LOG_LEVEL`, `LOG_FILE_ENABLED`, and `LOG_FILE_RETENTION` control stdout and
  bounded repository-managed file logging.

Canonical scheduling/delivery controls are default-off unless stated otherwise:

- `REPEAT_OVERFLOW_LIMIT` defaults to `2`.
- `CANONICAL_REPEAT_SHADOW_PLANNING_ENABLED` gates
  `CANONICAL_REPEAT_SUCCESSFUL_PLANNING_ENABLED`.
- `CANONICAL_REPEAT_OVERDUE_RECOVERY_SHADOW_ENABLED` gates
  `CANONICAL_REPEAT_OVERDUE_RECOVERY_PLANNING_ENABLED`.
- `CANONICAL_REPEAT_BOOT_RECOVERY_SHADOW_ENABLED` gates
  `CANONICAL_REPEAT_BOOT_RECOVERY_PLANNING_ENABLED`.
- `CANONICAL_PUBLICATION_DELIVERY_RECOVERY_WORKER_ENABLED` plus its
  `_INTERVAL_SECONDS` and `_BATCH_SIZE` settings configure fail-closed expired-lease
  recovery. The primary delivery worker requires this recovery flag to be enabled.
- `CANONICAL_PUBLICATION_DELIVERY_WORKER_ENABLED`,
  `CANONICAL_PUBLICATION_DELIVERY_WORKER_INTERVAL_SECONDS`,
  `CANONICAL_PUBLICATION_DELIVERY_WORKER_BATCH_SIZE`,
  `CANONICAL_PUBLICATION_DELIVERY_WORKER_SCAN_LIMIT`,
  `CANONICAL_PUBLICATION_DELIVERY_WORKER_LEASE_TTL_SECONDS`, and
  `CANONICAL_PUBLICATION_DELIVERY_WORKER_HEARTBEAT_INTERVAL_SECONDS` configure the
  opt-in primary delivery worker.
- `PUBLICATION_AUTODELETE_WORKER_*` configures time-based canonical autodelete.
- `PUBLICATION_AUTODELETE_VIEWS_WORKER_*` configures views-based autodelete and
  requires a successfully started userbot.
- `LOCAL_ENRICHMENT_WORKER_*` configures the optional deterministic Inbox enrichment
  worker.

AI/network operator settings are also enumerated with defaults in `.env.example`:
`OPENROUTER_*`, `AI_MODELS_JSON`, optional direct-provider keys/base URLs,
`WHISPER_MODEL`, `SPEECH_PROVIDER`, `GROQ_*`, `HTTP_FETCH_*`, and
`CONTENT_EXTRACT_MAX_LEN`.

## Modules overview (new/updated)

- app/services/llm/openrouter_client.py: OpenRouter client with retries, backoff, request_id, typed result
- app/services/llm/prompt_builder.py: PromptBuilder centralizes prompt construction
- app/services/notifier.py: Token quota alerts (80%)
- app/services/http/fetcher.py: fetch_html with retries, backoff and User-Agent
- app/services/extractors/html.py: HTML → text extractor with lxml/html.parser fallback
- app/services/ai_generation.py: unified LLM pipeline, instruction mapping helper, request_id propagation

## Observability

Each LLM call is logged with: request_id, model, duration (ms), tokens and outcome.

The repository file sink rotates daily, compresses rotated files, and retains them for
`LOG_FILE_RETENTION` (14 days by default). Set `LOG_FILE_ENABLED=false` when
stdout/stderr is collected by an external logging platform so the repository does not
persist a second copy on disk.