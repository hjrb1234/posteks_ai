"""Build a source + prebuilt frontend archive, excluding all private local data.

The earlier private archive is preserved outside the checkout and never overwritten.
"""
from pathlib import Path
import argparse
import hashlib
import tempfile
import zipfile
from .publication_guard import inspect, inspect_zip, forbidden

ROOT = Path(__file__).resolve().parent.parent
DESTINATION = ROOT / 'downloads' / 'posteks_ai_windows.zip'
FILES = ['.env.example', '.gitignore', 'AGENTS.md', 'README.md', 'CHANGELOG.md', 'TESTING_GUIDE_RU.md', 'alembic.ini',
         'requirements.txt', 'requirements.lock', 'requirements-browser.txt',
         'install.ps1', 'install.bat', 'start.ps1', 'start.bat', 'open-app.bat', 'check.bat', 'check.ps1']
DIRECTORIES = ['server', 'scripts', 'migrations', 'tests', 'docs', 'database', '.devcontainer', '.agents/skills',
               'frontend/src', 'frontend/preview', 'frontend/public', 'frontend/dist']
FRONTEND_FILES = ['package.json', 'package-lock.json', 'tsconfig.json', 'vite.config.ts', 'vite.preview.config.ts', 'index.html']


def main():
    parser = argparse.ArgumentParser(description='Локальная безопасная сборка программного ZIP без секретов и данных')
    parser.add_argument('--output', type=Path, default=DESTINATION, help='Локальный путь ZIP; ничего не публикуется')
    args = parser.parse_args()
    destination = args.output.resolve()
    if destination.suffix.lower() != '.zip':
        raise ValueError('Выберите выходной файл с расширением .zip')
    paths = [ROOT / name for name in FILES] + [ROOT / 'frontend' / name for name in FRONTEND_FILES]
    for name in DIRECTORIES:
        directory = ROOT / name
        if directory.exists():
            paths.extend(p for p in directory.rglob('*') if p.is_file())
    seen = set()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=destination.stem + '-', suffix='.tmp', dir=destination.parent, delete=False) as stream:
        temporary = Path(stream.name)
    try:
        with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path in sorted(paths):
                relative = path.relative_to(ROOT)
                if (relative in seen or relative.as_posix() == 'docs/index.html' or forbidden(relative.as_posix()) or path.suffix in {'.pyc', '.tsbuildinfo', '.zip', '.tmp'}
                        or path.resolve() == destination):
                    continue
                if not path.is_file() or path.is_symlink():
                    raise ValueError('Expected regular release file missing: ' + str(relative))
                raw = path.read_bytes()
                issues = inspect(relative.as_posix(), raw)
                if issues:
                    raise ValueError('\n'.join(issues))
                seen.add(relative)
                archive.writestr(str(Path('posteks_ai') / relative).replace('\\', '/'), raw)
        issues = inspect_zip(temporary)
        if issues:
            raise ValueError('\n'.join(issues))
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    checksum = hashlib.sha256(destination.read_bytes()).hexdigest()
    destination.with_suffix('.zip.sha256').write_text(checksum + '  ' + destination.name + '\n')
    print(f'Safe local release: {destination}, {destination.stat().st_size} bytes, {len(seen)} files; no original documents/database/credentials; nothing published')

if __name__ == '__main__':
    main()
