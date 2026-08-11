# Self-hosted CI bootstrap

GitHub-hosted Actions for this repository are blocked before runner allocation by the
account billing/spending policy documented in issue #213. This document defines an
independent self-hosted execution path. It does not make the hosted failure green and it
does not change application production code.

## Runner architecture

Use a dedicated Linux x64 VM/VPS for `refactor-bot` CI. Do not use a personal workstation
or a machine that contains other sensitive workloads.

Preferred production mode is disposable/ephemeral:

1. create a clean VM from a controlled image;
2. register the GitHub runner with `--ephemeral`;
3. execute at most one GitHub Actions job;
4. preserve the GitHub job log and, if needed, runner diagnostics externally;
5. destroy the VM and its disk after the job.

GitHub recommends ephemeral runners for autoscaling/isolation because an ephemeral runner
is automatically de-registered after one job. A reusable runner may be used only as a
short-lived bootstrap diagnostic. Repository workspace cleanup such as `git clean -ffdx`
is hygiene, not a security boundary: workflow code runs as the runner account and can
modify files outside the checkout. Re-image or destroy the VM between trust boundaries.

Exact labels used by repository workflows:

- `self-hosted` (default GitHub label)
- `linux` (default GitHub label)
- `x64` (default GitHub label)
- `refactor-bot-ci` (custom repository label)

## Minimal VM baseline

Recommended bootstrap image: Ubuntu 24.04 LTS x86_64. GitHub supports Ubuntu 20.04 or
later for self-hosted runners.

Create a dedicated unprivileged account and a dedicated runner directory from an admin
shell:

```bash
sudo useradd --create-home --shell /bin/bash actions || true
sudo install -d -o actions -g actions /opt/actions-runner
sudo apt-get update
sudo apt-get install -y \
  ca-certificates curl git tar gzip unzip \
  build-essential pkg-config libssl-dev libffi-dev
```

Do **not** add `actions` to sudoers. Do not give it the Docker socket. The current CI
uses SQLite in memory and does not require Docker, PostgreSQL, Redis, or inbound service
ports.

The machine needs outbound HTTPS to GitHub/GitHub Actions and normal package download
access used by pip/npm. No inbound network access is required by the runner itself.
Restrict administrative SSH separately to trusted sources.

Do not place API keys, SSH private keys, cloud credentials, production `.env` files, or
other workload data on the runner VM. The repository CI uses only synthetic non-secret
environment values.

## Register the repository runner

In GitHub open:

`vasily123322/refactor-bot` -> **Settings** -> **Actions** -> **Runners** ->
**New self-hosted runner** -> **Linux** -> **x64**.

GitHub displays the current runner archive URL, checksum/configuration commands, and a
short-lived registration token. The registration token expires after about one hour, so
generate it only when the VM is ready. Run the displayed download/extract commands as the
`actions` user inside `/opt/actions-runner`. Do not save or commit the token.

For the configuration step, keep the URL/token supplied by GitHub and add the repository
identity options below:

```bash
cd /opt/actions-runner
./config.sh \
  --url https://github.com/vasily123322/refactor-bot \
  --token '<TOKEN_FROM_GITHUB_UI>' \
  --name 'refactor-bot-ci-01' \
  --labels 'refactor-bot-ci' \
  --work '_work' \
  --unattended \
  --ephemeral
```

Do not use `--no-default-labels`; the workflows intentionally require GitHub's default
`self-hosted`, `linux`, and `x64` labels in addition to `refactor-bot-ci`.

For the preferred ephemeral runner, start it in the foreground:

```bash
cd /opt/actions-runner
./run.sh
```

It should report that it is connected and listening for jobs. After it accepts one job,
it deregisters. Destroy the VM after the job rather than reusing its filesystem. For a
production ephemeral provisioning system, forward runner diagnostic logs externally
before destroying the VM so runner-level failures remain diagnosable.

### Temporary reusable diagnostic service

If a disposable VM cannot be recreated yet, omit `--ephemeral` during configuration and
use a dedicated CI-only VM. From an administrator shell in the runner directory:

```bash
sudo ./svc.sh install actions
sudo ./svc.sh start
sudo ./svc.sh status
```

To stop or remove it:

```bash
sudo ./svc.sh stop
sudo ./svc.sh uninstall
```

The service account remains `actions`; the runner account itself should still have no
sudo capability. Replace/re-image this bootstrap VM after diagnostic use.

## Diagnostic workflow

`.github/workflows/self-hosted-diagnostic.yml` runs only on the bootstrap branch push or
manual dispatch. It requests exactly:

```yaml
runs-on: [self-hosted, linux, x64, refactor-bot-ci]
```

The diagnostic deliberately does not install project dependencies or execute the test
suite. It proves only:

- GitHub assigned a real self-hosted runner;
- checkout works with credentials not persisted in `.git/config`;
- `uname` and runner identity are visible;
- `actions/setup-python` can provide Python 3.12;
- `actions/setup-node` can provide Node 24/npm;
- repository files and exact checked-out commit are visible;
- workflow command logs are available.

Success criteria in GitHub's job metadata/logs:

- `runner_id` is non-zero;
- `runner_name` is populated (for example `refactor-bot-ci-01`);
- steps are present instead of `null`;
- checkout completes;
- command output is visible;
- the final `self-hosted diagnostic OK` line is present.

## Full CI control plane after the diagnostic

GitHub only accepts `workflow_dispatch` events when the workflow file exists on the
default branch. Therefore the full workflow has two deliberately different control paths.

### Pre-merge validation

Before `.github/workflows/self-hosted-ci.yml` reaches `main`, it can run only from its
control branch `agent/self-hosted-ci-workflow`. A push on that branch allocates a runner
**only** when all of these are true:

- actor is `vasily123322`;
- the first commit-message line begins exactly `validate-sha: `;
- the remainder is an exact lowercase 40-hex commit SHA.

Example control commit message:

```text
validate-sha: 0123456789abcdef0123456789abcdef01234567
```

The workflow parses that SHA as data, validates its format, checks out that exact commit
with persisted checkout credentials disabled, and requires `git rev-parse HEAD` to match.
A normal workflow-development push does not allocate the self-hosted runner job.

This exists so PR #281 can be validated before either CI PR is merged. It is not an
automatic `pull_request` executor and does not run every mutable PR head.

### Canonical path after merge

After the workflow exists on `main`, use GitHub **Actions** -> **Self-hosted CI** ->
**Run workflow**, supply the exact 40-hex `commit_sha`, and run it. Write access is
required to manually dispatch a workflow. The workflow again validates and checks out the
exact supplied SHA rather than a mutable PR branch name.

## Full CI contract

The self-hosted full CI reproduces the existing hosted CI contract and adds an explicit
Alembic upgrade command before each Python full-suite run.

Backend matrix semantics, executed sequentially in one ephemeral-runner job:

- Python 3.10 and 3.12;
- install `requirements.txt` and `requirements-dev.txt`;
- `python scripts/secret_scan.py`;
- `ruff check app tests scripts`;
- `python -m compileall -q app tests scripts`;
- `python -m alembic -c alembic.ini upgrade head`;
- `pytest -q` (includes additional Alembic/schema regressions).

Synthetic backend environment:

```text
BOT_TOKEN=123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi
API_ID=123456
API_HASH=0123456789abcdef0123456789abcdef
DB_URL=sqlite+aiosqlite:///:memory:
DB_SECRET_KEY=ci-only-database-secret-key-0123456789abcdef
PYTHONPATH=.
```

Telegram Studio frontend:

- Node 24 (the package declares Node >=20.19);
- `npm install --no-audit --no-fund` because the repository currently has no
  `studio/package-lock.json`;
- `npm run test`;
- `npm run build`.

No service containers, Docker daemon, PostgreSQL, Redis, caches, or build artifacts are
required by the current canonical CI workflow.

A single sequential full-CI job is intentional for the first production design: one
`--ephemeral` runner accepts one GitHub job, executes every backend/frontend gate for one
exact SHA, then de-registers and the VM can be destroyed. This avoids needing three
separately provisioned ephemeral runners merely to reproduce the old matrix.

## Security rules

- Keep workflow-level `permissions: contents: read`.
- Use `actions/checkout` with `persist-credentials: false`.
- Pin official actions to reviewed exact commit SHAs.
- Do not expose repository/environment secrets to validation jobs.
- Do not use `pull_request_target` to check out and execute PR code.
- Do not automatically execute arbitrary `pull_request` heads on a persistent runner.
- Do not mount the Docker socket or grant passwordless sudo.
- Do not keep SSH/private keys or cloud metadata credentials on the machine.
- Keep the runner dedicated to this repository.
- Prefer a fresh VM for every independently changing commit.
- Treat `git clean` as workspace hygiene only; it does not undo host compromise.
- For a reusable bootstrap runner, remove it from GitHub and re-image the VM after use.
- Preserve explicit hosted-CI failure status; self-hosted success is a separate validation
  signal and must not rewrite #213 as a code fix.

## Merge gate

A PR is not validated merely because the GitHub-hosted workflow fails with the known
billing signature. Merge readiness requires the exact PR head SHA to run through the
self-hosted full CI and show:

- real runner allocation and checkout;
- exact requested SHA equals `git rev-parse HEAD`;
- Python 3.10 green;
- Python 3.12 green;
- Ruff/compile/explicit Alembic upgrade/full pytest green;
- Telegram Studio tests green;
- Telegram Studio build green;
- any PR-specific safety regressions green.

The first full validations after runner bootstrap are PR #281 and then the top safe
migration/repeat head owned by Chat A.
