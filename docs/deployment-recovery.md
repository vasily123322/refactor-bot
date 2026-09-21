# Production deployment and recovery runbook

This runbook defines the repository-supported operational contract for deploying and
recovering `refactor-bot`. It intentionally stays provider-neutral: the repository
owns the application process, Alembic migrations, and readiness semantics; the
operator owns the host/container supervisor, database service, durable backups, secret
storage, and external monitoring.

## Supported baseline

- Production and canonical CI use Python 3.12.
- Install production runtime dependencies through the committed lock:
  `python -m pip install -r requirements.txt`. Development/canonical CI additionally
  installs `requirements-dev.txt` for test and tooling dependencies.
- The supported foreground entry point is `python -m app.bot.dispatcher`.
- Alembic is the only supported schema authority.
- The default database is SQLite at `./data/bot.db`; PostgreSQL is supported through
  `DB_URL`, but PostgreSQL service operation and backups are provider/operator owned.
- The repository does not ship a production process supervisor. Use a service manager,
  container platform, or orchestrator appropriate for the deployment.
- Telegram/userbot sessions, Redis data, provider-side Telegram state, and external
  service credentials are not part of a database backup unless the operator backs them
  up separately.

## Process supervision

Run one foreground application process under an external supervisor. The supervisor
should:

1. start `python -m app.bot.dispatcher` from the intended release checkout and
   environment;
2. restart the whole process after an unexpected process exit, with bounded backoff
   rather than a tight restart loop;
3. stop the process before database restore or other destructive recovery work;
4. preserve stdout/stderr in the platform log collector, or keep the repository file
   sink enabled with its bounded retention policy;
5. when Studio is enabled, use `/healthz` as liveness and `/readyz` as readiness.

The embedded Studio server is part of the supervised process. If it terminates
unexpectedly after becoming ready, the application treats that as a fatal runtime
failure rather than silently continuing bot polling without Studio.

A supervisor must not turn a persistent startup failure into an infinite hot loop.
Repeated schema/readiness failures require operator investigation.

## Normal deployment

For a deployment that may include schema changes:

1. Select the exact application commit/release to deploy.
2. Install the locked runtime dependencies in the target Python 3.12 environment with
   `python -m pip install -r requirements.txt`.
3. Verify that the required environment and secrets for that deployment are present,
   including the Telegram credentials required by `Settings`, any non-default
   `DB_URL`, userbot/session configuration when enabled, and configured provider
   credentials.
4. Stop or drain the currently running application before taking a file-level SQLite
   backup or performing restore work.
5. Take and verify a database backup using the database-specific procedure below.
6. Inspect the current Alembic state:

   ```bash
   alembic current
   alembic history
   ```

7. Upgrade the database before starting a new release when an existing
   Alembic-managed database is behind:

   ```bash
   alembic upgrade head
   alembic current --check-heads
   ```

8. Start the application under the external supervisor.
9. If Studio is enabled, verify `GET /healthz` and then `GET /readyz`. A 200 from
   `/healthz` only proves process liveness; production traffic should not be admitted
   until `/readyz` returns 200.

A truly empty database can be migrated automatically to the shipped Alembic head by
application startup. For production deployments, running `alembic upgrade head`
explicitly before startup is preferred because migration failure is then separated
from process startup.

An existing managed database is never silently upgraded by application startup. If it
is behind the application head, startup fails closed.

## SQLite backup

The default SQLite database is `./data/bot.db`. For a production backup, stop the
application first so the backup boundary is unambiguous. One supported operator
pattern with the SQLite CLI is:

```bash
mkdir -p backups
backup="backups/bot-$(date -u +%Y%m%dT%H%M%SZ).db"
sqlite3 ./data/bot.db ".backup '$backup'"
test "$(sqlite3 "$backup" 'PRAGMA integrity_check;')" = "ok"
echo "verified backup: $backup"
```

Store the verified backup on durable storage outside the application working
directory according to the deployment's retention policy. A filesystem or VM snapshot
is also acceptable if the storage provider guarantees a consistent database snapshot;
that guarantee is provider-specific.

Normal application startup and normal Alembic upgrades do **not** create an automatic
production backup.

### SQLite restore

Keep the application stopped during restore. Preserve the failed database copy for
forensics, restore a verified backup, and validate it before restart:

```bash
failed="./data/bot.db.failed.$(date -u +%Y%m%dT%H%M%SZ)"
cp -a ./data/bot.db "$failed"
cp -a /path/to/verified-backup.db ./data/bot.db
test "$(sqlite3 ./data/bot.db 'PRAGMA integrity_check;')" = "ok"
alembic current --check-heads
```

If `alembic current --check-heads` fails, do not bypass the check. Either deploy the
application version matching the restored database revision or apply the reviewed
migration path to the restored copy.

## PostgreSQL and managed database providers

For PostgreSQL, prefer a provider snapshot/point-in-time recovery mechanism or standard
PostgreSQL backup tools such as `pg_dump`/`pg_restore`. Backup consistency,
credentials, roles, extensions, server-version compatibility, WAL/PITR, and retention
are provider/operator responsibilities.

Do not assume that the application's SQLAlchemy URL is directly accepted by PostgreSQL
CLI tools. For example, a `postgresql+psycopg://...` application URL may need to be
converted to the provider/libpq DSN expected by `pg_dump` or `pg_restore`.

After restore, verify the restored database with the matching application environment:

```bash
alembic current --check-heads
```

Then start the application and verify readiness.

## Schema upgrades and unmanaged legacy databases

The normal schema contract is:

- empty database: Alembic may initialize it to head;
- existing Alembic-managed database at head: startup verifies it and continues;
- existing Alembic-managed database behind/ahead of the expected head: startup fails;
- non-empty database without `alembic_version`: startup refuses to mutate it.

For a historical database created by the old current-ORM `create_all()` bootstrap,
the only supported adoption path is:

1. stop the application;
2. take and verify a database backup;
3. run:

   ```bash
   python scripts/adopt_legacy_database.py
   ```

4. restart only after the command succeeds.

The adoption command requires the unmanaged schema to match current ORM metadata
exactly before stamping the current Alembic head. Do not replace it with a blind
`alembic stamp head`.

## Failed migration or failed startup

If `alembic upgrade head` fails:

1. keep the application stopped;
2. retain the migration error and database logs;
3. inspect `alembic current` and the failed migration revision;
4. do not run ad-hoc schema creation or blindly stamp a revision;
5. if the migration may have partially changed the database, restore the verified
   pre-deployment backup into a separate/recovered database before retrying;
6. retry only after the migration/data problem is understood and the intended path has
   been reviewed.

If application startup fails because the managed database is not at the expected head,
its schema shape is incomplete, or SQLite foreign-key integrity fails, fix/restore the
database before restarting. The runtime deliberately has no `create_all()` or
ad-hoc migration fallback.

When Studio is enabled, a persistent 503 from `/readyz` is an operational failure
signal. Do not bypass readiness to admit traffic.

## Rollback boundaries

A code rollback is safe only when the database state is compatible with the older
application commit.

If a deployment did not change schema, restore the previous application commit and its
locked dependencies, then verify startup/readiness.

If a deployment upgraded schema, do not assume the old application accepts the new
Alembic head. The conservative rollback unit is:

- the previous application commit and locked dependencies; and
- the verified pre-migration database backup.

Alembic `downgrade` is not a generic repository recovery guarantee. Use a downgrade
only when the specific migration's downgrade path has been reviewed and tested for the
affected data. Otherwise restore the pre-migration backup.

Database rollback cannot undo external Telegram/provider side effects that were already
performed, such as messages sent or deleted. Reconcile those separately after database
recovery.

## Legacy SQLite `.backup.<timestamp>.db` artifacts

`app.core.db.init_db_if_needed_sync()` is retained only as an explicit/manual
historical repair helper and is never called by supported application startup. For one
legacy SQLite layout it may rename the database to a file shaped like:

```text
<database-path>.backup.<unix-timestamp>.db
```

That artifact is a side effect of the legacy repair helper. It is **not** the normal
Alembic backup mechanism, is not created by supported production startup, and should
not be treated as a verified production backup unless the operator separately
validates and preserves it.

## Recovery checklist

Before returning service to production, confirm:

- the intended application commit is deployed;
- Python 3.12 runtime dependencies were installed through the committed lock;
- the deployment's required Telegram, database, userbot/session, and provider
  configuration is present;
- the restored/migrated database passes its engine-specific integrity checks;
- `alembic current --check-heads` succeeds;
- the application starts without schema/runtime configuration errors;
- `/readyz` returns 200 when Studio is enabled;
- required external services and Telegram/userbot credentials are available;
- any provider-side effects that cannot be rolled back through the database have been
  reconciled separately.
