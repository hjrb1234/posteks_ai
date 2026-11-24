"""Read-only, bounded MAX Bot API export to POSTEKS's private import format.

Uses only GET /messages. No Telegram token, account session or unofficial client.
The official API describes bot-admin access, not a guarantee of all old history.
Run from the project root: python -m scripts.export_max --help
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from email.utils import parsedate_to_datetime
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid


ROOT = Path(__file__).resolve().parent.parent
ENDPOINT = 'https://platform-api2.max.ru/messages'
PAGE_SIZE = 100
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_CHUNK_BYTES = 5 * 1024 * 1024
MAX_CHUNK_MESSAGES = 10000
MAX_TEXT_CHARS = 50000
SCHEMA = 'posteks.max_export.v1'


class MaxExportError(Exception):
    """Safe diagnostic: never includes response bodies, request headers or tokens."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # urllib otherwise carries Authorization to a redirected destination.
        return None


def local_configuration(path: Path | None = None) -> dict[str, str]:
    """Read only literal assignments needed by this stdlib maintenance command.

    No interpolation, commands, shell expansion or importing server.config (an
    unknown legacy DATABASE_URL must not prevent read-only MAX export).
    """
    allowed = {'MAX_BOT_TOKEN', 'STORAGE_DIR', 'SERVICE_STORAGE_ROOT'}
    values = {}
    try:
        lines = (path or ROOT / '.env').read_text(encoding='utf-8-sig').splitlines()
    except (FileNotFoundError, PermissionError, UnicodeError):
        lines = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        key = key.strip()
        if key not in allowed:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        else:
            value = re.split(r'\s+#', value, maxsplit=1)[0].rstrip()
        values[key] = value
    # Process values override local configuration, including deliberately empty
    # ones. BOT_TOKEN is never read as a fallback.
    for key in allowed:
        if key in os.environ:
            values[key] = os.environ[key]
    return values


def resolve_token(values: dict[str, str], *, prompt: Callable[[], str] | None = None) -> str:
    token = values.get('MAX_BOT_TOKEN', '').strip()
    if not token and prompt is not None:
        token = prompt().strip()
    if not token:
        raise MaxExportError('MAX_BOT_TOKEN не задан. Укажите токен MAX-бота в локальном .env или защищённом окружении; BOT_TOKEN Telegram не используется.')
    if len(token) > 4096 or any(ord(char) < 33 or ord(char) > 126 for char in token):
        raise MaxExportError('Формат MAX_BOT_TOKEN некорректен. Проверьте локальную настройку; значение не выводится.')
    return token


def default_output(values: dict[str, str]) -> Path:
    storage = Path(values.get('STORAGE_DIR') or values.get('SERVICE_STORAGE_ROOT') or 'storage').expanduser()
    if not storage.is_absolute():
        storage = ROOT / storage
    return storage.resolve() / 'history_exports'


def parse_date(value: str, *, end: bool = False) -> int:
    """ISO date covers a whole UTC day; ISO datetime is the exact UTC instant."""
    try:
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            instant = datetime.combine(date.fromisoformat(value), datetime_time(), timezone.utc)
            if end:
                instant += timedelta(days=1, milliseconds=-1)
        else:
            instant = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if instant.tzinfo is None:
                instant = instant.replace(tzinfo=timezone.utc)
        milliseconds = int(instant.timestamp() * 1000)
        if not 0 <= milliseconds <= 253402300798000:
            raise ValueError
        return milliseconds
    except (ValueError, OverflowError, OSError):
        raise MaxExportError('Дата должна быть YYYY-MM-DD или ISO 8601 с часовым поясом. Дата без пояса означает UTC.') from None


def _iso(milliseconds: int | None) -> str | None:
    return datetime.fromtimestamp(milliseconds / 1000, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z') if milliseconds is not None else None


class MaxClient:
    def __init__(self, token: str, *, transport=None, sleep=time.sleep, retries: int = 3):
        self._token = token
        self._transport = transport or build_opener(_NoRedirect()).open
        self._sleep = sleep
        self._retries = max(0, min(retries, 3))

    def messages(self, chat_id: int, lower: int, upper: int) -> list[dict]:
        """Inclusive local interval, using current official before/after fields.

        The published API schema deprecates from/to in favour of before/after.
        A one-millisecond overlap accommodates exclusive date boundaries; every
        returned timestamp is validated before it can advance the checkpoint.
        """
        query = {'chat_id': chat_id, 'after': max(0, lower - 1), 'before': upper + 1, 'count': PAGE_SIZE}
        request = Request(ENDPOINT + '?' + urlencode(query), headers={'Authorization': self._token,
                          'Accept': 'application/json', 'User-Agent': 'POSTEKS-local-history-export/1'}, method='GET')
        for attempt in range(self._retries + 1):
            try:
                with self._transport(request, timeout=25) as response:
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise MaxExportError('Ответ MAX превышает безопасный лимит 8 МиБ. Выгрузка остановлена; уменьшите период.')
                try:
                    value = json.loads(raw)
                except (ValueError, UnicodeError, RecursionError):
                    raise MaxExportError('MAX вернул некорректный JSON; выгрузка остановлена.') from None
                messages = value.get('messages') if isinstance(value, dict) else None
                if not isinstance(messages, list) or len(messages) > PAGE_SIZE or any(not isinstance(message, dict) for message in messages):
                    raise MaxExportError('Структура ответа MAX не соответствует документированному массиву messages.')
                return messages
            except HTTPError as error:
                if error.code == 429 and attempt < self._retries:
                    retry_after = error.headers.get('Retry-After', '') if error.headers else ''
                    try:
                        delay = float(retry_after)
                    except (TypeError, ValueError):
                        try:
                            delay = (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds()
                        except (TypeError, ValueError, OverflowError):
                            delay = 0.5 * (2 ** attempt)
                    self._sleep(min(10, max(0.1, delay)))
                    continue
                if error.code in {401, 403}:
                    raise MaxExportError('MAX отклонил доступ. Проверьте MAX_BOT_TOKEN и права администратора бота в выбранном чате; содержимое ответа не выводится.') from None
                if error.code == 429:
                    raise MaxExportError('MAX ограничил частоту запросов. Лимит повторов достигнут; повторите выгрузку позже.') from None
                raise MaxExportError('MAX вернул HTTP ' + str(error.code) + '. Выгрузка остановлена без отправки или изменения сообщений.') from None
            except (URLError, TimeoutError, OSError):
                raise MaxExportError('Соединение с MAX недоступно. Проверьте сеть и доверенные TLS-сертификаты; проверка TLS не отключается.') from None
        raise MaxExportError('Лимит запросов MAX достигнут.')


def _json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def normalize_message(message: dict, chat_id: int) -> tuple[str, dict, list[str]]:
    timestamp = message.get('timestamp')
    if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0 or timestamp > 253402300798000:
        raise MaxExportError('У сообщения MAX отсутствует допустимый timestamp. Контрольная точка не продвинута.')
    recipient = message.get('recipient')
    if isinstance(recipient, dict) and recipient.get('chat_id') is not None and str(recipient['chat_id']) != str(chat_id):
        raise MaxExportError('MAX вернул сообщение другого чата. Выгрузка остановлена, чтобы не смешивать данные.')
    body = message.get('body') or {}
    if not isinstance(body, dict):
        raise MaxExportError('Содержимое сообщения MAX имеет неизвестный формат.')
    raw_text = body.get('text')
    if raw_text is not None and not isinstance(raw_text, str):
        raise MaxExportError('Текст сообщения MAX имеет неизвестный формат.')
    if len(raw_text or '') > MAX_TEXT_CHARS:
        raise MaxExportError('Сообщение превышает лимит импорта 50 000 символов; исходный текст не обрезается молча.')
    sender = message.get('sender') or {}
    if not isinstance(sender, dict):
        raise MaxExportError('Отправитель сообщения MAX имеет неизвестный формат.')
    display_name = ' '.join(str(sender.get(key) or '').strip() for key in ('first_name', 'last_name')).strip()
    display_name = display_name or str(sender.get('name') or sender.get('username') or sender.get('user_id') or '')
    problems = []
    mid = body.get('mid') or message.get('mid')
    if not isinstance(mid, str) or not mid:
        mid = 'sha256:' + hashlib.sha256(_json_bytes(message)).hexdigest()
        problems.append('У части сообщений нет mid: использован отпечаток исходной записи; уникальность отдельных одинаковых сообщений не подтверждена.')
    link = message.get('link')
    linked_message = link.get('message') if isinstance(link, dict) else None
    linked_id = None
    if isinstance(linked_message, dict):
        linked_id = (linked_message.get('body') or {}).get('mid') if isinstance(linked_message.get('body'), dict) else linked_message.get('mid')
    # Keep non-text records represented in the transcript, while the exact
    # original metadata remains available in the private source JSON.
    text = raw_text or '[Сообщение MAX без текста: вложение или пересылка; см. исходные данные записи.]'
    normalized = {'conversation_id': str(chat_id), 'date': _iso(timestamp), 'sender': display_name[:500],
                  'text': text, 'message_id': mid, 'timestamp_ms': timestamp,
                  'reply_to_message_id': linked_id if isinstance(link, dict) and link.get('type') == 'reply' else None,
                  'forwarded_message_id': linked_id if isinstance(link, dict) and link.get('type') == 'forward' else None,
                  'source': 'MAX Bot API (непроверено)', 'max_source': message}
    return mid, normalized, problems


def collect_history(client: MaxClient, chat_id: int, lower: int, upper: int, *, max_requests: int = 200,
                    max_messages: int = 20000, max_total_bytes: int = MAX_TOTAL_BYTES) -> tuple[list[dict], dict]:
    records, fingerprints = {}, {}
    warnings = []
    requests, duplicates, total_bytes = 0, 0, 0
    exhausted, error = False, None
    cursor = upper

    def warning(value):
        if value not in warnings:
            warnings.append(value)

    def fetch(start, stop):
        nonlocal requests
        if requests >= max_requests:
            raise MaxExportError('Достигнут лимит запросов для одной выгрузки. Уже полученные данные сохранены; уменьшите период или явно увеличьте --max-requests.')
        requests += 1
        return client.messages(chat_id, start, stop)

    def save(page, start, stop):
        nonlocal duplicates, total_bytes
        timestamps = []
        for raw in page:
            mid, row, problems = normalize_message(raw, chat_id)
            timestamp = row['timestamp_ms']
            if not start <= timestamp <= stop:
                raise MaxExportError('MAX вернул сообщение вне запрошенного временного окна. Выгрузка остановлена: границы API нужно проверить на вашем чате.')
            timestamps.append(timestamp)
            for problem in problems:
                warning(problem)
            serialized = _json_bytes(row)
            fingerprint = hashlib.sha256(serialized).hexdigest()
            if mid in records:
                duplicates += 1
                if fingerprints[mid] != fingerprint:
                    warning('Сообщение с тем же mid изменилось во время выгрузки. Сохранена первая полученная версия; это не согласованный снимок чата.')
                continue
            if len(serialized) + 200 > MAX_CHUNK_BYTES:
                raise MaxExportError('Одна исходная запись больше допустимой импортной части 5 МиБ; запись не обрезается молча.')
            if len(records) >= max_messages or total_bytes + len(serialized) > max_total_bytes:
                raise MaxExportError('Достигнут лимит объёма одной выгрузки. Уже полученные записи сохранены; выгружайте историю меньшими периодами.')
            records[mid], fingerprints[mid] = row, fingerprint
            total_bytes += len(serialized)
        return timestamps

    try:
        while cursor >= lower:
            page = fetch(lower, cursor)
            timestamps = save(page, lower, cursor)
            if len(page) < PAGE_SIZE:
                exhausted = True
                break
            oldest = min(timestamps)
            # Never subtract a timestamp before checking ties. There may be
            # additional messages sharing the oldest millisecond in a page.
            boundary = fetch(oldest, oldest)
            save(boundary, oldest, oldest)
            if len(boundary) >= PAGE_SIZE:
                warning('На одной миллисекунде получено 100 сообщений: граница пагинации неоднозначна. Автоматическое продвижение остановлено, чтобы не пропустить сообщения.')
                break
            if oldest == lower:
                exhausted = True
                break
            cursor = oldest - 1
    except MaxExportError as exc:
        error = str(exc)
        warning(error)
    rows = sorted(records.values(), key=lambda row: (row['timestamp_ms'], row['message_id']))
    return rows, {'api_window_exhausted': exhausted, 'requests': requests, 'duplicates': duplicates,
                  'warnings': warnings, 'error': error, 'last_cursor_ms': cursor}


def write_bundle(rows: list[dict], state: dict, output_root: Path, chat_id: int, lower: int, upper: int) -> Path:
    output_root = output_root.expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    destination = output_root / ('max-history-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8])
    destination.mkdir(mode=0o700, exist_ok=False)
    chunks = []
    prefix = b'{"schema":"' + SCHEMA.encode() + b'","source":"MAX Bot API (unreviewed)","messages":['
    suffix = b']}'
    block, size = [], len(prefix) + len(suffix)

    def flush():
        nonlocal block, size
        if not block:
            return
        content = prefix + b','.join(block) + suffix
        path = destination / f'messages-{len(chunks) + 1:04d}.json'
        with path.open('xb') as stream:
            stream.write(content)
        path.chmod(0o600)
        chunks.append({'filename': path.name, 'messages': len(block), 'size_bytes': len(content),
                       'sha256': hashlib.sha256(content).hexdigest()})
        block, size = [], len(prefix) + len(suffix)

    for row in rows:
        raw = _json_bytes(row)
        addition = len(raw) + bool(block)
        if block and (len(block) >= MAX_CHUNK_MESSAGES or size + addition > MAX_CHUNK_BYTES):
            flush()
            addition = len(raw)
        if size + addition > MAX_CHUNK_BYTES:
            raise MaxExportError('Одна запись превышает размер импортной части. Незавершённая папка сохранена; не импортируйте её как полный архив.')
        block.append(raw)
        size += addition
    flush()
    manifest = {'schema': SCHEMA, 'private': True, 'chat_id': str(chat_id), 'created_at': datetime.now(timezone.utc).isoformat(),
        'source_endpoint': ENDPOINT, 'read_only': True, 'requires_bot_admin': True,
        'requested_from_utc': _iso(lower), 'requested_to_utc': _iso(upper),
        'earliest_returned_utc': rows[0]['date'] if rows else None,
        'latest_returned_utc': rows[-1]['date'] if rows else None,
        'message_count': len(rows), 'chunks': chunks, 'completeness': 'unknown',
        'pre_join_history_verified': False, 'status': 'visible_window_exhausted' if state['api_window_exhausted'] else 'partial',
        **state}
    manifest['warnings'] = [
        'Полнота истории за все годы и доступ к сообщениям до добавления бота не подтверждены. Проверьте старейший необходимый период в официальном клиенте.',
        'Переписка является внутренним непроверенным источником, а не автоматически подтверждённым техническим решением.',
        'Это последовательное чтение API, а не согласованный снимок: удаление и редактирование во время выгрузки могут менять результат.',
        *manifest['warnings']]
    path = destination / 'manifest.json'
    with path.open('x', encoding='utf-8') as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
    path.chmod(0o600)
    return destination


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Приватная read-only выгрузка доступной MAX-боту истории; полнота за все годы не гарантируется.')
    parser.add_argument('--chat-id', type=int, required=True, help='ID чата, в котором MAX-бот является администратором')
    parser.add_argument('--from-date', required=True, help='Начало: YYYY-MM-DD или ISO datetime; без пояса используется UTC')
    parser.add_argument('--to-date', required=True, help='Конец: YYYY-MM-DD (весь UTC-день) или ISO datetime')
    parser.add_argument('--output', type=Path, help='Приватный каталог; по умолчанию <STORAGE_DIR>/history_exports, для запуска создаётся новая подпапка')
    parser.add_argument('--max-requests', type=int, default=200, help='Лимит read-only запросов, включая проверки временных границ (1–2000)')
    parser.add_argument('--max-messages', type=int, default=20000, help='Лимит сообщений на один запуск (1–100000); общий объём ограничен 64 МиБ')
    args = parser.parse_args(argv)
    try:
        lower, upper = parse_date(args.from_date), parse_date(args.to_date, end=True)
        if lower > upper:
            raise MaxExportError('--from-date должен быть не позже --to-date.')
        if not 1 <= args.max_requests <= 2000 or not 1 <= args.max_messages <= 100000:
            raise MaxExportError('Проверьте --max-requests (1–2000) и --max-messages (1–100000).')
        values = local_configuration()
        prompt = (lambda: getpass.getpass('Токен MAX-бота (ввод скрыт, не Telegram): ')) if sys.stdin.isatty() else None
        token = resolve_token(values, prompt=prompt)
        client = MaxClient(token)
        rows, state = collect_history(client, args.chat_id, lower, upper, max_requests=args.max_requests, max_messages=args.max_messages)
        destination = write_bundle(rows, state, args.output or default_output(values), args.chat_id, lower, upper)
        print('Приватная выгрузка сохранена: ' + str(destination))
        print('Сообщений: ' + str(len(rows)) + '; запросов: ' + str(state['requests']) + '. Прочитайте manifest.json; полнота истории не подтверждена.')
        print('В POSTEKS загружайте только messages-*.json через предпросмотр и подтверждение импорта. Не публикуйте выгрузку, manifest или токен.')
        return 0 if state['api_window_exhausted'] else 2
    except MaxExportError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (OSError, ValueError):
        print('Не удалось сохранить приватную выгрузку. Проверьте путь, права и свободное место; значения токенов не выводятся.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
