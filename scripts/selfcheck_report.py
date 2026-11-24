"""Write a local, readable self-check and a deliberately limited support report.

Check titles/details/resolutions are diagnostic prose supplied by the runner,
not exception strings or material excerpts. Evidence and metadata use an
allowlist. Actual model answers/materials belong only to PRIVATE_details.json.
No report is uploaded or sent anywhere by this module.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape
import json
import math
import os
from pathlib import Path
import re
from typing import Any
import uuid


STATUSES = ('pass', 'fail', 'warn', 'skipped')
STATUS_NAMES = {'pass': 'Пройдено', 'fail': 'Ошибка', 'warn': 'Требует внимания', 'skipped': 'Не проверено'}
OVERALL_NAMES = {'passed': 'Все выполненные проверки пройдены',
    'failed': 'Обнаружены ошибки', 'partial': 'Проверка выполнена не полностью',
    'not_run': 'Проверки не выполнены'}
PRIVATE_WARNING = ('СОДЕРЖИТ КОНФИДЕНЦИАЛЬНЫЕ ДАННЫЕ. Здесь могут быть ответы модели, '
    'внутренние материалы и пути файлов. Не отправляйте этот файл вместе с отчётом поддержки.')

_SECRET_KEY = re.compile(r'(?:^|_)(?:api_key|password|password_hash|secret|token|access_token|refresh_token|'
    r'session_token|session_secret|authorization|cookie|set_cookie|credentials|username|user_name|email|'
    r'mail_address|mail_username)(?:$|_)', re.I)
_TOKENS = (
    re.compile(r'\bsk-(?:proj-)?[A-Za-z0-9_-]{12,}\b'),
    re.compile(r'\bgh[pousr]_[A-Za-z0-9]{20,}\b'),
    re.compile(r'\bgithub_pat_[A-Za-z0-9_]{20,}\b'),
    re.compile(r'\bAKIA[0-9A-Z]{16}\b'),
    re.compile(r'\b[0-9]{6,14}:[A-Za-z0-9_-]{25,}\b'),
    re.compile(r'\bBearer\s+[^\s\"\'<>]+', re.I),
)
_EMAIL = re.compile(r'[A-Z0-9.!#$%&\'*+/=?^_`{|}~-]+@[A-Z0-9.-]+\.[A-Z]{2,}', re.I)
_ASSIGNMENT = re.compile(r'\b(?:[A-Z0-9_]*(?:API_KEY|PASSWORD|PASSWORD_HASH|TOKEN|SECRET)|AUTHORIZATION|COOKIE)'
    r'\s*[:=]\s*(?:\"[^\"]*\"|\'[^\']*\'|[^\s,;]+)', re.I)
_USER_PATH = re.compile(r'(?:(?:[A-Z]:[\\/])?Users[\\/]|/home/)[^\\/\s]+', re.I)
_CODE = re.compile(r'[A-Za-z0-9_.:/+\-]{1,160}\Z')

_SAFE_KEYS = {
    'model', 'models', 'main', 'fast', 'vision', 'audio', 'embedding', 'configured_model',
    'configured', 'connected', 'key_present', 'openai_key_present', 'provider_called',
    'provider_status', 'provider_response_id', 'response_id', 'provider_request_id', 'request_id',
    'invocation_id', 'status', 'stage', 'failed_stage', 'failure_kind', 'exception_type',
    'http_status', 'http_status_code', 'provider_error_code', 'provider_error_param', 'error_code',
    'code', 'response_status', 'incomplete_reason', 'validation_errors', 'location', 'type',
    'diagnostics', 'provider_diagnostics', 'runtime', 'platform', 'python_version',
    'app_version', 'version', 'database_revision', 'revision', 'integrity', 'mode',
    'created_at', 'model_origin', 'key_configured', 'provider_calls_started',
    'test_material_kind', 'openai_sdk_version',
    'folder_kind', 'folder', 'folder_type', 'folders', 'kind', 'read_only',
    'sending_enabled', 'mail_send_enabled', 'flags_changed', 'checkpoint_changed',
    'import_started', 'history_complete', 'history_completed', 'completed', 'coverage_verified',
    'history_status', 'history_days', 'days', 'since', 'last_success', 'last_sync',
    'uid', 'last_uid', 'history_last_uid', 'uidvalidity', 'retryable',
    'available', 'indexed', 'pending', 'processed', 'imported', 'duplicates', 'errors', 'total',
    'checked', 'missing', 'unresolved', 'complete', 'expected_turns', 'successful_turns', 'sample_limit',
    'eligible', 'remaining', 'fetched', 'stored', 'sampled', 'mailbox_messages', 'unresolved_skips',
    'document_id', 'solution_id', 'page', 'record_version', 'sources', 'source_ids',
    'sha256', 'source_fingerprint', 'message_sha256', 'input_sha256', 'instructions_sha256',
    'duration_seconds', 'input_tokens', 'output_tokens', 'max_output_tokens', 'payload_chars',
    'history_turns', 'source_count', 'generation_calls', 'provider_calls', 'real_provider_calls',
    'live_requested', 'live_tested', 'live_comparison_completed', 'windows_execution_tested',
    'ocr_available', 'ffprobe_available', 'ffmpeg_available', 'available_languages', 'languages',
    'safe_mode', 'isolated', 'temporary_database', 'working_database_unchanged',
    'missing_variable_names', 'stored_messages', 'history_run_id', 'remaining_in_search',
    'unresolved_gaps', 'full_live_coverage_verified', 'samples', 'folders_checked',
    'remote_flags_changed', 'date_basis', 'year_messages_found', 'stored_uids_in_year',
    'missing_uids_count', 'first_missing_uids', 'all_visible_year_uids_stored',
    'attachments_content_verified', 'model_learning_verified', 'body_read', 'bytes',
    'imap_status', 'response_code', 'transport_error_type',
    'citation_error', 'citation_source_index', 'citation_retry', 'reasoning_tokens', 'usage',
}
_NESTED_KEYS = {'models', 'diagnostics', 'provider_diagnostics', 'runtime', 'folders',
                'source_ids', 'sources', 'validation_errors', 'samples', 'missing_variable_names',
                'first_missing_uids', 'citation_retry', 'usage'}
_NUMERIC_KEYS = {'days', 'history_days', 'uid', 'last_uid', 'history_last_uid', 'page', 'record_version',
    'document_id', 'solution_id', 'indexed', 'pending', 'processed', 'imported', 'duplicates', 'errors',
    'total', 'eligible', 'remaining', 'fetched', 'stored', 'sampled', 'mailbox_messages', 'unresolved_skips',
    'checked', 'missing', 'unresolved', 'expected_turns', 'successful_turns', 'sample_limit', 'provider_calls_started',
    'http_status', 'http_status_code', 'input_tokens', 'output_tokens', 'max_output_tokens',
    'payload_chars', 'history_turns', 'source_count', 'generation_calls', 'provider_calls', 'real_provider_calls',
    'stored_messages', 'history_run_id', 'remaining_in_search', 'unresolved_gaps', 'folders_checked',
    'year_messages_found', 'stored_uids_in_year', 'missing_uids_count', 'bytes',
    'citation_source_index', 'reasoning_tokens'}


def _redact_text(value: str, *, redact_email=True) -> str:
    text = value.replace('\x00', '')
    text = _ASSIGNMENT.sub('[СКРЫТО: секрет]', text)
    for pattern in _TOKENS:
        text = pattern.sub('[СКРЫТО: секрет]', text)
    if redact_email:
        text = _EMAIL.sub('[СКРЫТО: адрес]', text)
        text = _USER_PATH.sub('[СКРЫТО: папка пользователя]', text)
    return text


def _prose(value: Any, limit=2000) -> str:
    if not isinstance(value, str):
        return ''
    if 'Traceback (most recent call last)' in value:
        return 'Технический сбой. Исходная трассировка исключена из отчёта поддержки.'
    return _redact_text(value)[:limit]


def _evidence(value: Any, key='', depth=0):
    if depth > 6:
        return None
    if isinstance(value, dict):
        result = {}
        for name, item in value.items():
            if not isinstance(name, str) or _SECRET_KEY.search(name):
                # Token *counts* have separate, harmless numeric fields.
                if name not in {'input_tokens', 'output_tokens', 'max_output_tokens'}:
                    continue
            normalized = name.casefold() if isinstance(name, str) else ''
            numeric = normalized in _NUMERIC_KEYS or bool(re.fullmatch(
                r'[a-z][a-z0-9_]*(?:_count|_total|_bytes|_seconds)', normalized))
            if normalized not in _SAFE_KEYS and not numeric:
                continue
            if numeric and not isinstance(item, (int, float, bool)) and item is not None:
                continue
            if isinstance(item, (dict, list)) and normalized not in _NESTED_KEYS and normalized not in {'location','languages','available_languages'}:
                continue
            clean = _evidence(item, normalized, depth + 1)
            if clean is not None:
                result[normalized] = clean
        return result
    if isinstance(value, list):
        return [clean for item in value[:50] if (clean := _evidence(item, key, depth + 1)) is not None]
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value if not isinstance(value, float) or math.isfinite(value) else None
    if isinstance(value, str):
        cleaned = _redact_text(value)
        if cleaned != value:
            return '[СКРЫТО]'
        if key == 'folder' and value not in {'INBOX', 'Sent', 'SENT', 'inbox', 'sent'}:
            return '[ПАПКА]'
        if key in {'since', 'last_success', 'last_sync', 'created_at'}:
            return value if re.fullmatch(r'[0-9TZ:+. \-]{4,60}', value) else None
        if key == 'date_basis':
            return value if value == 'IMAP INTERNALDATE' else None
        return value if _CODE.fullmatch(value) else None
    return None


def safe_support_payload(checks: list[dict], metadata: dict) -> dict:
    clean_checks = []
    for index, check in enumerate(checks, 1):
        if not isinstance(check, dict):
            check = {'status': 'fail', 'title': 'Некорректная запись проверки'}
        status = check.get('status')
        if status not in STATUSES:
            status = 'fail'
        identifier = check.get('id', '')
        if not isinstance(identifier, str) or not re.fullmatch(r'[a-z][a-z0-9_.\-]{0,79}', identifier):
            identifier = f'check.{index}'
        clean_checks.append({'id': identifier, 'status': status,
            'title': _prose(check.get('title') or 'Проверка', 240),
            'details': _prose(check.get('details', '')),
            'resolution': _prose(check.get('resolution', ''), 1500),
            'evidence': _evidence(check.get('evidence', {})) or {}})
    counts = {status: sum(check['status'] == status for check in clean_checks) for status in STATUSES}
    overall = ('failed' if counts['fail'] else 'partial' if counts['warn'] or counts['skipped']
               else 'passed' if clean_checks else 'not_run')
    return {'schema': 'posteks.selfcheck.support.v1', 'overall_status': overall,
        'all_checks_passed': bool(clean_checks and overall == 'passed'), 'counts': counts,
        'metadata': _evidence(metadata) or {}, 'checks': clean_checks,
        'contains_private_details': False,
        'sharing_note': 'Для разбора отправьте только support_report.json. Не прикладывайте .env, рабочую БД и PRIVATE_details.json.'}


def _private_value(value, depth=0):
    if depth > 15:
        return '[ОГРАНИЧЕНО ПО ГЛУБИНЕ]'
    if isinstance(value, dict):
        return {str(key): '[СКРЫТО: секрет]' if _SECRET_KEY.search(str(key)) else _private_value(item, depth + 1)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_private_value(item, depth + 1) for item in value]
    if isinstance(value, str):
        return _redact_text(value, redact_email=False)
    if value is None or isinstance(value, (int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return '[НЕСЕРИАЛИЗУЕМОЕ ЗНАЧЕНИЕ]'


def _write_private(path: Path, text: str):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as stream:
        stream.write(text)


def _summary(payload):
    counts = payload['counts']
    lines = ['ПОСТЭКС — проверка установленной программы', OVERALL_NAMES[payload['overall_status']],
        f"Пройдено: {counts['pass']}; ошибок: {counts['fail']}; замечаний: {counts['warn']}; не проверено: {counts['skipped']}.",
        'Зелёные проверки не подтверждают пропущенные подключения или правильность всех технических ответов.', '']
    for check in payload['checks']:
        lines.append(f"[{STATUS_NAMES[check['status']]}] {check['title']} ({check['id']})")
        if check['details']: lines.append(check['details'])
        if check['resolution']: lines.append('Что делать: ' + check['resolution'])
        if check['evidence']: lines.append('Сведения: ' + json.dumps(check['evidence'], ensure_ascii=False, sort_keys=True))
        lines.append('')
    lines.append(payload['sharing_note'])
    lines.append(PRIVATE_WARNING)
    return '\n'.join(lines) + '\n'


def _html(payload, private_exists):
    def e(value): return escape(str(value), quote=True)
    counts = payload['counts']
    cards = []
    for check in payload['checks']:
        proof = ('<details><summary>Технические сведения</summary><pre>' +
            e(json.dumps(check['evidence'], ensure_ascii=False, indent=2)) + '</pre></details>') if check['evidence'] else ''
        resolution = '<p><strong>Что делать:</strong> ' + e(check['resolution']) + '</p>' if check['resolution'] else ''
        cards.append(f'<article class="{check["status"]}"><div class="label">{e(STATUS_NAMES[check["status"]])}</div>'
            f'<h2>{e(check["title"])}</h2><small>{e(check["id"])}</small><p>{e(check["details"])}</p>{resolution}{proof}</article>')
    private_note = '<p class="private">' + e(PRIVATE_WARNING) + '</p>' if private_exists else ''
    return '''<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'">
<title>ПОСТЭКС — результаты проверки</title><style>
body{font:16px/1.6 system-ui,sans-serif;color:#242a30;background:#f4f6f8;margin:0}main{max-width:980px;margin:36px auto;padding:0 24px}
h1{font-size:29px;line-height:1.3}h2{font-size:19px;margin:5px 0}article{background:white;border:1px solid #d8dde2;border-left:5px solid #64748b;border-radius:9px;padding:18px 22px;margin:14px 0}
.pass{border-left-color:#14774c}.fail{border-left-color:#b42318}.warn{border-left-color:#a15c00}.skipped{border-left-color:#64748b}
.label{font-size:14px;font-weight:650}.pass>.label{color:#14774c}.fail>.label{color:#b42318}.warn>.label{color:#995600}
small{color:#59636e}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px;background:#f4f6f8;padding:12px}
.counts,.sharing,.private{padding:16px;background:white;border:1px solid #d8dde2;border-radius:8px}.private{color:#8a3900;background:#fff4e8}
summary{cursor:pointer}a{color:#995000}p{white-space:pre-wrap}footer{margin:25px 0;color:#59636e}</style></head><body><main>''' + \
        f'<h1>ПОСТЭКС — проверка программы</h1><h2>{e(OVERALL_NAMES[payload["overall_status"]])}</h2>' + \
        f'<p class="counts">Пройдено: <strong>{counts["pass"]}</strong> · Ошибок: <strong>{counts["fail"]}</strong> · Замечаний: <strong>{counts["warn"]}</strong> · Не проверено: <strong>{counts["skipped"]}</strong></p>' + \
        '<p>Пропущенные подключения не считаются успешными. Автоматическая проверка не гарантирует правильность всех технических рекомендаций.</p>' + \
        ''.join(cards) + '<p class="sharing">' + e(payload['sharing_note']) + ' <a href="support_report.json">Открыть отчёт для передачи</a></p>' + \
        private_note + '<footer>Файлы сформированы локально. Программа отчёта ничего не отправляла.</footer></main></body></html>'


def write_reports(output_root: Path, checks: list[dict], metadata: dict, *, private_details: dict | None = None) -> dict:
    output_root = Path(output_root).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    created = datetime.now(timezone(timedelta(hours=5)))
    directory = output_root / ('selfcheck_' + created.strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:8])
    directory.mkdir(mode=0o700)
    payload = safe_support_payload(checks, metadata)
    payload['created_at'] = created.isoformat(timespec='seconds')
    support = directory / 'support_report.json'
    summary = directory / 'SUMMARY.txt'
    html = directory / 'REPORT.html'
    private = directory / 'PRIVATE_details.json' if private_details is not None else None
    _write_private(support, json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    _write_private(summary, _summary(payload))
    if private:
        details = {'private_report_do_not_share': True, 'warning': PRIVATE_WARNING,
                   'created_at': payload['created_at'], 'details': _private_value(private_details)}
        _write_private(private, json.dumps(details, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    _write_private(html, _html(payload, private is not None))
    latest = output_root / 'LATEST_REPORT.txt'
    temporary = output_root / ('.latest-' + uuid.uuid4().hex + '.tmp')
    _write_private(temporary, str(html) + '\n')
    os.replace(temporary, latest)
    return {'directory': str(directory), 'html': str(html), 'summary': str(summary),
        'support': str(support), 'private': str(private) if private else None, 'latest': str(latest),
        'counts': payload['counts'], 'overall_status': payload['overall_status']}
