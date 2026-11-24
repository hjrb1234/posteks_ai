"""Local diagnostics and private snapshots. Never migrate an unknown source DB."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import uuid

ROOT = Path(__file__).resolve().parent.parent


def _readonly(path: Path, *, immutable=False):
    if not path.is_file():
        raise ValueError('SQLite-файл не найден. Проверьте путь.')
    connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro' + ('&immutable=1' if immutable else ''), uri=True, timeout=15)
    connection.execute('PRAGMA query_only=ON')
    connection.execute('PRAGMA trusted_schema=OFF')
    return connection


def _quote_identifier(value: str):
    return '"' + value.replace('"', '""') + '"'


def inspect_legacy(path: Path) -> dict:
    """Schema/counts only: no message bodies, accounts, passwords or token values."""
    path = path.resolve()
    if not path.is_file():
        raise ValueError('SQLite-файл не найден.')
    wal = path.with_name(path.name + '-wal')
    result = {'path': str(path), 'size_bytes': path.stat().st_size, 'read_only': True,
              'migration_run': False, 'wal_present': wal.exists() and wal.stat().st_size > 0}
    if result['wal_present']:
        result['warning'] = 'Есть WAL. Остановите старое приложение и выполните snapshot-legacy --confirm-stopped; прямой immutable-анализ не учитывал бы WAL.'
        result['tables'] = []
        return result
    with _readonly(path, immutable=True) as connection:
        result['integrity_ok'] = connection.execute('PRAGMA quick_check(5)').fetchall() == [('ok',)]
        tables = []
        for name, in connection.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
            columns = [{'name': row[1], 'type': row[2]} for row in connection.execute('PRAGMA table_info(' + _quote_identifier(name) + ')')]
            # Do not query views or virtual tables (unknown modules may execute
            # user-defined behavior). sqlite_schema SQL is inspected, not run.
            definition = connection.execute('SELECT sql FROM sqlite_schema WHERE name=?', (name,)).fetchone()[0] or ''
            virtual = definition.lstrip().upper().startswith('CREATE VIRTUAL TABLE')
            count = None if virtual else connection.execute('SELECT COUNT(*) FROM ' + _quote_identifier(name)).fetchone()[0]
            tables.append({'name': name, 'columns': columns, 'row_count': count, 'virtual': virtual})
        result['tables'] = tables
        result['has_alembic_version'] = any(row['name'] == 'alembic_version' for row in tables)
    return result


def snapshot_legacy(source: Path, destination_root: Path, *, confirmed_stopped: bool) -> Path:
    """Copy stopped DB+WAL privately, then produce a consistent SQLite snapshot.

    The original is only read as bytes. Any WAL recovery happens on the private
    temporary copy, never on the user's original posteks.db.
    """
    if not confirmed_stopped:
        raise ValueError('Остановите старый сервис и подтвердите --confirm-stopped. Исходная БД не открывается на запись.')
    source = source.resolve()
    if not source.is_file():
        raise ValueError('Исходная SQLite не найдена.')
    destination_root = destination_root.resolve()
    destination_root.mkdir(parents=True, exist_ok=True)
    destination = destination_root / ('legacy-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8])
    destination.mkdir(exist_ok=False)
    try:
        with tempfile.TemporaryDirectory(prefix='legacy-read-', dir=destination) as temporary:
            temporary_path = Path(temporary) / 'source.sqlite3'
            paths = [source]
            wal = source.with_name(source.name + '-wal')
            if wal.exists():
                paths.append(wal)
            before = {str(path): (path.stat().st_size, path.stat().st_mtime_ns) for path in paths}
            shutil.copy2(source, temporary_path)
            if wal.exists():
                shutil.copy2(wal, temporary_path.with_name(temporary_path.name + '-wal'))
            after = {str(path): (path.stat().st_size, path.stat().st_mtime_ns) for path in paths}
            if before != after:
                raise ValueError('Источник изменился во время копирования. Остановите старое приложение и повторите.')
            output = destination / 'legacy_snapshot.sqlite3'
            with _readonly(temporary_path) as current, sqlite3.connect(output) as target:
                if current.execute('PRAGMA quick_check(5)').fetchall() != [('ok',)]:
                    raise ValueError('Проверка целостности копии SQLite не пройдена. Импорт остановлен.')
                current.backup(target)
        summary = inspect_legacy(output)
        (destination / 'schema_report.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
        (destination / 'manifest.json').write_text(json.dumps({'original_name': source.name,
            'snapshot': output.name, 'sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
            'original_modified': False, 'migrations_run': False, 'private': True}, indent=2), encoding='utf-8')
        return output
    except Exception:
        # Keep the diagnostic directory for inspection; never delete originals.
        (destination / 'INCOMPLETE.txt').write_text('Снимок не завершён. Не импортируйте его; исходный файл не изменён.', encoding='utf-8')
        raise


def diagnostics() -> dict:
    """Report effective local paths and binding presence, without secret values."""
    from dotenv import dotenv_values
    from sqlalchemy.engine import make_url
    values = {**dotenv_values(ROOT / '.env'), **os.environ}
    keys = ('DATABASE_URL', 'DB_URL', 'STORAGE_DIR', 'SERVICE_STORAGE_ROOT', 'OPENAI_API_KEY',
            'POSTEKS_OPENAI_API_KEY', 'IMAP_HOST', 'MAIL_IMAP_HOST', 'IMAP_USER', 'IMAP_USERNAME',
            'MAIL_USERNAME', 'MAIL_ADDRESS', 'IMAP_PASSWORD', 'MAIL_APP_PASSWORD', 'SAFE_MODE', 'MAIL_SEND_ENABLED')
    result = {'project_root': str(ROOT), 'env_file_present': (ROOT / '.env').is_file(),
              'bindings_present': {key: bool(values.get(key)) for key in keys}, 'warnings': []}
    for left, right in (('DATABASE_URL', 'DB_URL'), ('STORAGE_DIR', 'SERVICE_STORAGE_ROOT')):
        if values.get(left) and values.get(right) and values[left] != values[right]:
            result['warnings'].append(f'{left} и {right} заданы одновременно и отличаются. Проверьте эффективный путь.')
    try:
        configured_url = make_url(values.get('DATABASE_URL') or values.get('DB_URL') or '')
        if configured_url.get_backend_name() == 'sqlite':
            result['configured_database_path'] = configured_url.database
    except Exception:
        pass
    try:
        from server.config import settings
        database = make_url(settings.database_url)
        result.update({'configuration_valid': True, 'database_backend': database.get_backend_name(),
                       'storage_dir': str(settings.storage_dir), 'documents_dir': str(settings.documents_dir)})
        if database.get_backend_name() == 'sqlite':
            path = Path(database.database).resolve()
            result['database_path'] = str(path)
            result['database_exists'] = path.is_file()
            if path.is_file():
                report = inspect_legacy(path)
                result['schema'] = report
        else:
            result['warnings'].append('Для PostgreSQL путь не выводится. Проверяйте соединение и pg_dump отдельно.')
    except Exception as exc:
        result['configuration_valid'] = False
        result['configuration_error_type'] = type(exc).__name__
        result['warnings'].append('Конфигурация не применена. Проверьте DATABASE_URL/DB_URL и защиту неизвестного posteks.db; значения секретов не выводятся.')
    result['local_sqlite_candidates'] = [{'path': str(path.resolve()), 'size_bytes': path.stat().st_size}
        for path in sorted({*ROOT.glob('*.db'), *(ROOT / 'storage').glob('*.db')}) if path.is_file()]
    return result


def backup_private(destination_root: Path) -> Path:
    """Snapshot active SQLite plus all private storage, including new attachments."""
    from server.config import settings
    from sqlalchemy.engine import make_url
    database = make_url(settings.database_url)
    if database.get_backend_name() != 'sqlite':
        raise ValueError('Для PostgreSQL выполните pg_dump и скопируйте приватное storage; см. docs/DEPLOYMENT.md.')
    source = Path(database.database).resolve()
    if not source.is_file():
        raise ValueError('Активная SQLite не найдена. Сначала выполните diagnostics.')
    destination = destination_root.resolve() / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8])
    if destination.is_relative_to(settings.storage_dir.resolve()):
        raise ValueError('Резервную копию нельзя помещать внутрь storage.')
    destination.mkdir(parents=True, exist_ok=False)
    with _readonly(source) as current, sqlite3.connect(destination / 'database.sqlite3') as target:
        current.backup(target)
    excluded_database = {source, source.with_name(source.name + '-wal'), source.with_name(source.name + '-shm')}
    def ignore(directory, names):
        return [name for name in names if name in {'.cache', 'tessdata', '__pycache__'}
                or name.casefold().startswith('.env')
                or (Path(directory) / name).is_symlink()
                or (Path(directory) / name).resolve() in excluded_database]
    if settings.storage_dir.exists():
        shutil.copytree(settings.storage_dir, destination / 'storage', ignore=ignore, symlinks=False)
    manifest = {'created_at': datetime.now(timezone.utc).isoformat(), 'database': 'database.sqlite3',
                'original_database_path': str(source), 'storage': 'storage',
                'env_included': False, 'private': True,
                'note': 'Для согласованности БД и файлов остановите приложение перед копией. .env храните отдельно; восстановление — только при остановленном сервисе.'}
    (destination / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    return destination


def prepare_update(destination_root: Path) -> dict:
    """Stop-before-update guard plus private backup, before any new migration."""
    from scripts.check_server_stopped import assert_server_stopped
    from server.config import settings
    from sqlalchemy.engine import make_url
    assert_server_stopped()
    database = make_url(settings.database_url)
    if database.get_backend_name() != 'sqlite':
        raise ValueError('Перед обновлением PostgreSQL сделайте pg_dump и private storage backup вручную; автоматическое обновление Windows рассчитано на SQLite.')
    if Path(database.database).is_file():
        return {'backup_created': True, 'path': str(backup_private(destination_root))}
    return {'backup_created': False, 'reason': 'Первичная установка: рабочая SQLite ещё не существует.'}


def import_legacy_documents(source: Path, documents_root: Path, *, confirmed: bool, backup_root: Path) -> dict:
    """Copy an explicitly recognized document table; never run source migrations.

    This is deliberately not a universal account/case/history migration. Unknown
    schemas require their own reviewed mapping and leave the source untouched.
    """
    source, documents_root = source.resolve(), documents_root.resolve()
    if not documents_root.is_dir():
        raise ValueError('Каталог старых документов не найден.')
    report = inspect_legacy(source)
    if report.get('wal_present') or not report.get('integrity_ok'):
        raise ValueError('Нужен целостный snapshot-legacy без незавершённого WAL.')
    recognized = []
    for table in report['tables']:
        columns = {column['name'] for column in table['columns']}
        path_column = next((name for name in ('storage_name', 'file_path', 'storage_path', 'path') if name in columns), None)
        if table['name'] in {'documents', 'knowledge_documents', 'uploaded_documents'} and 'filename' in columns and path_column and not table['virtual']:
            recognized.append((table['name'], path_column, columns))
    if len(recognized) != 1:
        raise ValueError('Однозначная таблица документов не найдена. Поддерживаются documents/knowledge_documents/uploaded_documents с filename и storage_name/file_path/storage_path/path. Нужна отдельная схема переноса; источник не изменён.')
    table, path_column, columns = recognized[0]
    allowed = {'.pdf', '.docx', '.txt', '.md', '.xlsx', '.png', '.jpg', '.jpeg', '.webp'}
    candidates, issues = [], []
    with _readonly(source, immutable=True) as connection:
        connection.row_factory = sqlite3.Row
        for index, row in enumerate(connection.execute('SELECT * FROM ' + _quote_identifier(table) + ' LIMIT 1001'), 1):
            if index > 1000:
                raise ValueError('Более 1000 документов: разделите перенос на проверяемые партии.')
            if 'deleted' in columns and row['deleted']:
                continue
            filename = str(row['filename'] or '').replace('\\', '/').rsplit('/', 1)[-1][:400]
            raw_path = str(row[path_column] or '').replace('\\', '/')
            path = (documents_root / raw_path).resolve()
            if not path.is_relative_to(documents_root) or not path.is_file() or path.is_symlink():
                issues.append({'row': index, 'error': 'Файл отсутствует или выходит за разрешённый каталог документов'})
                continue
            if (Path(filename).suffix.lower() not in allowed or path.suffix.lower() not in allowed
                    or path.suffix.lower() != Path(filename).suffix.lower()
                    or path.stat().st_size > 25 * 1024 * 1024):
                issues.append({'row': index, 'error': 'Неподдерживаемый формат либо файл больше 25 МиБ'})
                continue
            candidates.append((filename, path, str(row['title'])[:500] if 'title' in columns and row['title'] else Path(filename).stem))
    result = {'dry_run': not confirmed, 'source_modified': False, 'source_migrations_run': False,
              'table': table, 'documents_found': len(candidates), 'issues': issues,
              'scope': 'Только оригиналы документов. Пользователи, пароли, обращения, письма и история не переносятся автоматически.'}
    if not confirmed:
        return result
    from sqlalchemy import select
    from sqlalchemy.engine import make_url
    from server.config import settings
    from server.db import SessionLocal, init_db
    from server.knowledge import ingest_document
    from server.models import Document
    from server.workflows import record_event
    target_url = make_url(settings.database_url)
    if target_url.get_backend_name() == 'sqlite' and Path(target_url.database).resolve() == source:
        raise ValueError('Источник и рабочая база совпадают. Выберите отдельную новую рабочую SQLite.')
    target_exists = target_url.get_backend_name() == 'sqlite' and Path(target_url.database).is_file()
    if target_exists:
        result['backup_path'] = str(backup_private(backup_root))
    init_db()
    imported, duplicates = [], 0
    for filename, path, title in candidates:
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        with SessionLocal() as session:
            if session.scalar(select(Document.id).where(Document.sha256 == digest, Document.deleted.is_(False))):
                duplicates += 1
                continue
            storage_name = uuid.uuid4().hex + Path(filename).suffix.lower()
            destination = settings.documents_dir / storage_name
            try:
                with destination.open('xb') as stream:
                    stream.write(raw)
                document = Document(title=title, filename=filename, storage_name=storage_name,
                    sha256=digest, mime_type='application/octet-stream', size_bytes=len(raw),
                    category='Импорт прежней базы', visibility='internal', status='queued',
                    indexing_details={'source_kind': 'legacy_import'})
                session.add(document)
                session.flush()
                record_event(session, 'local-cli', 'legacy.document_imported', 'document', document.id,
                             {'visibility': 'internal', 'source_table': table})
                session.commit()
                document_id = document.id
            except Exception:
                session.rollback()
                destination.unlink(missing_ok=True)
                raise
        ingest_document(document_id)
        with SessionLocal() as session:
            document = session.get(Document, document_id)
            imported.append({'id': document_id, 'status': document.status, 'visibility': document.visibility})
    result.update({'imported': imported, 'duplicates': duplicates})
    return result
