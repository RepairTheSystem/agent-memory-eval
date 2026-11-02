"""Bounded publication check. Reports locations, never matched secret values."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    'API key': re.compile(r'(?<![\w-])(?:sk-or-v1-|sk-proj-|ghp_|github_pat_)[A-Za-z0-9_-]{16,}'),
    'private key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'private IP URL': re.compile(r'https?://(?:10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+)(?::\d+)?'),
    'internal host': re.compile(r'https?://[^\s/]+\.(?:internal|corp|local)(?:[:/\s]|$)',re.I),
    'embedded URL credentials': re.compile(r'https?://[^\s/:@]+:[^\s/@]+@'),
}


def main():
    issues = []
    count = 0
    skipped = {'.git','__pycache__','.venv','venv','reports','datasets'}
    for path in ROOT.rglob('*'):
        rel = path.relative_to(ROOT)
        if any(part in skipped for part in rel.parts) or not path.is_file():
            continue
        count += 1
        if path.suffix.lower() in {'.java','.class','.jar','.db','.sqlite','.sqlite3','.pem','.p12','.pfx'}:
            issues.append(f'{rel}: excluded file type')
        if (path.name.endswith('.env') or path.name.startswith('.env')) and not path.name.endswith('.example'):
            issues.append(f'{rel}: local settings file')
        content = path.read_text(encoding='utf-8')
        for label, pattern in PATTERNS.items():
            for match in pattern.finditer(content):
                line = content.count('\n',0,match.start()) + 1
                issues.append(f'{rel}:{line}: {label}')
    if issues:
        raise SystemExit('\n'.join(issues))
    print(f'Release check passed: {count} files; no excluded file types or configured credential/internal-URL patterns found.')
    print('Pattern checking is not a complete confidentiality or ownership audit.')


if __name__ == '__main__':
    main()
