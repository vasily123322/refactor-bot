from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = ROOT / "tests"
HISTORICAL_MANIFEST = TESTS_DIR / "historical_backend_tests.txt"
REQUIRED_SUPPORTED_TESTS = {
    "tests/test_startup_smoke.py",
}


def _load_historical_tests() -> list[str]:
    entries: list[str] = []
    seen: set[str] = set()

    for line_number, raw_line in enumerate(
        HISTORICAL_MANIFEST.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        entry = raw_line.strip()
        if not entry or entry.startswith("#"):
            continue
        if entry in seen:
            raise SystemExit(
                f"{HISTORICAL_MANIFEST}: duplicate entry on line {line_number}: {entry}"
            )
        if not entry.startswith("tests/") or not entry.endswith(".py"):
            raise SystemExit(
                f"{HISTORICAL_MANIFEST}: invalid test path on line {line_number}: {entry}"
            )
        path = ROOT / entry
        if not path.is_file():
            raise SystemExit(
                f"{HISTORICAL_MANIFEST}: missing historical test on line {line_number}: {entry}"
            )
        seen.add(entry)
        entries.append(entry)

    if entries != sorted(entries):
        raise SystemExit(f"{HISTORICAL_MANIFEST}: entries must stay sorted")
    return entries


def main() -> int:
    historical = _load_historical_tests()
    historical_set = set(historical)
    discovered = sorted(
        str(path.relative_to(ROOT))
        for path in TESTS_DIR.rglob("test_*.py")
        if path.is_file()
    )

    unsupported_required = REQUIRED_SUPPORTED_TESTS & historical_set
    if unsupported_required:
        names = ", ".join(sorted(unsupported_required))
        raise SystemExit(f"required supported tests cannot be historical: {names}")

    supported = [path for path in discovered if path not in historical_set]
    if not supported:
        raise SystemExit("supported backend test scope is empty")

    print(
        "backend-test-scope: "
        f"{len(supported)} supported files, "
        f"{len(historical)} historical files, "
        f"{len(discovered)} total files"
    )

    command = [
        sys.executable,
        "-m",
        "pytest",
        "--collect-only",
        "-q",
        *supported,
    ]
    return subprocess.call(command, cwd=ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
