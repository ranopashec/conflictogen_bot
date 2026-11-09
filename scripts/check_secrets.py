"""Fail closed on tracked env files, databases and recognizable credentials.

Print paths and rule names only, never the matched secret. Run before every push.
This is a guardrail, not proof that arbitrary secrets can always be recognized.
"""
from pathlib import Path
import re
import subprocess
import sys

RULES = {
    "Telegram bot token": re.compile(rb"\b[0-9]{7,}:[A-Za-z0-9_-]{30,}\b"),
    "Gemini API key": re.compile(rb"\bAIza[A-Za-z0-9_-]{30,}\b"),
    "Groq API key": re.compile(rb"\bgsk_[A-Za-z0-9_-]{32,}\b"),
    "GitHub access token": re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,})\b"),
    "private key": re.compile(rb"-----BEGIN (?:OPENSSH |RSA |EC |DSA )?PRIVATE KEY-----"),
}


def forbidden(path):
    p = Path(path)
    return ((p.name == ".env" or p.name.startswith(".env.")) and p.name != ".env.example"
            or any(part in {".venv", "venv", "secrets", "credentials", "__pycache__"} for part in p.parts)
            or p.suffix.lower() in {".key", ".pem", ".p12", ".pfx", ".db", ".log", ".bak", ".backup"}
            or ".sqlite" in p.name
            or p.parts[0] == "data" and p.name != ".gitkeep")


def main():
    root = Path(__file__).resolve().parent.parent
    paths = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode().split("\0")
    failures = []
    # Compare exact local configuration values as an additional pre-push check.
    known_secrets = []
    env = root / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            key, _, value = line.partition("=")
            if any(word in key.upper() for word in ("TOKEN", "SECRET", "KEY", "PASSWORD")) and len(value.strip()) >= 12:
                known_secrets.append(value.strip().encode())
    for path in filter(None, paths):
        if forbidden(path):
            failures.append((path, "forbidden tracked file"))
            continue
        content = subprocess.check_output(["git", "show", f":{path}"], cwd=root)
        for rule, pattern in RULES.items():
            if pattern.search(content):
                failures.append((path, rule))
        if any(secret in content for secret in known_secrets):
            failures.append((path, "exact local secret"))
    for path, rule in failures:
        print(f"BLOCKED: {path}: {rule}", file=sys.stderr)
    if failures:
        return 1
    print(f"Secret scan passed: {len(list(filter(None, paths)))} tracked files; no env files, databases or recognized credentials.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
