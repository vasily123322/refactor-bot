# Python dependency lock

The supported production and canonical CI Python version is **Python 3.12**.

Python dependencies have two layers:

- `requirements.in` contains direct runtime dependency ranges.
- `requirements-dev.in` contains direct test/tooling dependency ranges.
- `requirements.lock` pins the complete runtime + test dependency graph resolved on Python 3.12.
- `requirements.txt` and `requirements-dev.txt` are install wrappers that apply `requirements.lock` as a constraints file.

A normal development or CI install is therefore:

```bash
python -m pip install -r requirements.txt -r requirements-dev.txt
```

Because both wrappers apply `requirements.lock`, the same git commit installs the same resolved Python package versions instead of resolving range constraints again.

## Intentional dependency upgrades

Do not edit versions in `requirements.lock` by hand. Change the direct ranges in `requirements.in` or `requirements-dev.in`, then regenerate the lock from a clean Python 3.12 virtual environment with:

```bash
./scripts/update_python_lock.sh
```

The script creates a temporary isolated virtual environment, installs the source manifests with upgrades enabled, and rewrites `requirements.lock` from that resolved graph. Set `PYTHON_BIN` only when the Python 3.12 executable has a non-default name:

```bash
PYTHON_BIN=/path/to/python3.12 ./scripts/update_python_lock.sh
```

After a lock refresh, run the canonical backend checks (including a clean install through `requirements.txt` and `requirements-dev.txt`) before merging. Dependency upgrades should commit the source-manifest change and regenerated lock together.

## Version compatibility

Python 3.12 is the reproducibility authority for this repository. The committed lock is generated and validated against Python 3.12, which is also the canonical hosted CI version. Other Python versions are not part of the locked production contract unless the repository explicitly adds a separately generated and tested lock for them.
