"""Safe local maintenance. Original posteks.db is never opened by these commands."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import getpass
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import uuid

ROOT = Path(__file__).resolve().parent.parent


def migrate():
    from server.db import init_db
    init_db()


def create_admin(args):
    migrate()
    from sqlalchemy import delete, select
    from server.auth import hash_password
    from server.db import SessionLocal
    from server.models import AuthSession, User
    username = (args.username or input('Имя администратора [admin]: ').strip() or 'admin').strip()
    if not 1 <= len(username) <= 100:
        raise ValueError('Имя должно содержать 1–100 символов')
    password = sys.stdin.readline().rstrip('\r\n') if args.password_stdin else getpass.getpass('Новый пароль (от 12 символов): ')
    if not args.password_stdin:
        if password != getpass.getpass('Повторите пароль: '):
            raise ValueError('Пароли не совпадают')
    if not 12 <= len(password) <= 256:
        raise ValueError('Пароль должен содержать от 12 до 256 символов')
    with SessionLocal() as session:
        user = session.scalar(select(User).where(User.username == username))
        if user:
            if not args.reset:
                raise ValueError('Пользователь существует. Для явного сброса используйте --reset')
            user.password_hash = hash_password(password)
            user.active = True
            user.role = 'admin'
            session.execute(delete(AuthSession).where(AuthSession.user_id == user.id))
        else:
            session.add(User(username=username, password_hash=hash_password(password), role='admin'))
        session.commit()
    print('Администратор сохранён. Пароль не выводится и не сохраняется в открытом виде.')


def has_admin():
    migrate()
    from sqlalchemy import select
    from server.db import SessionLocal
    from server.models import User
    with SessionLocal() as session:
        exists = session.scalar(select(User.id).where(User.role == 'admin', User.active == True).limit(1))
    return 0 if exists else 1


def import_sources(args):
    migrate()
    from sqlalchemy import select
    from server.config import settings
    from server.db import SessionLocal
    from server.knowledge import ingest_document
    from server.main import validate_magic
    from server.models import Document
    from server.workflows import import_equipment_workbook, record_event
    directory = Path(args.directory).resolve() if args.directory else settings.storage_dir / 'imports'
    if not directory.is_dir():
        if args.directory:
            raise ValueError('Папка исходников не найдена: ' + str(directory))
        print('Папка импорта в настроенном storage отсутствует; загрузите документы через Базу знаний. Рабочие данные не изменены.')
        return 0
    summaries = []
    for source in sorted(directory.iterdir()):
        if not source.is_file() or source.suffix.lower() not in {'.pdf', '.xlsx'}:
            continue
        if source.suffix.lower() == '.xlsx':
            with SessionLocal() as session:
                result = import_equipment_workbook(session, source.read_bytes(), 'local-cli')
                session.commit()
                summaries.append({'filename': source.name, 'equipment': result})
            continue
        if source.stat().st_size > settings.max_file_bytes:
            summaries.append({'filename': source.name, 'error': 'Файл превышает допустимый размер'})
            continue
        validate_magic(source, source.suffix.lower())
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        with SessionLocal() as session:
            existing = session.scalar(select(Document).where(Document.sha256 == digest, Document.deleted == False))
            if existing:
                doc_id = existing.id
                retry = existing.status != 'indexed'
            else:
                storage_name = str(uuid.uuid4()) + source.suffix.lower()
                shutil.copy2(source, settings.documents_dir / storage_name)
                title = source.stem.replace('_', ' ')
                if 'РЕ38_12' in source.name:
                    title = 'Формирователь PE38-12 · руководство по эксплуатации'
                elif 'РЕ38_17' in source.name:
                    title = 'Формирователи PE38-17 / PE38-25 · руководство'
                elif 'AZ01' in source.name:
                    title = 'Заклейщик AZ01 · руководство по эксплуатации'
                elif 'ре104' in source.name.lower():
                    title = 'Заклеивающая группа РЕ104 · руководство и деталировка'
                elif 'О компании' in source.name:
                    title = 'О компании ПОСТЭКС · внутренний документ'
                document = Document(title=title, filename=source.name, storage_name=storage_name,
                                    sha256=digest, mime_type='application/pdf', size_bytes=source.stat().st_size,
                                    visibility='internal', category='О компании' if 'О компании' in source.name else 'Руководство',
                                    status='queued')
                session.add(document)
                session.flush()
                doc_id = document.id
                retry = True
                record_event(session, 'local-cli', 'document.import', 'document', doc_id, {'visibility': 'internal'})
                session.commit()
        if retry:
            ingest_document(doc_id)
        with SessionLocal() as session:
            document = session.get(Document, doc_id)
            summaries.append({'filename': source.name, 'id': doc_id, 'status': document.status,
                              'pages': document.pages, 'chunks': document.chunks, 'visibility': document.visibility,
                              'error': document.error})
    print(json.dumps(summaries, ensure_ascii=False, indent=2))
    if any(item.get('error') or (item.get('status') and item['status'] != 'indexed') for item in summaries):
        return 1
    return 0


def backup(args):
    from scripts.local_maintenance import backup_private
    destination = backup_private(Path(args.directory))
    print('Резервная копия создана: ' + str(destination))


def main():
    parser = argparse.ArgumentParser(description='POSTEKS AI: локальное обслуживание новой базы')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('init-db', help='Применить миграции новой базы')
    commands.add_parser('has-admin', help='Код 0 если администратор создан, иначе 1')
    admin = commands.add_parser('create-admin', help='Создать администратора без тестового пароля')
    admin.add_argument('--username')
    admin.add_argument('--reset', action='store_true', help='Явно сбросить пароль существующего пользователя')
    admin.add_argument('--password-stdin', action='store_true', help='Прочитать пароль из stdin, не из аргумента/лога')
    sources = commands.add_parser('import-sources', help='Импортировать PDF и реестр XLSX; PDF закрытые по умолчанию')
    sources.add_argument('--directory', help='По умолчанию <эффективный STORAGE_DIR>/imports')
    copies = commands.add_parser('backup', help='Скопировать рабочую SQLite и всё приватное storage; .env хранится отдельно')
    copies.add_argument('--directory', default=str(ROOT / 'backups'))
    commands.add_parser('diagnostics', help='Локальные пути, схема и наличие привязок без значений секретов')
    update = commands.add_parser('prepare-update', help='Проверить остановку локального сервера и создать приватную копию перед миграциями')
    update.add_argument('--directory', default=str(ROOT / 'backups'))
    legacy = commands.add_parser('inspect-legacy', help='Прочитать только схему/число строк старой SQLite без миграций')
    legacy.add_argument('--source', required=True)
    snapshot = commands.add_parser('snapshot-legacy', help='Сделать приватную копию остановленной старой SQLite и WAL')
    snapshot.add_argument('--source', required=True)
    snapshot.add_argument('--directory', default=str(ROOT / 'backups'))
    snapshot.add_argument('--confirm-stopped', action='store_true')
    legacy_import = commands.add_parser('import-legacy-documents', help='Проверяемый перенос только документов известной схемы; по умолчанию dry-run')
    legacy_import.add_argument('--source', required=True)
    legacy_import.add_argument('--documents-root', required=True)
    legacy_import.add_argument('--confirm', action='store_true')
    legacy_import.add_argument('--backup-directory', default=str(ROOT / 'backups'))
    args = parser.parse_args()
    try:
        if args.command == 'init-db':
            migrate()
            print('Миграции новой базы применены')
        elif args.command == 'create-admin':
            create_admin(args)
        elif args.command == 'has-admin':
            return has_admin()
        elif args.command == 'import-sources':
            return import_sources(args)
        elif args.command == 'backup':
            backup(args)
        elif args.command == 'diagnostics':
            from scripts.local_maintenance import diagnostics
            print(json.dumps(diagnostics(), ensure_ascii=False, indent=2))
        elif args.command == 'prepare-update':
            from scripts.local_maintenance import prepare_update
            print(json.dumps(prepare_update(Path(args.directory)), ensure_ascii=False, indent=2))
        elif args.command == 'inspect-legacy':
            from scripts.local_maintenance import inspect_legacy
            print(json.dumps(inspect_legacy(Path(args.source)), ensure_ascii=False, indent=2))
        elif args.command == 'snapshot-legacy':
            from scripts.local_maintenance import snapshot_legacy
            print('Приватный снимок: ' + str(snapshot_legacy(Path(args.source), Path(args.directory), confirmed_stopped=args.confirm_stopped)))
        elif args.command == 'import-legacy-documents':
            from scripts.local_maintenance import import_legacy_documents
            print(json.dumps(import_legacy_documents(Path(args.source), Path(args.documents_root), confirmed=args.confirm, backup_root=Path(args.backup_directory)), ensure_ascii=False, indent=2))
    except Exception as exc:
        # Never print exception payloads that may contain a connection string/password.
        if isinstance(exc, ValueError) or (isinstance(exc, RuntimeError) and str(exc).startswith('База содержит таблицы без истории миграций.')):
            print(str(exc), file=sys.stderr)
        elif isinstance(exc, PermissionError):
            print('Нет прав на файл или каталог. Проверьте доступ к источнику и папке резервных копий. Исходные данные не удалялись.', file=sys.stderr)
        elif isinstance(exc, OSError):
            print('Ошибка файловой системы (' + type(exc).__name__ + '). Проверьте свободное место и доступ. Копия без manifest.json не завершена.', file=sys.stderr)
        elif isinstance(exc, sqlite3.DatabaseError):
            print('Не удалось прочитать SQLite. Проверьте путь, целостность и остановку старого сервиса; не запускайте миграции неизвестной БД.', file=sys.stderr)
        else:
            print('Операция не выполнена (' + type(exc).__name__ + '). Проверьте локальную конфигурацию и инструкцию.', file=sys.stderr)
        return 2 if args.command == 'has-admin' else 1
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
