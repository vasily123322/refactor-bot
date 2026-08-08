from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str
    excerpt: str


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern[str]


RULES: tuple[Rule, ...] = (
    Rule(
        "telegram-bot-token",
        re.compile(r"(?<![A-Za-z0-9_-])\d{5,12}:[A-Za-z0-9_-]{30,}(?![A-Za-z0-9_-])"),
    ),
    Rule(
        "sk-api-key",
        re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])", re.IGNORECASE),
    ),
    Rule(
        "google-api-key",
        re.compile(r"(?<![A-Za-z0-9_-])AIza[0-9A-Za-z_-]{30,}(?![A-Za-z0-9_-])"),
    ),
    Rule("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    Rule("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    Rule("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
    Rule(
        "private-key",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    ),
    Rule(
        "credential-output-interpolation",
        re.compile(
            r"(?i)(?:token|токен|api[_ -]?key|password|secret)[^\n]{0,120}"
            r"\{(?:token|api_key|api[_ -]?key|password|secret)\}"
        ),
    ),
)

# Exact synthetic values only. Do not add broad wildcard exceptions here.
_SAFE_LITERALS = {
    "123456:" + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
    "sk-or-v1-xxxxxxxxxxxxxxxxxxxxxxxx",
}

_SKIP_PATHS = {
    # These files intentionally contain credential-shaped fixtures to test redaction/scanning.
    "tests/test_credential_redaction.py",
    "tests/test_secret_scan.py",
}


def _tracked_files() -> list[str]:
    proc = subprocess.run(
        ["git", "ls-files", "-z"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return [item for item in proc.stdout.decode("utf-8").split("\0") if item]


def scan_text(path: str, text: str) -> list[Finding]:
    findings: list[Finding] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        scan_line = line
        for literal in _SAFE_LITERALS:
            scan_line = scan_line.replace(literal, "[SAFE_TEST_CREDENTIAL]")
        for rule in RULES:
            if rule.pattern.search(scan_line):
                excerpt = line.strip()
                if len(excerpt) > 180:
                    excerpt = excerpt[:177] + "..."
                findings.append(Finding(path, line_no, rule.name, excerpt))
    return findings


def scan_repository(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for relative in _tracked_files():
        if relative in _SKIP_PATHS:
            continue
        if Path(relative).name == ".env":
            findings.append(Finding(relative, 1, "tracked-dotenv", "tracked .env file"))
            continue
        path = root / relative
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if b"\0" in raw:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        findings.extend(scan_text(relative, text))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan tracked repository text for high-confidence credential leaks.")
    parser.add_argument("--root", default=".", help="Repository root (default: current directory)")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    findings = scan_repository(root)
    if not findings:
        print("secret-scan: clean")
        return 0

    print("secret-scan: credential-shaped content found:", file=sys.stderr)
    for finding in findings:
        print(
            f"  {finding.path}:{finding.line} [{finding.rule}] {finding.excerpt}",
            file=sys.stderr,
        )
    print("Remove the secret or replace it with an explicit non-secret fixture.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
