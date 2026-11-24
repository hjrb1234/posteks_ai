"""Check staged/release paths for private files and recognizable secret material.

This complements manual review; it does not make arbitrary business documents public.
The release packager also uses an explicit allowlist and never reads storage/.
"""
from __future__ import annotations
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parent.parent
PRIVATE_DOCS = {'docs/TECHNICAL_SPEC.md', 'docs/AUDIT.md', 'docs/VALIDATION.md', 'docs/CLOUD_ENVIRONMENT.md'}
PRIVATE_PARTS = {'storage', 'backups', 'attachments', '.venv', 'node_modules', '.git', '__pycache__', 'artifacts', 'reports'}
PRIVATE_SUFFIXES = {'.db', '.sqlite', '.sqlite3', '.pdf', '.xlsx', '.xls', '.doc', '.docx', '.pem', '.key',
                    '.eml', '.msg', '.mbox', '.pst', '.ost'}
SYNTHETIC_MAIL_FIXTURE = 'tests/fixtures/service-request.eml'
PATTERNS = [
    re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    re.compile(rb'\bgh[pousr]_[A-Za-z0-9]{20,}\b'),
    re.compile(rb'\bgithub_pat_[A-Za-z0-9_]{30,}\b'),
    re.compile(rb'\bsk-(?:proj-)?[A-Za-z0-9_-]{25,}\b'),
    re.compile(rb'\bAKIA[0-9A-Z]{16}\b'),
    re.compile(rb'\b[0-9]{6,14}:AA[A-Za-z0-9_-]{25,}\b'),
    re.compile(rb'\b[0-9]{7,12}:[A-Za-z0-9_-]{30,50}\b'),
]


def forbidden(name: str) -> bool:
    path = PurePosixPath(name.replace('\\', '/'))
    normalized = str(path)
    if path.is_absolute() or '..' in path.parts:
        return True
    if PRIVATE_PARTS.intersection(part.casefold() for part in path.parts):
        return True
    if path.suffix.lower() in PRIVATE_SUFFIXES and normalized != SYNTHETIC_MAIL_FIXTURE:
        return True
    if normalized.casefold() in {private.casefold() for private in PRIVATE_DOCS} or normalized.casefold().startswith('docs/validation/'):
        return True
    if path.name.casefold().startswith('.env') and path.name.casefold() != '.env.example':
        return True
    return False


def inspect(name: str, raw: bytes) -> list[str]:
    issues = []
    if forbidden(name):
        issues.append(name + ': private file/path')
    if any(pattern.search(raw) for pattern in PATTERNS):
        issues.append(name + ': possible credential; inspect locally, value suppressed')
    if PurePosixPath(name.replace('\\', '/')).name.casefold() == '.env.example':
        for line in raw.decode('utf-8').splitlines():
            if '=' not in line or line.lstrip().startswith('#'):
                continue
            key, value = line.split('=', 1)
            secret_field = re.search(r'(?:^|_)(?:API_KEY|PASSWORD|PASSWORD_HASH|TOKEN|SECRET)$', key.strip().upper())
            if secret_field and value.strip():
                issues.append(name + ': secret template contains a nonempty value (' + key.strip() + ')')
    return issues


def inspect_zip(path: Path) -> list[str]:
    issues = []
    with zipfile.ZipFile(path) as archive:
        for item in archive.infolist():
            if item.is_dir():
                continue
            relative = PurePosixPath(item.filename.replace('\\', '/'))
            if relative.parts and relative.parts[0] == 'posteks_ai':
                relative = PurePosixPath(*relative.parts[1:])
            issues.extend(inspect(str(relative), archive.read(item)))
        bad = archive.testzip()
        if bad:
            issues.append('ZIP CRC failed: ' + bad)
    return issues


def main():
    paths = subprocess.check_output(['git', 'diff', '--cached', '--name-only', '-z', '--diff-filter=ACMR'], cwd=ROOT).decode().split('\0')
    issues = []
    count = 0
    for name in filter(None, paths):
        raw = subprocess.check_output(['git', 'show', ':' + name], cwd=ROOT)
        issues.extend(inspect(name, raw))
        if name.endswith('.zip'):
            issues.extend(inspect_zip(ROOT / name))
        count += 1
    if issues:
        print('\n'.join(issues), file=sys.stderr)
        return 1
    print(f'Publication guard passed: {count} staged files, no protected paths or recognized credentials')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
