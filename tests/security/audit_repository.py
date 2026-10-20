"""Read-only pre-commit audit. Reports locations/categories, never matched values.

Uses Git's candidate list, including ignored-file protection, and compares local
secret values only in memory. This is a targeted audit, not a general secret
scanner or a substitute for reviewing the staged diff.
"""
import hashlib
import json
from pathlib import Path
import re
import subprocess

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]


def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT)


def main():
    names = sorted(set(
        x.decode('utf-8') for x in git('ls-files', '-z', '--cached', '--others',
                                     '--exclude-standard').split(b'\0') if x
    ))
    secrets = []
    for env in (ROOT / 'backend/.env', ROOT / 'backend/backend/.env'):
        if env.is_file():
            for key, value in dotenv_values(env).items():
                if value and len(value) >= 8 and re.search(
                    r'SECRET|PASSWORD|TOKEN|ENCRYPTION|API_KEY|CLIENT_ID', key
                ):
                    secrets.append(value.encode('utf-8'))
    findings = []
    inventory = []
    patterns = {
        'private_key': rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
        'google_api_key': rb'AIza[0-9A-Za-z_-]{35}',
        'google_refresh_token': rb'1//[0-9A-Za-z_-]{40,}',
        'encrypted_token_literal': rb'gAAAA[A-Za-z0-9_-]{75,}',
        'aws_access_key': rb'(?:AKIA|ASIA)[A-Z0-9]{16}',
        'github_token': rb'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})',
        'personal_mail_provider': rb'[A-Za-z0-9._%+-]+@(?:gmail|hotmail|outlook|yahoo)\.[A-Za-z.]+',
    }
    forbidden = re.compile(r'(?:^|/)(?:\.env(?:\..*)?|.*\.(?:dump|fbackup|log|pgpass|pem|key|sqlite3))$')
    for name in names:
        path = ROOT / name
        data = path.read_bytes()
        inventory.append({'path': name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
        if forbidden.search(name) and not name.endswith(('.example', '.sample', '.template')):
            findings.append({'path': name, 'category': 'private_or_generated_filename'})
        if any(secret in data for secret in secrets):
            findings.append({'path': name, 'category': 'matches_local_secret'})
        for category, pattern in patterns.items():
            for match in re.finditer(pattern, data):
                findings.append({'path': name, 'line': data[:match.start()].count(b'\n') + 1,
                                 'category': category})
    ignored = {}
    for name in ('backend/.env', 'backend/backend/.env', 'backend/.venv/',
                 'frontend/node_modules/', 'frontend/dist/', 'docs/plan_de_travail_INF4173.pdf'):
        ignored[name] = subprocess.run(['git', 'check-ignore', '-q', name], cwd=ROOT).returncode == 0
    print(json.dumps({'candidate_count': len(names), 'findings': findings,
                      'ignored': ignored, 'inventory': inventory}, ensure_ascii=True, indent=2))
    return int(bool(findings) or not all(ignored.values()))


if __name__ == '__main__':
    raise SystemExit(main())
