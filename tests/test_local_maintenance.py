import hashlib
from pathlib import Path
import sqlite3

import pytest

from scripts.local_maintenance import backup_private, inspect_legacy, snapshot_legacy, import_legacy_documents, prepare_update
from server.config import settings


def test_legacy_schema_inspection_and_snapshot_do_not_mutate_source(tmp_path):
    source = tmp_path / 'posteks.db'
    with sqlite3.connect(source) as connection:
        connection.execute('CREATE TABLE "odd""name" (id INTEGER PRIMARY KEY, private_text TEXT)')
        connection.execute('INSERT INTO "odd""name" (private_text) VALUES (?)', ('secret-content-not-in-report',))
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    report = inspect_legacy(source)
    assert report['tables'][0]['row_count'] == 1
    assert 'secret-content-not-in-report' not in str(report)
    assert not report['migration_run'] and not report['has_alembic_version']
    with pytest.raises(ValueError, match='confirm-stopped'):
        snapshot_legacy(source, tmp_path / 'copies', confirmed_stopped=False)
    snapshot = snapshot_legacy(source, tmp_path / 'copies', confirmed_stopped=True)
    assert inspect_legacy(snapshot)['tables'][0]['row_count'] == 1
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert not source.with_name(source.name + '-wal').exists()


def test_backup_includes_mail_history_and_attachments_but_env_stays_separate(tmp_path, monkeypatch):
    storage = tmp_path / 'storage'
    storage.mkdir()
    database = storage / 'working.db'
    with sqlite3.connect(database) as connection:
        connection.execute('CREATE TABLE private_data (id INTEGER)')
        connection.execute('INSERT INTO private_data VALUES (7)')
    for name in ['documents/manual.txt', 'mail/raw.eml', 'history/raw/history.json', 'attachments/photo.png']:
        path = storage / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('private fixture', encoding='utf-8')
    (tmp_path / '.env').write_text('TEST_SECRET=value', encoding='utf-8')
    (storage / '.env.private').write_text('PRIVATE_FIXTURE=value', encoding='utf-8')
    (storage / '.ENV.local').write_text('PRIVATE_UPPER_FIXTURE=value', encoding='utf-8')
    monkeypatch.setattr(settings, 'storage_dir', storage)
    monkeypatch.setattr(settings, 'database_url', 'sqlite:///' + str(database))
    copy = backup_private(tmp_path / 'backups')
    for name in ['documents/manual.txt', 'mail/raw.eml', 'history/raw/history.json', 'attachments/photo.png']:
        assert (copy / 'storage' / name).is_file()
    assert not (copy / '.env').exists()
    assert not (copy / 'storage' / '.env.private').exists()
    assert not (copy / 'storage' / '.ENV.local').exists()
    assert not (copy / 'storage' / 'working.db').exists()
    with sqlite3.connect(copy / 'database.sqlite3') as connection:
        assert connection.execute('SELECT id FROM private_data').fetchone()[0] == 7
    with pytest.raises(ValueError, match='внутрь storage'):
        backup_private(storage / 'backups')


def test_legacy_document_dry_run_rejects_path_escape_and_unknown_schema(tmp_path):
    old_docs = tmp_path / 'old-documents'
    old_docs.mkdir()
    (old_docs / 'manual.txt').write_text('PE38-12 инструкция', encoding='utf-8')
    outside = tmp_path / 'private.txt'
    outside.write_text('never import', encoding='utf-8')
    source = tmp_path / 'posteks.db'
    with sqlite3.connect(source) as connection:
        connection.execute('CREATE TABLE documents (filename TEXT, file_path TEXT)')
        connection.executemany('INSERT INTO documents VALUES (?, ?)', [('manual.txt', 'manual.txt'), ('private.txt', '../private.txt')])
    before = source.read_bytes()
    report = import_legacy_documents(source, old_docs, confirmed=False, backup_root=tmp_path / 'backups')
    assert report['dry_run'] and report['documents_found'] == 1 and len(report['issues']) == 1
    assert not report['source_migrations_run'] and source.read_bytes() == before
    other = tmp_path / 'unknown.db'
    with sqlite3.connect(other) as connection:
        connection.execute('CREATE TABLE unknown (id INTEGER)')
    with pytest.raises(ValueError, match='отдельная схема переноса'):
        import_legacy_documents(other, old_docs, confirmed=False, backup_root=tmp_path / 'backups')


def test_prepare_update_stops_before_backup_when_server_port_is_busy(tmp_path, monkeypatch):
    import socket
    class Connected:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def close(self):
            pass
    monkeypatch.setattr(socket, 'create_connection', lambda *args, **kwargs: Connected())
    with pytest.raises(ValueError, match='Остановите POSTEKS'):
        prepare_update(tmp_path / 'copies')
    assert not (tmp_path / 'copies').exists()


def test_dependency_free_update_guard_detects_a_real_listening_port():
    import socket
    from scripts.check_server_stopped import assert_server_stopped
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        port = listener.getsockname()[1]
        with pytest.raises(ValueError, match='Остановите POSTEKS'):
            assert_server_stopped(port=port)
    assert_server_stopped(port=port)


def test_update_guard_checks_port_after_connection_timeout(monkeypatch):
    import socket
    from scripts.check_server_stopped import assert_server_stopped
    def unavailable(*args, **kwargs):
        raise TimeoutError('fixture timeout')
    monkeypatch.setattr(socket, 'create_connection', unavailable)
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        port = listener.getsockname()[1]
        with pytest.raises(ValueError, match='Остановите POSTEKS'):
            assert_server_stopped(port=port)
    assert_server_stopped(port=port)


def test_update_guard_uses_exclusive_windows_bind_and_reports_denial(monkeypatch):
    import errno
    import socket
    import sys
    from unittest.mock import MagicMock, Mock
    from scripts.check_server_stopped import assert_server_stopped

    probe = Mock()
    manager = MagicMock()
    manager.__enter__.return_value = probe
    monkeypatch.setattr(sys, 'platform', 'win32')
    monkeypatch.setattr(socket, 'SO_EXCLUSIVEADDRUSE', -5, raising=False)
    monkeypatch.setattr(socket, 'create_connection', Mock(side_effect=TimeoutError))
    monkeypatch.setattr(socket, 'socket', Mock(return_value=manager))
    probe.bind.side_effect = OSError(errno.EACCES, 'fixture denial')
    with pytest.raises(ValueError, match=f'код ОС: {errno.EACCES}'):
        assert_server_stopped()
    probe.setsockopt.assert_called_once_with(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    probe.bind.assert_called_once_with(('127.0.0.1', 8080))
