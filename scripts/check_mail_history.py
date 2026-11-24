"""Read-only mail diagnostics used by the local one-click check.

This module never queues/imports mail, moves a checkpoint, writes an audit event,
or changes remote flags. A live check compares UID coverage and reads only a few
bounded BODY.PEEK samples. It is not a substitute for the historical importer.
"""
from __future__ import annotations

from datetime import timedelta
from email import policy
from email.parser import BytesParser
import imaplib
import time

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from server import mail_sync
from server import workflows  # Register InboxMessage before mapper queries.
from server.config import effective_mode
from server.db import utcnow
from server.mail_models import MailFolderState, MailRemoteMessage


_KNOWN_ERRORS = {
    'imap_authentication_failed': 'Яндекс отклонил вход. Проверьте логин, пароль приложения и разрешение IMAP в локальном .env.',
    'imap_connection_failed': 'Соединение с IMAP не установлено. Проверьте сеть, имя сервера и порт; сообщения не изменялись.',
    'imap_tls_failed': 'Проверка TLS не прошла. Исправьте сертификаты или адрес сервера; не отключайте проверку TLS.',
    'invalid_mail_configuration': 'Проверьте MAIL_IMAP_HOST и числовой MAIL_IMAP_PORT в локальном .env.',
    'imap_protocol_failed': 'Сервер отклонил IMAP-команду. Проверьте доступ IMAP и выбранные папки.',
    'imap_folder_list_failed': 'Не удалось прочитать список папок. Проверьте доступ или задайте MAIL_SENT_FOLDER.',
    'imap_sent_folder_missing': 'Не обнаружена папка отправленных. Укажите её точное имя в MAIL_SENT_FOLDER.',
    'imap_folder_unavailable': 'Не удалось открыть папку только для чтения. Проверьте MAIL_MAILBOX и MAIL_SENT_FOLDER.',
    'imap_uidvalidity_missing': 'Сервер не сообщил UIDVALIDITY. Полноту истории подтвердить нельзя.',
    'imap_uidvalidity_changed': 'UIDVALIDITY изменился во время проверки. Запустите её снова; прежние UID нельзя сравнивать с новой папкой.',
    'imap_search_failed': 'Не удалось получить список писем за год; полнота не подтверждена.',
    'imap_fetch_failed': 'Не удалось прочитать образец письма. Передайте код, UID и этап из отчёта; импорт не запускался.',
    'imap_size_missing': 'Сервер не сообщил размер выбранного письма; чтение образца не подтверждено.',
    'imap_body_incomplete': 'Содержимое образца письма получено не полностью. Повторите проверку и передайте UID/этап.',
    'selfcheck_time_limit': 'Достигнут предел времени почтовой проверки. Уже полученные результаты сохранены; полный импорт не запускался.',
}
_SAFE_STAGES = {'size', 'body', 'body_partial', 'body_integrity'}
_SAFE_RESPONSE_CODES = {'AUTHENTICATIONFAILED', 'AUTHORIZATIONFAILED', 'EXPIRED', 'PRIVACYREQUIRED',
                        'NOPERM', 'LIMIT', 'TOOBIG', 'UNAVAILABLE', 'SERVERBUG', 'NONEXISTENT', 'CANNOT'}


def _record(identifier, status, title, details, evidence=None, resolution=''):
    return {'id': identifier, 'status': status, 'title': title, 'details': details,
            'evidence': evidence or {}, 'resolution': resolution}


def _safe_failure(error):
    failure = mail_sync._failure(error)
    code = failure.code if failure.code in _KNOWN_ERRORS else 'mail_check_failed'
    evidence = {'code': code}
    detail = failure.detail if isinstance(failure.detail, dict) else {}
    if isinstance(detail.get('uid'), int) and detail['uid'] > 0:
        evidence['uid'] = detail['uid']
    if detail.get('stage') in _SAFE_STAGES:
        evidence['stage'] = detail['stage']
    if detail.get('imap_status') in {'OK', 'NO', 'BAD', 'BYE', 'UNKNOWN'}:
        evidence['imap_status'] = detail['imap_status']
    if detail.get('response_code') in _SAFE_RESPONSE_CODES:
        evidence['response_code'] = detail['response_code']
    return evidence, _KNOWN_ERRORS.get(code, 'Почтовая проверка не выполнена. Передайте безопасный код ошибки из отчёта; секреты и тексты писем не нужны.')


def _kind(value):
    return value if value in {'inbox', 'sent'} else 'other'


def _local_history(session):
    """Use existing read-only status DTO; deliberately never call _control()."""
    current = mail_sync.get_mail_status(session)
    history = current['history']
    folders = [{'kind': _kind(folder.get('kind')), 'last_uid': folder.get('last_uid', 0),
                'history_last_uid': folder.get('history_last_uid', 0),
                'history_days': folder.get('history_days'),
                'history_completed': bool(folder.get('history_completed'))}
               for folder in current['folders']]
    both = {'inbox', 'sent'} <= {folder['kind'] for folder in folders}
    locally_completed = bool(history.get('completed') and int(history.get('days') or 0) >= 365 and both)
    evidence = {'stored_messages': current['stored_messages'], 'history_days': history.get('days'),
                'history_status': history.get('status'), 'history_run_id': history.get('run_id'),
                'remaining_in_search': history.get('remaining', 0),
                'unresolved_gaps': history.get('unresolved_skips', 0), 'folders': folders,
                'full_live_coverage_verified': False}
    if locally_completed:
        return _record('mail.history', 'pass', 'История почты в локальной базе',
            'Журнал показывает завершённый импорт не менее года для входящих и отправленных. '
            'Это состояние локальной базы, а не новая проверка текущего состава Яндекса.', evidence,
            'Для сравнения с текущими UID сервера запустите живую почтовую проверку.')
    return _record('mail.history', 'warn', 'История почты в локальной базе',
        'Полный год в обеих папках по локальному журналу не подтверждён. Недавняя синхронизация и число писем в списке не доказывают загрузку года.',
        evidence, 'В разделе «Входящие» запустите «Загрузить последний год». Дождитесь всех порций и проверьте пропуски. Повторный запуск не должен дублировать сохранённые письма.')


def check_mail_history(session, *, live=False, sample_limit=2, imap_factory=None):
    """Return safe check records without modifying the database or mailbox.

    ``imap_factory`` is injectable for protocol fixtures. External connections
    are still disabled in effective TEST mode. At most ten sample bodies can
    be requested, and the default is two across both folders combined.
    """
    results = []
    config = mail_sync.mail_config()
    missing = [name for name, field in [('MAIL_IMAP_HOST', 'host'), ('MAIL_USERNAME', 'username'),
               ('MAIL_APP_PASSWORD', 'password')] if not config.get(field)]
    results.append(_record('mail.config', 'warn' if missing else 'pass', 'Настройки чтения почты',
        'Не все обязательные настройки заданы.' if missing else 'Обязательные настройки присутствуют; соединение ещё не подтверждено.',
        {'configured': not missing, 'missing_variable_names': missing, 'sending_enabled': False},
        'Заполните недостающие имена в локальном .env. Пароль приложения и OAuth/session-токены в отчёт и чат не передавайте.' if missing else ''))
    try:
        with session.no_autoflush:
            results.append(_local_history(session))
            mode = effective_mode(session)
    except SQLAlchemyError:
        results.append(_record('mail.history', 'fail', 'История почты в локальной базе',
            'Не удалось прочитать схему или состояние почтовой базы.', {'code': 'mail_database_query_failed'},
            'Проверьте путь к рабочей БД и применение миграций. Не заменяйте существующую БД пустой.'))
        results.append(_record('mail.live', 'skipped', 'Проверка IMAP', 'Живая проверка пропущена из-за ошибки чтения базы.'))
        return results
    if not live or missing or mode == 'TEST':
        reason = ('Живое подключение не запрошено.' if not live else
                  'Без обязательных настроек вход невозможен.' if missing else
                  'Режим TEST запрещает подключение к реальному IMAP.')
        results.append(_record('mail.live', 'skipped', 'Проверка IMAP', reason,
            {'provider_called': False, 'import_started': False, 'sample_limit': 0},
            'Проверка не запускала импорт года и не отправляла писем.'))
        return results

    limit = min(max(int(sample_limit), 0), 10)
    key = mail_sync.account_key(config)
    deadline = time.monotonic() + 60
    client = None
    folder_reports, sample_reports, candidates = [], [], []
    def remaining_time():
        if time.monotonic() >= deadline:
            raise mail_sync.MailFailure('selfcheck_time_limit', '')
    def bounded_factory(*args, **kwargs):
        kwargs['timeout'] = min(float(kwargs.get('timeout', 12)), 12)
        return imaplib.IMAP4_SSL(*args, **kwargs)
    try:
        client = mail_sync._connect(config, imap_factory or bounded_factory)
        folders = mail_sync.discover_folders(client, config)
        since = (utcnow() - timedelta(days=365)).date().isoformat()
        for folder, kind in folders[:2]:
            remaining_time()
            epoch = mail_sync._select_folder(client, folder)
            uids = mail_sync._search(client, 'SINCE ' + mail_sync._date_criterion(since))
            with session.no_autoflush:
                known = set(session.scalars(select(MailRemoteMessage.uid).where(
                    MailRemoteMessage.account_key == key, MailRemoteMessage.folder == folder,
                    MailRemoteMessage.uidvalidity == epoch, MailRemoteMessage.inbox_id.is_not(None))))
            missing_uids = set(uids) - known
            folder_reports.append({'kind': _kind(kind), 'mailbox_messages': getattr(client, '_posteks_selected_count', None),
                'year_messages_found': len(uids), 'stored_uids_in_year': len(set(uids) & known),
                'missing_uids_count': len(missing_uids), 'first_missing_uids': sorted(missing_uids)[:10],
                'uidvalidity': int(epoch) if str(epoch).isdigit() else None})
            # Take one old UID from each folder before filling any extra budget.
            if uids:
                candidates.append((folder, kind, uids[0], epoch))
        for folder, kind, uid, epoch in candidates[:limit]:
            remaining_time()
            if mail_sync._select_folder(client, folder) != epoch:
                raise mail_sync.MailFailure('imap_uidvalidity_changed', '')
            raw, skipped = mail_sync._fetch(client, uid, heartbeat=remaining_time)
            item = {'kind': _kind(kind), 'uid': uid, 'body_read': raw is not None,
                    'bytes': len(raw) if raw is not None else 0}
            if skipped:
                item['code'] = 'message_too_large' if skipped == 'message_too_large' else 'sample_not_read'
            elif not BytesParser(policy=policy.default).parsebytes(raw, headersonly=True).get('From'):
                item.update(body_read=False, code='invalid_mime_headers')
            sample_reports.append(item)
        failures = [sample for sample in sample_reports if not sample['body_read']]
        results.append(_record('mail.live', 'warn' if failures or not sample_reports else 'pass',
            'Чтение образцов IMAP без изменения писем',
            'Папки открыты только для чтения; образцы прочитаны через BODY.PEEK. '
            'Это не импорт писем в базу и не запуск загрузки всего года.',
            {'provider_called': True, 'read_only': True, 'sample_limit': limit,
             'samples': sample_reports, 'folders_checked': len(folder_reports),
             'import_started': False, 'remote_flags_changed': False},
            'Если образец не прочитан, проверьте его безопасный код. Полнота импорта оценивается отдельным результатом ниже.'))
        both = {'inbox', 'sent'} <= {r['kind'] for r in folder_reports}
        gaps = sum(folder['missing_uids_count'] for folder in folder_reports)
        found = sum(folder['year_messages_found'] for folder in folder_reports)
        complete = bool(both and not gaps and found)
        results.append(_record('mail.coverage_live', 'pass' if complete else 'warn', 'Сопоставление UID за последний год',
            'Все найденные за год UID обеих папок связаны с локальными письмами.' if complete else
            'Полнота года не подтверждена: есть незагруженные UID, пустой диапазон или не проверены обе папки.',
            {'since': since, 'date_basis': 'IMAP INTERNALDATE', 'folders': folder_reports,
             'year_messages_found': found, 'missing_uids_count': gaps,
             'all_visible_year_uids_stored': complete, 'attachments_content_verified': False,
             'model_learning_verified': False},
            'При пропусках запустите обычный исторический импорт и дождитесь окончания. '
            'Наличие письма не подтверждает обработку его вложений или проверку технического решения.'))
    except Exception as error:
        evidence, resolution = _safe_failure(error)
        evidence.update(provider_called=True, read_only=True, import_started=False,
                        samples=sample_reports, folders_checked=len(folder_reports))
        results.append(_record('mail.live', 'fail', 'Проверка IMAP',
            'Живая почтовая проверка не завершена; полнота истории не подтверждена.', evidence, resolution))
    finally:
        mail_sync._logout(client)
    return results
