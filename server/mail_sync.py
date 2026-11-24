"""Durable read-only IMAP ingestion, bounded jobs, and private attachments.

Every remote operation uses SELECT readonly=True and BODY.PEEK. There is no
SMTP client, STORE, EXPUNGE, COPY, MOVE, or server-side mailbox mutation here.
"""
from __future__ import annotations

import base64
from datetime import timedelta
from email import policy
from email.parser import BytesParser
import hashlib
import imaplib
import io
import os
from pathlib import Path, PurePosixPath
import re
import socket
import ssl
import threading
import uuid
from zipfile import ZipFile, BadZipFile

from fastapi import HTTPException
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import effective_mode, settings
from .db import SessionLocal, utcnow
from .mail_models import MailAttachment, MailFolderState, MailRemoteMessage, MailSyncControl, MailSyncRun

MAX_IMAP_BYTES = 25 * 1024 * 1024
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
FETCH_CHUNK_BYTES = 1024 * 1024
_FETCH_AUTH_CODES = {'AUTHENTICATIONFAILED', 'AUTHORIZATIONFAILED', 'EXPIRED', 'PRIVACYREQUIRED', 'NOPERM'}
_worker_stop = threading.Event()
_worker_thread: threading.Thread | None = None
_worker_guard = threading.Lock()
_worker_restart_requested = False
_worker_client = None
_worker_heartbeat = None
_worker_error = None
_MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')


class MailFailure(Exception):
    def __init__(self, code, message, detail=None):
        self.code, self.message = code, message
        self.detail = detail or {}
        super().__init__(message)


def mail_config():
    def first(*names, default=''):
        return next((os.getenv(name) for name in names if os.getenv(name)), default)
    return {'host': first('IMAP_HOST', 'MAIL_IMAP_HOST'), 'username': first('IMAP_USER', 'IMAP_USERNAME', 'MAIL_USERNAME', 'MAIL_ADDRESS'),
            'password': first('IMAP_PASSWORD', 'MAIL_APP_PASSWORD'), 'port': first('IMAP_PORT', 'MAIL_IMAP_PORT', default='993'),
            'mailbox': first('MAIL_INBOX_FOLDER', 'MAIL_MAILBOX', default='INBOX'), 'sent_folder': first('MAIL_SENT_FOLDER')}


def account_key(config=None):
    config = config or mail_config()
    return hashlib.sha256(f"{config['host'].casefold()}|{config['port']}|{config['username'].casefold()}".encode()).hexdigest()


def masked_account(username):
    local, separator, domain = username.partition('@')
    return (local[:1] + '***' + ('@' + domain if separator else '')) if username else ''


def _iso(value):
    return value.isoformat() + 'Z' if value else None


def _safe_int(value, default, low, high):
    try:
        return min(high, max(low, int(value)))
    except (TypeError, ValueError):
        return default


def _control(session, key):
    item = session.get(MailSyncControl, key)
    if item is None:
        item = MailSyncControl(account_key=key, background_enabled=os.getenv('MAIL_POLL_ENABLED', 'false').lower() in {'true', '1', 'yes'}, poll_seconds=_safe_int(os.getenv('MAIL_POLL_SECONDS'), 60, 30, 3600))
        session.add(item)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            item = session.get(MailSyncControl, key)
    return item


def _require_config(session):
    if effective_mode(session) == 'TEST':
        raise HTTPException(409, {'code': 'test_mode', 'message': 'В TEST реальная почта отключена. Используйте импорт EML или mock IMAP.'})
    config = mail_config()
    if not all(config[name] for name in ('host', 'username', 'password')):
        raise HTTPException(409, {'code': 'missing_mail_configuration', 'message': 'Укажите MAIL_IMAP_HOST, MAIL_USERNAME и MAIL_APP_PASSWORD в серверном .env. Также поддерживаются IMAP_HOST, IMAP_USER, IMAP_PASSWORD.'})
    return config


def _failure(error):
    if isinstance(error, MailFailure):
        return error
    if isinstance(error, ssl.SSLError):
        return MailFailure('imap_tls_failed', 'Не удалось проверить TLS-сертификат IMAP. Проверьте сервер и доверенные сертификаты; проверка TLS не отключается.')
    if isinstance(error, imaplib.IMAP4.error):
        return MailFailure('imap_protocol_failed', 'Сервер IMAP отклонил команду. Проверьте доступ к IMAP, папку и журнал синхронизации.')
    if isinstance(error, (OSError, socket.timeout, TimeoutError)):
        return MailFailure('imap_connection_failed', 'IMAP недоступен: проверьте сеть, имя сервера и порт 993.')
    return MailFailure('mail_processing_failed', 'Не удалось обработать письмо или сохранить данные. Контрольная точка сохранена; повторите синхронизацию.')


def _connect(config, factory=None):
    try:
        try:
            port = int(config['port'])
        except (TypeError, ValueError):
            raise MailFailure('invalid_mail_configuration', 'Порт IMAP должен быть числом от 1 до 65535.') from None
        if not 1 <= port <= 65535 or any(char in config['host'] for char in '\r\n/\\'):
            raise MailFailure('invalid_mail_configuration', 'Некорректное имя IMAP-сервера или порт.')
        client = (factory or imaplib.IMAP4_SSL)(config['host'], port, ssl_context=ssl.create_default_context(), timeout=25)
    except Exception as error:
        raise _failure(error) from None
    try:
        result, _ = client.login(config['username'], config['password'])
        if result != 'OK':
            raise imaplib.IMAP4.error('login rejected')
    except imaplib.IMAP4.error:
        _logout(client)
        raise MailFailure('imap_authentication_failed', 'Яндекс отклонил вход. Проверьте имя ящика, пароль приложения и разрешение IMAP в настройках Яндекс Почты.') from None
    except Exception as error:
        _logout(client)
        raise _failure(error) from None
    return client


def _logout(client):
    if client is not None:
        try:
            client.logout()
        except Exception:
            pass


def _utf7_encode(text):
    result, pending = [], []
    def flush():
        if pending:
            result.append('&' + base64.b64encode(''.join(pending).encode('utf-16-be')).decode().rstrip('=').replace('/', ',') + '-')
            pending.clear()
    for char in text:
        if ' ' <= char <= '~':
            flush()
            result.append('&-' if char == '&' else char)
        else:
            pending.append(char)
    flush()
    return ''.join(result)


def _utf7_decode(text):
    def decode(match):
        value = match.group(1)
        if not value:
            return '&'
        value = value.replace(',', '/')
        try:
            return base64.b64decode(value + '=' * (-len(value) % 4)).decode('utf-16-be')
        except (ValueError, UnicodeError):
            return match.group(0)
    return re.sub(r'&([^-]*)-', decode, text)


def _folder_argument(name):
    if not name or len(name) > 512 or any(char in name for char in '\r\n\x00'):
        raise MailFailure('invalid_folder', 'Некорректное имя почтовой папки.')
    return '"' + _utf7_encode(name).replace('\\', '\\\\').replace('"', '\\"') + '"'


def discover_folders(client, config):
    inbox = config['mailbox']
    if config.get('sent_folder'):
        return [(inbox, 'inbox'), (config['sent_folder'], 'sent')] if config['sent_folder'] != inbox else [(inbox, 'inbox')]
    result, lines = client.list()
    if result != 'OK':
        raise MailFailure('imap_folder_list_failed', 'Не удалось получить список папок. Укажите MAIL_SENT_FOLDER явно.')
    candidates = []
    for line in lines or []:
        if not isinstance(line, bytes):
            continue
        text = line.decode('ascii', errors='replace')
        match = re.match(r'\(([^)]*)\)\s+(?:"[^"]*"|NIL)\s+(.+)$', text)
        if not match:
            continue
        flags, name = match.groups()
        name = name.strip()
        if name.startswith('"') and name.endswith('"'):
            name = name[1:-1].replace('\\"', '"').replace('\\\\', '\\')
        name = _utf7_decode(name)
        candidates.append((name, '\\sent' in flags.casefold()))
    sent = next((name for name, special in candidates if special), None)
    sent = sent or next((name for name, _ in candidates if name.casefold() in {'sent', 'sent messages', 'sent items', 'отправленные', 'inbox.sent'}), None)
    if not sent:
        raise MailFailure('imap_sent_folder_missing', 'Папка «Отправленные» не найдена. Укажите её точное имя в MAIL_SENT_FOLDER; входящая папка не подменяется отправленной.')
    return [(inbox, 'inbox'), (sent, 'sent')] if sent != inbox else [(inbox, 'inbox')]


def _response_number(client, name):
    try:
        _, values = client.response(name)
        for value in values or []:
            if isinstance(value, bytes) and re.fullmatch(rb'\d+', value.strip()):
                return int(value)
    except (AttributeError, TypeError):
        return None
    return None


def _select_folder(client, folder):
    result, selected = client.select(_folder_argument(folder), readonly=True)
    if result != 'OK':
        raise MailFailure('imap_folder_unavailable', 'IMAP не разрешил открыть выбранную папку. Проверьте MAIL_MAILBOX и MAIL_SENT_FOLDER.')
    validity = _response_number(client, 'UIDVALIDITY')
    if validity is None or validity <= 0:
        raise MailFailure('imap_uidvalidity_missing', 'Сервер не сообщил UIDVALIDITY. Синхронизация остановлена, чтобы не потерять письма.')
    # SELECT's count is a snapshot of this folder, not a filtered client count.
    # Do not issue SEARCH ALL on every batch just to populate a UI counter.
    count = next((int(value.strip()) for value in (selected or []) if isinstance(value, bytes) and value.strip().isdigit()), None)
    try:
        client._posteks_selected_count = count
    except (AttributeError, TypeError):
        pass
    return str(validity)


def _search(client, criteria):
    result, data = client.uid('search', None, criteria)
    if result != 'OK':
        raise MailFailure('imap_search_failed', 'Сервер не выполнил поиск UID. Контрольная точка не изменена.')
    return sorted({int(uid) for part in (data or []) if isinstance(part, bytes) for uid in part.split() if uid.isdigit() and int(uid) > 0})


def _date_criterion(iso_date):
    year, month, day = map(int, iso_date.split('-'))
    return f'{day:02d}-{_MONTHS[month - 1]}-{year}'


def _fetch_records(data):
    """Keep FETCH records separate, including metadata after a MIME literal.

    imaplib returns literal tuples followed by bytes with closing metadata; a
    server can also interleave an unsolicited FETCH for another message. MIME
    bytes must never be parsed as UID/size metadata or joined across messages.
    """
    records, current = [], None
    for part in data if isinstance(data, (list, tuple)) else [data]:
        header = part[0] if isinstance(part, tuple) and part else part
        if not isinstance(header, bytes):
            continue
        if re.match(rb'^(?:\*\s+)?\d+\s+\(', header.lstrip()):
            if current is not None:
                records.append(current)
            current = {'metadata': [], 'literals': []}
        if current is None:
            continue
        current['metadata'].append(header)
        if isinstance(part, tuple) and len(part) == 2 and isinstance(part[1], bytes):
            current['literals'].append((header, part[1]))
    if current is not None:
        records.append(current)
    for record in records:
        record['metadata'] = b' '.join(record['metadata'])
    return records


def _fetch_error(uid, stage, result=None, data=None, *, code='imap_fetch_failed'):
    # Persist only bounded protocol identifiers. A NO/BAD response can contain
    # a mailbox address or arbitrary server text, so never store it verbatim.
    response_code = None
    for part in data if isinstance(data, (list, tuple)) else [data]:
        if isinstance(part, bytes):
            match = re.match(rb'\s*\[([A-Z][A-Z0-9_-]{1,39})\]', part)
            if match:
                response_code = match.group(1).decode('ascii')
                break
    status = result.decode('ascii', errors='replace') if isinstance(result, bytes) else str(result or '')
    status = status.upper() if status.upper() in {'OK', 'NO', 'BAD', 'BYE'} else 'UNKNOWN'
    detail = {'uid': int(uid), 'stage': stage, 'imap_status': status}
    if response_code:
        detail['response_code'] = response_code
    stages = {'size': 'размера письма', 'body': 'содержимого письма',
              'body_partial': 'части письма', 'body_integrity': 'полноты письма'}
    suffix = f', ответ {status}' + (f' [{response_code}]' if response_code else '')
    return MailFailure(code, f'Не удалось получить {stages.get(stage, "письмо")} (UID {uid}{suffix}). '
                       'Письмо не отмечено загруженным; повторите загрузку истории. '
                       'Если ошибка повторится, передайте сотруднику UID, папку и этап из журнала.', detail)


def _fetch_payload(data, uid, expected_size, *, offset=None):
    matches = []
    for record in _fetch_records(data):
        metadata = record['metadata']
        record_uid = re.search(rb'\bUID\s+(\d+)\b', metadata, re.I)
        if not record_uid or int(record_uid.group(1)) != uid:
            continue
        size = re.search(rb'\bRFC822\.SIZE\s+(\d+)\b', metadata, re.I)
        if size and int(size.group(1)) != expected_size:
            raise _fetch_error(uid, 'body_integrity', 'OK', code='imap_body_incomplete')
        for header, raw in record['literals']:
            literal = re.search(rb'\bBODY\[\](?:<(\d+)>)?\s+\{(\d+)\+?\}\s*$', header, re.I)
            if not literal:
                continue
            origin = int(literal.group(1)) if literal.group(1) is not None else None
            if len(raw) != int(literal.group(2)) or (offset is None and origin not in {None, 0}):
                raise _fetch_error(uid, 'body_integrity', 'OK', code='imap_body_incomplete')
            if offset is not None and origin != offset:
                # A full BODY[] at offset zero is safe only if it really is the
                # complete message. Never duplicate a first chunk on later reads.
                if not (offset == 0 and origin is None and len(raw) == expected_size):
                    raise _fetch_error(uid, 'body_integrity', 'OK', code='imap_body_incomplete')
            matches.append(raw)
    if len(matches) != 1:
        return None
    return matches[0]


def _fetch(client, uid, heartbeat=None):
    uid = int(uid)
    result, data = client.uid('fetch', str(uid), '(UID RFC822.SIZE)')
    if result != 'OK':
        raise _fetch_error(uid, 'size', result, data)
    sizes = []
    for record in _fetch_records(data):
        metadata = record['metadata']
        identity = re.search(rb'\bUID\s+(\d+)\b', metadata, re.I)
        size = re.search(rb'\bRFC822\.SIZE\s+(\d+)\b', metadata, re.I)
        if identity and int(identity.group(1)) == uid and size:
            sizes.append(int(size.group(1)))
    if len(sizes) != 1:
        raise _fetch_error(uid, 'size', result, code='imap_size_missing')
    expected_size = sizes[0]
    if expected_size > MAX_IMAP_BYTES:
        return None, 'message_too_large'
    result, data = client.uid('fetch', str(uid), '(UID RFC822.SIZE BODY.PEEK[])')
    if result != 'OK':
        failure = _fetch_error(uid, 'body', result, data)
        if failure.detail.get('imap_status') in {'BAD', 'BYE'} or failure.detail.get('response_code') in _FETCH_AUTH_CODES:
            raise failure
    raw = _fetch_payload(data, uid, expected_size) if result == 'OK' else None
    if raw is not None and len(raw) == expected_size:
        return raw, None
    # Some IMAP servers reject a full literal while allowing bounded reads.
    # Retry this same UID with RFC-compliant BODY.PEEK[] ranges, never RFC822
    # (which could set Seen), and never move a checkpoint on a failed read.
    parts, offset = [], 0
    while offset < expected_size:
        count = min(FETCH_CHUNK_BYTES, expected_size - offset)
        result, data = client.uid('fetch', str(uid), f'(UID RFC822.SIZE BODY.PEEK[]<{offset}.{count}>)')
        if result != 'OK':
            raise _fetch_error(uid, 'body_partial', result, data)
        part = _fetch_payload(data, uid, expected_size, offset=offset)
        if part is None or not part or len(part) > expected_size - offset:
            raise _fetch_error(uid, 'body_integrity', result, code='imap_body_incomplete')
        # A server may ignore the partial range only for an entire first body.
        if len(part) != count and not (offset == 0 and len(part) == expected_size):
            raise _fetch_error(uid, 'body_integrity', result, code='imap_body_incomplete')
        parts.append(part)
        offset += len(part)
        if heartbeat:
            heartbeat()
    raw = b''.join(parts)
    if len(raw) != expected_size:
        raise _fetch_error(uid, 'body_integrity', 'OK', code='imap_body_incomplete')
    return raw, None


def _acquire(session, key):
    _control(session, key)
    token = uuid.uuid4().hex
    now = utcnow()
    changed = session.execute(update(MailSyncControl).where(MailSyncControl.account_key == key, or_(MailSyncControl.lease_until.is_(None), MailSyncControl.lease_until < now)).values(lease_owner=token, lease_until=now + timedelta(minutes=3), last_heartbeat=now))
    session.commit()
    if not changed.rowcount:
        raise HTTPException(409, {'code': 'mail_sync_busy', 'message': 'Синхронизация этого ящика уже выполняется.'})
    return token


def _heartbeat(session, key, token):
    changed = session.execute(update(MailSyncControl).where(MailSyncControl.account_key == key, MailSyncControl.lease_owner == token).values(lease_until=utcnow() + timedelta(minutes=3), last_heartbeat=utcnow()))
    if not changed.rowcount:
        raise MailFailure('mail_lease_lost', 'Другой обработчик получил синхронизацию. Текущая задача остановлена безопасно.')


def _release(session, key, token):
    session.execute(update(MailSyncControl).where(MailSyncControl.account_key == key, MailSyncControl.lease_owner == token).values(lease_owner=None, lease_until=None))
    session.commit()


def run_dict(run):
    result = {name: getattr(run, name) for name in ('id', 'kind', 'status', 'days', 'batch_size', 'imported', 'duplicates', 'fetched', 'errors', 'progress', 'has_more', 'stop_requested', 'error_code', 'error')}
    result.update({name: _iso(getattr(run, name)) for name in ('created_at', 'started_at', 'finished_at')})
    return result


def _folder_progress(run, part):
    """Assign a new JSON value so a checkpoint and its counters commit together."""
    values = [dict(value) for value in (run.progress or []) if value.get('folder') != part['folder']]
    values.append(dict(part))
    run.progress = values


def check_connection(session, actor='operator', imap_factory=None):
    config = _require_config(session)
    key, client = account_key(config), None
    control = _control(session, key)
    try:
        client = _connect(config, imap_factory)
        folders = discover_folders(client, config)
        for folder, _ in folders:
            _select_folder(client, folder)
        control.last_connection = utcnow()
        control.connection_error = None
        from .workflows import record_event
        record_event(session, actor, 'mail.connection_checked', 'mail', key[:16], {'read_only': True, 'folder_count': len(folders)})
        session.commit()
        return {'ok': True, 'connected': True, 'read_only': True, 'sending_enabled': False, 'account_masked': masked_account(config['username']), 'folders': [{'name': name, 'kind': kind} for name, kind in folders], 'last_check': _iso(control.last_connection), 'last_sync': None, 'message': 'IMAP-подключение проверено. Папки открыты только для чтения; письма не отправлялись.'}
    except Exception as error:
        failure = _failure(error)
        session.rollback()
        control = _control(session, key)
        control.connection_error = failure.code
        session.commit()
        raise HTTPException(502, {'code': failure.code, 'message': failure.message}) from None
    finally:
        _logout(client)


def sync_mailbox(session, *, kind='new', days=365, batch_size=100, actor='worker', imap_factory=None, run_id=None, stop_event=None):
    global _worker_client
    config = _require_config(session)
    if kind not in {'new', 'history'} or not 1 <= days <= 3650 or not 1 <= batch_size <= 500:
        raise HTTPException(422, 'Некорректный диапазон или размер пакета почты')
    key = account_key(config)
    token = _acquire(session, key)
    client = None
    run = session.get(MailSyncRun, run_id) if run_id else None
    if run is None:
        run = MailSyncRun(account_key=key, kind=kind, days=days, batch_size=batch_size, actor=str(getattr(actor, 'username', actor))[:160])
        session.add(run)
    elif run.status == 'failed' and run.error_code and run.error_code != 'partial_import':
        # A failed attempt did not advance its UID. After the same job retries,
        # it is not an unresolved skipped message. Preserve actual MIME/size
        # skips and expose failed attempts in the folder's diagnostic history.
        run.errors = max(0, int(run.errors or 0) - 1)
    run.status, run.started_at, run.finished_at = 'running', run.started_at or utcnow(), None
    run.error, run.error_code, run.has_more = None, None, False
    session.commit()
    try:
        client = _connect(config, imap_factory)
        if stop_event is not None:
            with _worker_guard:
                _worker_client = client
        control = _control(session, key)
        control.last_connection, control.connection_error = utcnow(), None
        session.commit()
        folders = discover_folders(client, config)
        previous_progress = {part.get('folder'): part for part in (run.progress or [])}
        run.progress = [dict(previous_progress.get(folder, {}), folder=folder, kind=folder_kind,
                             status='pending' if folder not in previous_progress else previous_progress[folder].get('status', 'pending'))
                        for folder, folder_kind in folders]
        session.commit()
        consecutive_fetch_failures = 0
        for folder, folder_kind in folders:
            if stop_event is not None and stop_event.is_set():
                raise MailFailure('worker_stopping', 'Обработчик завершает работу.')
            state = None
            session.refresh(run)
            if run.stop_requested:
                run.status = 'stopped'
                break
            validity = _select_folder(client, folder)
            state = session.scalar(select(MailFolderState).where(MailFolderState.account_key == key, MailFolderState.folder == folder))
            if state is None:
                state = MailFolderState(account_key=key, folder=folder, kind=folder_kind)
                session.add(state)
                session.flush()
            if state.uidvalidity != validity:
                state.uidvalidity, state.last_uid, state.history_last_uid = validity, 0, 0
                state.new_initialized, state.history_completed = False, False
                state.initial_upper_uid, state.initial_since = None, None
            if kind == 'history':
                if not state.history_since or state.history_days != days:
                    state.history_since = (utcnow().date() - timedelta(days=days)).isoformat()
                    state.history_days, state.history_last_uid, state.history_completed = days, 0, False
                cursor, since = state.history_last_uid, state.history_since
                upper = None
            else:
                cursor, since, upper = state.last_uid, None, None
                if not state.new_initialized:
                    if state.initial_upper_uid is None:
                        next_uid = _response_number(client, 'UIDNEXT')
                        state.initial_upper_uid = max(0, next_uid - 1) if next_uid else max(_search(client, 'ALL'), default=0)
                        state.initial_since = (utcnow().date() - timedelta(days=_safe_int(os.getenv('MAIL_INITIAL_DAYS'), 7, 1, 365))).isoformat()
                    since, upper = state.initial_since, state.initial_upper_uid
            session.commit()
            criteria = f'UID {cursor + 1}:{upper if upper is not None else "*"}'
            if since:
                criteria += ' SINCE ' + _date_criterion(since)
            uids = [] if upper == 0 else [uid for uid in _search(client, criteria) if uid > cursor and (upper is None or uid <= upper)]
            prior = previous_progress.get(folder, {})
            same_epoch = prior.get('uidvalidity') == validity
            retry_attempted = set(prior.get('retry_attempted_uids', [])) if same_epoch else set()
            consecutive_fetch_failures = int(prior.get('consecutive_fetch_failures', 0)) if same_epoch else 0
            if kind == 'history':
                # Failed bodies are durable gaps, not imported messages. A new
                # explicit history job retries gaps from earlier jobs once,
                # even when its high-water UID already passed them. Ordinary
                # new-mail polling does not hammer the old failed history.
                retry_uids = set(session.scalars(select(MailRemoteMessage.uid).where(
                    MailRemoteMessage.account_key == key, MailRemoteMessage.folder == folder,
                    MailRemoteMessage.uidvalidity == validity, MailRemoteMessage.status == 'retryable',
                    MailRemoteMessage.inbox_id.is_(None), MailRemoteMessage.created_at < run.started_at))) - retry_attempted
                if retry_uids:
                    in_period = set(_search(client, 'SINCE ' + _date_criterion(since)))
                    uids = sorted(set(uids) | (retry_uids & in_period))
            batch, more = uids[:batch_size], len(uids) > batch_size
            issues = list(previous_progress.get(folder, {}).get('issues', []))[:100]
            prior = previous_progress.get(folder, {})
            # A changed UID epoch invalidates earlier per-run cursor counts.
            prior_processed = int(prior.get('processed', 0)) if prior.get('uidvalidity') == validity else 0
            part = {'folder': folder, 'kind': folder_kind, 'uidvalidity': validity,
                    'last_uid': state.last_uid, 'history_last_uid': state.history_last_uid,
                    'period_since': since, 'mailbox_messages': getattr(client, '_posteks_selected_count', None),
                    'processed': prior_processed,
                    'failed_attempts': int(prior.get('failed_attempts', 0)),
                    'eligible_in_run': max(int(prior.get('eligible_in_run', 0)) if prior_processed else 0, prior_processed + len(uids)),
                    'remaining_in_search': len(uids), 'history_completed': False, 'issues': issues, 'status': 'running'}
            part['retry_attempted_uids'] = sorted(retry_attempted)
            part['consecutive_fetch_failures'] = consecutive_fetch_failures
            _folder_progress(run, part)
            run.has_more = bool(run.has_more or more)
            # The per-message refresh below must not discard this flag. It is
            # the worker's instruction to queue the next bounded history batch.
            session.commit()
            for batch_index, uid in enumerate(batch):
                if stop_event is not None and stop_event.is_set():
                    raise MailFailure('worker_stopping', 'Обработчик завершает работу.')
                session.refresh(run)
                if run.stop_requested:
                    run.status, run.has_more = 'stopped', True
                    break
                remote = session.scalar(select(MailRemoteMessage).where(MailRemoteMessage.account_key == key, MailRemoteMessage.folder == folder, MailRemoteMessage.uidvalidity == validity, MailRemoteMessage.uid == uid))
                fatal_fetch_failure = None
                if remote is None or (remote.status == 'retryable' and remote.inbox_id is None):
                    retrying = remote is not None
                    if retrying:
                        retry_attempted.add(uid)
                        part['retry_attempted_uids'] = sorted(retry_attempted)
                    def fetch_heartbeat():
                        # A large ranged read can span several network calls.
                        # Renew the lease without acknowledging this UID yet.
                        session.refresh(run)
                        if (stop_event is not None and stop_event.is_set()) or run.stop_requested:
                            raise MailFailure('worker_stopping', 'Чтение письма прервано по запросу остановки; UID будет прочитан повторно.')
                        _heartbeat(session, key, token)
                        session.commit()
                    read_failure = None
                    try:
                        raw, skip_reason = _fetch(client, uid, heartbeat=fetch_heartbeat)
                        run.fetched += 1
                        consecutive_fetch_failures = 0
                    except MailFailure as failure:
                        # A syntactically valid NO for one body, or a malformed
                        # body for that UID, need not prevent reading all the
                        # healthy later messages. Record the gap atomically.
                        # Authentication, socket loss, BAD commands and size
                        # failures still stop immediately without being skipped.
                        isolatable = (failure.code == 'imap_body_incomplete' or
                            (failure.code == 'imap_fetch_failed' and failure.detail.get('stage') == 'body_partial'
                             and failure.detail.get('imap_status') == 'NO'
                             and failure.detail.get('response_code') not in _FETCH_AUTH_CODES))
                        if not isolatable:
                            raise
                        raw, skip_reason, read_failure = None, failure.code, failure
                        consecutive_fetch_failures += 1
                        if consecutive_fetch_failures >= 3:
                            fatal_fetch_failure = MailFailure('imap_repeated_fetch_failed',
                                f'Чтение остановлено после трёх последовательных ошибок писем (последний UID {uid}). '
                                'Незагруженные UID сохранены для повторной попытки. Проверьте журнал и повторите историю; '
                                'письма и флаги в Яндексе не изменены.', failure.detail)
                    inbox_id, status = None, 'skipped'
                    if raw is not None:
                        from .workflows import InboxMessage, import_eml_batch
                        result = import_eml_batch(session, [(f'uid-{uid}.eml', raw)], actor, source='IMAP_HISTORY' if kind == 'history' else 'IMAP_NEW', commit=False, max_bytes=MAX_IMAP_BYTES, direction='outgoing' if folder_kind == 'sent' else 'incoming')
                        if result['errors']:
                            skip_reason = 'invalid_mime'
                        else:
                            item = session.scalar(select(InboxMessage).where(InboxMessage.content_hash == hashlib.sha256(raw).hexdigest()))
                            if item is None:
                                from .workflows import parse_eml
                                fields = parse_eml(raw, max_bytes=MAX_IMAP_BYTES)
                                item = session.scalar(select(InboxMessage).where(InboxMessage.message_id == fields['message_id']))
                            inbox_id = item.id if item else None
                            status = 'imported' if result['imported'] else 'duplicate'
                            run.imported += result['imported']
                            run.duplicates += result['duplicates']
                            if item:
                                store_mail_attachments(session, item)
                    if skip_reason:
                        run.errors += 1
                        if read_failure:
                            status = 'retryable'
                        if len(issues) < 100:
                            issue = {'uid': uid, 'error_code': skip_reason, 'message':
                                (read_failure.message + ' UID сохранён в списке повторных попыток; остальные письма могут быть загружены.') if read_failure else
                                'Письмо превышает лимит 25 МБ и не загружено.' if skip_reason == 'message_too_large' else
                                'Некорректное MIME-письмо: импорт пропущен; исходное письмо в IMAP не изменено.'}
                            if read_failure:
                                issue['detail'] = read_failure.detail
                            issues.append(issue)
                    if remote is None:
                        session.add(MailRemoteMessage(account_key=key, folder=folder, uidvalidity=validity, uid=uid, inbox_id=inbox_id, status=status, direction='outgoing' if folder_kind == 'sent' else 'incoming', error_code=skip_reason))
                    else:
                        remote.inbox_id, remote.status, remote.error_code = inbox_id, status, skip_reason
                else:
                    run.duplicates += 1
                if kind == 'history':
                    state.history_last_uid = max(state.history_last_uid, uid)
                else:
                    state.last_uid = max(state.last_uid, uid)
                state.error = None
                part.update(processed=prior_processed + batch_index + 1,
                            remaining_in_search=max(0, len(uids) - batch_index - 1),
                            last_uid=state.last_uid, history_last_uid=state.history_last_uid,
                            consecutive_fetch_failures=consecutive_fetch_failures)
                _folder_progress(run, part)
                _heartbeat(session, key, token)
                # The message, remote identity and checkpoint commit atomically.
                session.commit()
                if fatal_fetch_failure:
                    raise fatal_fetch_failure
            if kind == 'new' and not more and run.status != 'stopped':
                state.new_initialized = True
                if upper is not None:
                    state.last_uid = max(state.last_uid, upper)
            if kind == 'history':
                state.history_completed = not more and run.status != 'stopped'
            state.last_sync, state.error = utcnow(), None
            part.update(last_uid=state.last_uid, history_last_uid=state.history_last_uid,
                        history_completed=state.history_completed,
                        status='stopped' if run.status == 'stopped' else 'pending' if more else 'completed')
            _folder_progress(run, part)
            _heartbeat(session, key, token)
            session.commit()
            if run.status == 'stopped':
                break
        if run.status != 'stopped':
            run.status = 'completed'
        if run.errors:
            run.error_code = 'partial_import'
            run.error = f'Синхронизация завершена с пропусками: {run.errors}. Подробности UID и причин указаны в progress. Письма на сервере не изменены.'
        run.finished_at = utcnow()
        from .workflows import record_event
        record_event(session, actor, 'mail.synchronized', 'mail_run', run.id, {'kind': kind, 'imported': run.imported, 'duplicates': run.duplicates, 'fetched': run.fetched, 'errors': run.errors})
        session.commit()
    except Exception as error:
        session.rollback()
        run = session.get(MailSyncRun, run.id)
        if (stop_event is not None and stop_event.is_set()) or run.stop_requested:
            # Shutdown pauses work. Committed message/checkpoint pairs survive,
            # and a restart resumes the same durable job without duplicates.
            run.status = 'stopped' if run.stop_requested else 'queued'
            run.has_more = not run.stop_requested
            run.error_code, run.error = None, None
            run.finished_at = utcnow() if run.stop_requested else None
        else:
            failure = _failure(error)
            run.status, run.error_code, run.error, run.finished_at = 'failed', failure.code, failure.message, utcnow()
            run.errors += 1
            control = _control(session, key)
            if failure.code.startswith('imap_') or failure.code == 'invalid_mail_configuration':
                control.connection_error = failure.code
            if 'state' in locals() and state is not None:
                state.error = failure.message
            if 'folder' in locals():
                saved = next((dict(value) for value in run.progress or [] if value.get('folder') == folder), {'folder': folder})
                saved.update(status='failed', error_code=failure.code, error=failure.message,
                             failed_attempts=int(saved.get('failed_attempts', 0)) + 1)
                if failure.detail:
                    saved['failure_detail'] = dict(failure.detail)
                _folder_progress(run, saved)
        session.commit()
    finally:
        _logout(client)
        _release(session, key, token)
        if stop_event is not None:
            with _worker_guard:
                if _worker_client is client:
                    _worker_client = None
    return run_dict(run)


_ALLOWED = {'.pdf': 'application/pdf', '.txt': 'text/plain', '.md': 'text/markdown', '.csv': 'text/csv', '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', '.doc': 'application/msword', '.xls': 'application/vnd.ms-excel', '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp', '.mp4': 'video/mp4', '.mov': 'video/quicktime', '.webm': 'video/webm', '.mp3': 'audio/mpeg', '.wav': 'audio/wav', '.m4a': 'audio/mp4', '.ogg': 'audio/ogg'}


def _attachment_allowed(filename, raw):
    suffix = Path(filename).suffix.casefold()
    if suffix not in _ALLOWED:
        return None, 'unsupported_file_type'
    if len(raw) > MAX_ATTACHMENT_BYTES:
        return None, 'attachment_too_large'
    if not raw:
        return None, 'empty_attachment'
    if suffix == '.pdf' and not raw.lstrip().startswith(b'%PDF-'):
        return None, 'file_signature_mismatch'
    if suffix in {'.doc', '.xls'} and not raw.startswith(bytes.fromhex('d0cf11e0a1b11ae1')):
        return None, 'file_signature_mismatch'
    if suffix in {'.docx', '.xlsx'}:
        try:
            with ZipFile(io.BytesIO(raw)) as archive:
                names = archive.namelist()
                prefix = 'word/' if suffix == '.docx' else 'xl/'
                if len(names) > 5000 or sum(item.file_size for item in archive.infolist()) > 120 * 1024 * 1024 or '[Content_Types].xml' not in names or not any(name.startswith(prefix) for name in names):
                    return None, 'invalid_office_archive'
        except (BadZipFile, OSError):
            return None, 'file_signature_mismatch'
    if suffix in {'.png', '.jpg', '.jpeg', '.webp'}:
        try:
            from PIL import Image
            with Image.open(io.BytesIO(raw)) as image:
                expected_format = {'.png': 'PNG', '.jpg': 'JPEG', '.jpeg': 'JPEG', '.webp': 'WEBP'}[suffix]
                if image.width * image.height > 30_000_000 or image.format != expected_format:
                    return None, 'image_limits_exceeded'
                image.verify()
        except Exception:
            return None, 'invalid_image'
    if suffix in {'.mp4', '.mov', '.m4a'} and raw[4:8] != b'ftyp':
        return None, 'file_signature_mismatch'
    if suffix == '.webm' and not raw.startswith(bytes.fromhex('1a45dfa3')):
        return None, 'file_signature_mismatch'
    if suffix == '.wav' and not (raw.startswith(b'RIFF') and raw[8:12] == b'WAVE'):
        return None, 'file_signature_mismatch'
    if suffix == '.ogg' and not raw.startswith(b'OggS'):
        return None, 'file_signature_mismatch'
    if suffix == '.mp3' and not (raw.startswith(b'ID3') or len(raw) > 1 and raw[0] == 255 and raw[1] & 0xE0 == 0xE0):
        return None, 'file_signature_mismatch'
    if suffix in {'.txt', '.md', '.csv'} and (raw.startswith((b'MZ', b'\x7fELF', b'PK\x03\x04')) or b'\x00' in raw[:4096] and not raw.startswith((b'\xff\xfe', b'\xfe\xff'))):
        return None, 'binary_file_with_text_extension'
    return _ALLOWED[suffix], None


def store_mail_attachments(session, inbox):
    message = BytesParser(policy=policy.default).parsebytes(inbox.raw_mime)
    metadata = []
    for part in message.walk():
        if part.is_multipart() or not (part.get_content_disposition() == 'attachment' or part.get_filename()):
            continue
        filename = PurePosixPath(str(part.get_filename() or 'attachment').replace('\\', '/')).name[:240]
        filename = re.sub(r'[\x00-\x1f\x7f]', '_', filename) or 'attachment'
        raw = part.get_payload(decode=True) or b''
        digest = hashlib.sha256(raw).hexdigest()
        item = session.scalar(select(MailAttachment).where(MailAttachment.inbox_id == inbox.id, MailAttachment.sha256 == digest, MailAttachment.filename == filename))
        if item is None:
            mime, reason = _attachment_allowed(filename, raw)
            path = None
            if not reason:
                directory = settings.storage_dir / 'mail_attachments'
                directory.mkdir(parents=True, exist_ok=True)
                target = directory / (digest + Path(filename).suffix.casefold())
                if not target.exists():
                    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(descriptor, 'wb') as stream:
                        stream.write(raw)
                path = str(target.resolve())
            item = MailAttachment(inbox_id=inbox.id, filename=filename, content_type=mime or part.get_content_type(), size=len(raw), sha256=digest, storage_path=path, status='blocked' if reason else 'available', reason=reason)
            session.add(item)
            session.flush()
        metadata.append({'id': item.id, 'name': item.filename, 'content_type': item.content_type, 'size': item.size, 'sha256': item.sha256, 'available': item.status == 'available', 'reason': item.reason, 'download_url': f'/api/mail/attachments/{item.id}/download' if item.status == 'available' else None})
        if len(metadata) >= 100:
            break
    inbox.attachments = metadata


def _history_status(states, runs, unresolved_skips=0, unresolved=None):
    run = next((item for item in runs if item.kind == 'history'), None)
    progress = list(run.progress or []) if run else []
    days = run.days if run else 365
    scanned = bool(run and run.status == 'completed' and not run.has_more and states and
                   all(state.history_days == days and state.history_completed for state in states))
    completed = bool(scanned and not run.errors and not unresolved_skips)
    status = run.status if run else 'not_started'
    if run and run.status == 'completed':
        status = 'partial' if run.errors or unresolved_skips else 'completed' if scanned else 'queued' if run.has_more else 'incomplete'
    counts = [part.get('mailbox_messages') for part in progress]
    return {'run_id': run.id if run else None, 'days': days, 'status': status,
            'completed': completed, 'scanned_complete': scanned,
            'imported': run.imported if run else 0, 'fetched': run.fetched if run else 0,
            'duplicates': run.duplicates if run else 0, 'errors': max(run.errors if run else 0, unresolved_skips),
            'job_errors': run.errors if run else 0, 'unresolved_skips': unresolved_skips,
            'unresolved': unresolved or [],
            'processed': sum(int(part.get('processed', 0)) for part in progress),
            'eligible_in_run': sum(int(part.get('eligible_in_run', 0)) for part in progress),
            'remaining': sum(int(part.get('remaining_in_search', 0)) for part in progress),
            'mailbox_messages': sum(counts) if counts and all(isinstance(count, int) for count in counts) else None,
            'has_more': bool(run and run.has_more), 'folders': progress,
            'date_basis': 'IMAP INTERNALDATE',
            'notice': ('История ещё не запрашивалась. Обычная первичная синхронизация не загружает весь год.' if not run else
                       'Импорт остановлен или не подтвердил полноту истории. Запустите его снова: сохранённые письма не дублируются.' if status in {'failed', 'stopped', 'incomplete'} else
                       'История просмотрена, но часть писем не сохранена. Проверьте пропущенные UID; повторный пустой опрос не устраняет эти пропуски.' if run.errors or unresolved_skips else
                       'Все доступные сообщения выбранных папок за запрошенный период обработаны.' if completed else
                       'История загружается порциями. Сохранённая контрольная точка позволяет продолжить импорт.')}


def get_mail_status(session):
    from .workflows import InboxMessage
    config, key = mail_config(), account_key()
    control = session.get(MailSyncControl, key)
    folder_states = list(session.scalars(select(MailFolderState).where(MailFolderState.account_key == key)))
    runs = list(session.scalars(select(MailSyncRun).where(MailSyncRun.account_key == key).order_by(MailSyncRun.id.desc()).limit(20)))
    last_history = session.scalar(select(MailSyncRun).where(MailSyncRun.account_key == key, MailSyncRun.kind == 'history').order_by(MailSyncRun.id.desc()).limit(1))
    covered = [state for state in folder_states if last_history and state.history_days == last_history.days and state.history_last_uid]
    unresolved_skips, unresolved = 0, []
    if covered:
        missing = select(MailRemoteMessage).where(MailRemoteMessage.account_key == key,
            MailRemoteMessage.status.in_(['skipped', 'retryable']), MailRemoteMessage.inbox_id.is_(None),
            or_(*( (MailRemoteMessage.folder == state.folder) & (MailRemoteMessage.uidvalidity == state.uidvalidity) &
                   (MailRemoteMessage.uid <= state.history_last_uid) for state in covered)))
        unresolved_skips = int(session.scalar(select(func.count()).select_from(missing.subquery())) or 0)
        unresolved = [{'folder': item.folder, 'uid': item.uid, 'error_code': item.error_code}
                      for item in session.scalars(missing.order_by(MailRemoteMessage.id).limit(100))]
    last_sync = max((state.last_sync for state in folder_states if state.last_sync), default=None)
    error_code = control.connection_error if control else None
    totals = {name: int(session.scalar(select(func.coalesce(func.sum(getattr(MailSyncRun, name)), 0)).where(MailSyncRun.account_key == key)) or 0) for name in ('imported', 'duplicates', 'fetched')}
    stored = int(session.scalar(select(func.count(func.distinct(MailRemoteMessage.inbox_id))).where(
        MailRemoteMessage.account_key == key, MailRemoteMessage.inbox_id.is_not(None))) or 0)
    configured = bool(config['host'] and config['username'] and config['password'])
    worker_alive = bool(_worker_thread and _worker_thread.is_alive())
    worker_status = ('restarting' if _worker_restart_requested else 'stopping') if worker_alive and _worker_stop.is_set() else 'running' if control and control.lease_until and control.lease_until > utcnow() else 'idle' if worker_alive else 'stopped'
    return {'configured': configured, 'connected': bool(configured and effective_mode(session) != 'TEST' and control and control.last_connection and not error_code),
            'mode': effective_mode(session), 'safe_mode': True, 'sending_enabled': False, 'imap_host': config['host'], 'username_present': bool(config['username']), 'account_masked': masked_account(config['username']), 'selected_mailbox': config['mailbox'],
            'last_check': _iso(control.last_connection) if control else None, 'last_sync': _iso(last_sync), 'error': error_code, 'last_error': runs[0].error if runs else None,
            'folders': [{name: getattr(state, name) for name in ('folder', 'kind', 'uidvalidity', 'last_uid', 'new_initialized', 'initial_upper_uid', 'initial_since', 'history_last_uid', 'history_since', 'history_days', 'history_completed', 'error')} | {'name': state.folder, 'last_sync': _iso(state.last_sync)} for state in folder_states],
            'worker': {'running': worker_alive, 'background_enabled': control.background_enabled if control else os.getenv('MAIL_POLL_ENABLED', 'false').lower() in {'true', '1', 'yes'}, 'poll_seconds': control.poll_seconds if control else _safe_int(os.getenv('MAIL_POLL_SECONDS'), 60, 30, 3600), 'last_heartbeat': _iso(_worker_heartbeat), 'status': worker_status, 'error': _worker_error},
            'totals': totals, 'stored_messages': stored,
            'import_window': {'initial_days': _safe_int(os.getenv('MAIL_INITIAL_DAYS'), 7, 1, 365),
                              'recommended_history_days': 365, 'normal_sync': 'incremental',
                              'history_date_basis': 'IMAP INTERNALDATE',
                              'notice': 'Первый обычный опрос ограничен начальными днями; затем загружаются новые UID. Для старых писем запустите исторический импорт.'},
            'history': _history_status(folder_states, [last_history] if last_history else [], unresolved_skips, unresolved),
            'recent_runs': [run_dict(run) for run in runs], 'capabilities': {'test_eml_import': True, 'read_only_connection_check': True, 'background_polling': True, 'history_import': True, 'outbound_sending': False}}


def queue_sync(session, kind='new', days=365, batch_size=100, actor='operator'):
    config = _require_config(session)
    key = account_key(config)
    existing = session.scalar(select(MailSyncRun).where(MailSyncRun.account_key == key, MailSyncRun.status.in_(['queued', 'running'])).order_by(MailSyncRun.id.desc()))
    if existing:
        raise HTTPException(409, {'code': 'mail_sync_busy', 'message': 'Задача почты уже ожидает или выполняет синхронизацию.', 'run_id': existing.id})
    run = MailSyncRun(account_key=key, kind=kind, days=days, batch_size=batch_size, actor=str(getattr(actor, 'username', actor))[:160])
    session.add(run)
    session.commit()
    return run_dict(run)


def configure_worker(session, enabled, poll_seconds, actor='operator'):
    control = _control(session, account_key())
    control.background_enabled, control.poll_seconds = enabled, poll_seconds
    from .workflows import record_event
    record_event(session, actor, 'mail.worker_configured', 'mail', account_key()[:16], {'enabled': enabled, 'poll_seconds': poll_seconds})
    session.commit()
    return get_mail_status(session)['worker']


def _worker_loop(stop_event):
    global _worker_heartbeat, _worker_error
    while not stop_event.is_set():
        _worker_heartbeat = utcnow()
        try:
            with SessionLocal() as session:
                config = mail_config()
                key = account_key(config)
                control = _control(session, key)
                # A process may have stopped mid-command. A stale lease permits resume;
                # already committed UIDs remain the source of truth.
                if not control.lease_until or control.lease_until < utcnow():
                    session.execute(update(MailSyncRun).where(MailSyncRun.account_key == key, MailSyncRun.status == 'running').values(status='queued'))
                    session.commit()
                if effective_mode(session) != 'TEST' and all(config[name] for name in ('host', 'username', 'password')):
                    run = session.scalar(select(MailSyncRun).where(MailSyncRun.account_key == key, MailSyncRun.status == 'queued').order_by(MailSyncRun.id))
                    if run is None and control.background_enabled:
                        last = session.scalar(select(MailSyncRun).where(MailSyncRun.account_key == key, MailSyncRun.kind == 'new').order_by(MailSyncRun.id.desc()))
                        if last is None or (last.finished_at and last.finished_at <= utcnow() - timedelta(seconds=control.poll_seconds)):
                            queued = queue_sync(session, actor='mail-worker')
                            run = session.get(MailSyncRun, queued['id'])
                    if run:
                        if run.stop_requested:
                            run.status, run.finished_at = 'stopped', utcnow()
                            session.commit()
                        else:
                            result = sync_mailbox(session, kind=run.kind, days=run.days, batch_size=run.batch_size, actor=run.actor, run_id=run.id, stop_event=stop_event)
                            # A history run consumes bounded batches, yielding between them.
                            if result['status'] == 'completed' and result['has_more']:
                                run = session.get(MailSyncRun, result['id'])
                                run.status = 'queued'
                                session.commit()
                _worker_error = None
        except HTTPException as error:
            _worker_error = error.detail.get('code', 'mail_worker_blocked') if isinstance(error.detail, dict) else 'mail_worker_blocked'
        except Exception:
            _worker_error = 'mail_worker_failed'
        stop_event.wait(2)


def _interrupt_connection(client):
    """Interrupt our read-only socket; never issue IMAP CLOSE or mutations."""
    connection = getattr(client, 'sock', None)
    if connection is not None:
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            connection.close()
        except OSError:
            pass


def _launch_worker_locked():
    global _worker_stop, _worker_thread
    # A new generation must not clear the exiting generation's stop signal.
    _worker_stop = threading.Event()
    _worker_thread = threading.Thread(target=_worker_runner, args=(_worker_stop,), name='posteks-imap-readonly', daemon=True)
    _worker_thread.start()


def _worker_runner(stop_event):
    global _worker_thread, _worker_client, _worker_restart_requested
    try:
        _worker_loop(stop_event)
    finally:
        with _worker_guard:
            if _worker_thread is threading.current_thread():
                _worker_thread, _worker_client = None, None
                if _worker_restart_requested:
                    _worker_restart_requested = False
                    _launch_worker_locked()


def start_mail_worker():
    global _worker_restart_requested
    with _worker_guard:
        if _worker_thread and _worker_thread.is_alive():
            if _worker_stop.is_set():
                # Cleanup and lease release finish before its replacement runs.
                _worker_restart_requested = True
            return
        _launch_worker_locked()


def stop_mail_worker(timeout=5):
    global _worker_restart_requested
    with _worker_guard:
        _worker_restart_requested = False
        _worker_stop.set()
        thread, client = _worker_thread, _worker_client
    _interrupt_connection(client)
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout=max(0, timeout))
    return not thread or not thread.is_alive()
