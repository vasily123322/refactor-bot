# Self-hosted CI

GitHub-hosted CI in `.github/workflows/ci.yml` is the repository's canonical CI
contract. It runs on pull requests, pushes to `main`, and manual dispatch. Do not
treat a historical account billing/spending incident as current repository state:
availability of GitHub-hosted runners is an external account/platform condition and
must be checked from the current workflow run when it matters.

The canonical required job names are:

- `Python 3.12`
- `Telegram Studio frontend`

A self-hosted runner is optional. Use it when an operator deliberately wants an
independent exact-SHA validation path, when GitHub-hosted runner allocation is
temporarily unavailable, or when diagnosing runner-specific infrastructure. A
self-hosted success is supplemental evidence; it does not replace the canonical hosted
checks or change their required status.

## Canonical CI contract

For backend validation, `.github/workflows/ci.yml` currently uses Python 3.12 and:

```text
python -m pip install --upgrade pip
pip install -r requirements.txt -r requirements-dev.txt
python scripts/secret_scan.py
ruff check app tests scripts
python -m compileall -q app tests scripts
pytest -q <stabilization regression files>
python scripts/backend_test_scope.py
```

The committed `requirements.txt` and `requirements-dev.txt` wrappers use the
repository dependency lock contract. The supported broad-suite step is collection,
not an unrestricted `pytest -q` of every historical test.

For Telegram Studio, the canonical workflow uses Node 24 and the committed
`studio/package-lock.json`:

```text
npm ci --no-audit --no-fund
npm run test
npm run build
```

Use `npm ci`, not `npm install`, when reproducing the canonical frontend dependency
install. The lockfile exists and is part of the reproducible CI contract.

## Self-hosted runner architecture

Use a dedicated Linux x64 CI machine. Prefer a disposable/ephemeral VM rather than a
personal workstation or a host carrying production credentials or unrelated sensitive
workloads.

The repository self-hosted workflows request these labels:

- `self-hosted`
- `linux`
- `x64`
- `refactor-bot-ci`

A practical baseline is a currently supported Ubuntu LTS image with outbound HTTPS,
Git, archive tools, and the build prerequisites needed by Python packages. The runner
does not require inbound service ports, Docker, PostgreSQL, or Redis for the repository's
current SQLite-based CI environment.

Do not place production `.env` files, API keys, SSH private keys, cloud credentials,
or unrelated workload data on the runner. Do not grant the runner account passwordless
sudo or access to the Docker socket.

## Registration

In the repository, open **Settings → Actions → Runners → New self-hosted runner** and
follow GitHub's current Linux/x64 download and registration instructions. Registration
tokens are short-lived; generate one only when the runner host is ready and never
commit it.

Keep GitHub's default labels and add the custom `refactor-bot-ci` label. For an
ephemeral runner, configure it with GitHub's `--ephemeral` option and destroy or
re-image the VM after the accepted job. Workspace cleanup such as `git clean -ffdx`
is hygiene, not a security boundary.

## Diagnostic workflow

`.github/workflows/self-hosted-diagnostic.yml` is for runner/bootstrap diagnostics.
It validates allocation, checkout, Python 3.12, Node 24, and basic repository/toolchain
visibility. It intentionally does not prove the canonical backend/frontend test
contract.

Use it when confirming that a newly registered runner is reachable and correctly
labelled. Do not use a diagnostic success as a merge gate.

## Exact-SHA self-hosted workflow

`.github/workflows/self-hosted-ci.yml` validates an explicitly selected commit SHA on
a runner labelled `refactor-bot-ci`. Its checkout is deliberately exact-SHA and uses
non-persisted checkout credentials.

The self-hosted workflow has historically carried extra compatibility/stress checks
(such as Python 3.10, an explicit Alembic upgrade, or a broader full-suite run). Those
checks are supplemental and are not part of the canonical required-check definition.
When the two workflows differ, `.github/workflows/ci.yml` is the source of truth for
the required dependency-install and test contract.

In particular, anyone maintaining the self-hosted workflow should keep its canonical
overlap synchronized with hosted CI:

- Python 3.12 dependency install through `requirements.txt` and
  `requirements-dev.txt`;
- tracked-file secret scan;
- Ruff and compile gates;
- the current stabilization regression set;
- `scripts/backend_test_scope.py`;
- Node 24;
- Studio install with `npm ci --no-audit --no-fund`;
- Studio tests and build.

Additional self-hosted-only checks must be clearly treated as supplemental rather than
silently redefining canonical CI.

## Security rules

Keep workflow permissions read-only unless a reviewed use case requires more. Use
checkout without persisted credentials for untrusted/exact-SHA validation. Do not use
`pull_request_target` to execute PR code, do not automatically execute arbitrary PR
heads on a persistent privileged runner, and do not expose repository/environment
secrets to validation jobs.

Prefer a fresh runner image for independently changing commits. Preserve GitHub job
logs and runner diagnostics externally when an ephemeral host will be destroyed.

## Merge interpretation

Normal repository merge readiness is determined by the canonical hosted workflow for
the exact PR head: `Python 3.12` and `Telegram Studio frontend` must both be green.
After merge, the canonical push workflow on the merge SHA must also be green before the
roadmap issue is considered done.

A self-hosted exact-SHA run can provide additional confidence or temporary diagnostic
coverage, but it must not be used to rewrite a hosted infrastructure/account failure as
a code success or to claim that a different test matrix is the repository's canonical
contract.
