"""Official API protocol, bounded history export and private importer compatibility.

All requests use an in-memory transport; no credentials or real MAX calls.
"""
import io
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

import pytest

from scripts import export_max as exporter
from server.history_import import parse_history


TOKEN = 'max-fixture-not-a-live-token'


def message(mid, timestamp, text='Непроверенный пример сервисного обсуждения', chat_id=-123):
    return {'sender': {'user_id': 7, 'first_name': 'Тестовый', 'last_name': 'Сотрудник'},
            'recipient': {'chat_id': chat_id, 'chat_type': 'chat'}, 'timestamp': timestamp,
            'body': {'mid': mid, 'text': text, 'attachments': []}}


class MockApi:
    def __init__(self, messages):
        self.records = messages
        self.requests = []

    def __call__(self, request, *, timeout):
        self.requests.append(request)
        assert timeout == 25 and request.method == 'GET'
        url = urlparse(request.full_url)
        assert url.scheme == 'https' and url.netloc == 'platform-api2.max.ru' and url.path == '/messages'
        query = parse_qs(url.query)
        assert set(query) == {'chat_id', 'after', 'before', 'count'}
        assert TOKEN not in request.full_url and request.get_header('Authorization') == TOKEN
        assert query['count'] == ['100']
        after, before = int(query['after'][0]), int(query['before'][0])
        selected = [row for row in self.records if after < row['timestamp'] < before]
        selected.sort(key=lambda row: (row['timestamp'], row['body']['mid']), reverse=True)
        return io.BytesIO(json.dumps({'messages': selected[:100]}, ensure_ascii=False).encode())


def test_official_readonly_headers_time_pagination_dedup_and_reply_preserved(tmp_path):
    source = [message(f'mid.{index:03}', 1000 + index * 10) for index in range(230)]
    source[-1]['link'] = {'type': 'reply', 'message': {'body': {'mid': 'mid.001', 'text': 'Original'}}}
    api = MockApi(source)
    client = exporter.MaxClient(TOKEN, transport=api)
    rows, state = exporter.collect_history(client, -123, 1000, 4000)
    assert len(rows) == 230 and state['api_window_exhausted'] and not state['error']
    assert state['requests'] == 5 and state['duplicates'] == 2
    assert rows[0]['timestamp_ms'] == 1000 and rows[-1]['timestamp_ms'] == 3290
    assert rows[-1]['reply_to_message_id'] == 'mid.001'
    assert rows[-1]['max_source']['sender']['user_id'] == 7
    destination = exporter.write_bundle(rows, state, tmp_path, -123, 1000, 4000)
    manifest = json.loads((destination / 'manifest.json').read_text())
    assert manifest['completeness'] == 'unknown' and manifest['pre_join_history_verified'] is False
    assert manifest['read_only'] and manifest['requires_bot_admin']
    imported = parse_history((destination / manifest['chunks'][0]['filename']).read_bytes(), 'messages-0001.json')
    assert imported['message_count'] == 230 and imported['conversation_count'] == 1
    assert imported['messages'][0]['sender'] == 'Тестовый Сотрудник'
    assert TOKEN not in ''.join(path.read_text() for path in destination.iterdir())


def test_equal_timestamp_boundary_is_not_silently_skipped():
    source = [message(f'tie.{index}', 2000) for index in range(140)] + [message('older', 1000)]
    api = MockApi(source)
    rows, state = exporter.collect_history(exporter.MaxClient(TOKEN, transport=api), -123, 500, 3000)
    assert len(rows) == 100 and not state['api_window_exhausted']
    assert any('граница пагинации неоднозначна' in warning for warning in state['warnings'])
    assert state['requests'] == 2
    # Do not claim the older unseen record was exported, or subtract its window
    # before proving that the 140-message tie has been completely traversed.
    assert not any(row['message_id'] == 'older' for row in rows)


def test_more_than_one_page_with_ties_below_boundary_limit_has_no_gap():
    source = [message(f'new.{index}', 4000 + index) for index in range(80)]
    source += [message(f'tie.{index}', 3000) for index in range(40)]
    source += [message(f'old.{index}', 1000 + index) for index in range(30)]
    rows, state = exporter.collect_history(exporter.MaxClient(TOKEN, transport=MockApi(source)), -123, 1000, 5000)
    assert len(rows) == 150 and len({row['message_id'] for row in rows}) == 150
    assert state['api_window_exhausted'] and state['duplicates'] == 20


def test_auth_and_429_retry_are_bounded_and_never_leak_token():
    sleeps = []
    attempts = []
    def transport(request, *, timeout):
        attempts.append(request)
        if len(attempts) < 3:
            raise HTTPError(request.full_url, 429, 'secret response body must not be logged', {'Retry-After': '9999'}, None)
        return io.BytesIO(b'{"messages":[]}')
    assert exporter.MaxClient(TOKEN, transport=transport, sleep=sleeps.append).messages(-123, 1000, 2000) == []
    assert sleeps == [10, 10] and len(attempts) == 3
    def denied(request, *, timeout):
        raise HTTPError(request.full_url, 403, TOKEN, {}, io.BytesIO(TOKEN.encode()))
    with pytest.raises(exporter.MaxExportError) as caught:
        exporter.MaxClient(TOKEN, transport=denied).messages(-123, 1000, 2000)
    assert 'администратора' in str(caught.value) and TOKEN not in str(caught.value)
    attempts.clear()
    def limited(request, *, timeout):
        attempts.append(request)
        raise HTTPError(request.full_url, 429, TOKEN, {}, None)
    with pytest.raises(exporter.MaxExportError, match='Лимит повторов'):
        exporter.MaxClient(TOKEN, transport=limited, sleep=lambda seconds: None).messages(-123, 1000, 2000)
    assert len(attempts) == 4


def test_redirects_cannot_forward_authorization_to_other_host():
    assert exporter._NoRedirect().redirect_request(None, None, 302, 'redirect', {}, 'https://other.invalid') is None


def test_partial_error_preserves_received_data_and_safe_manifest(tmp_path):
    source = [message(f'mid.{index}', 1000 + index) for index in range(150)]
    api = MockApi(source)
    original = api.__call__
    calls = 0
    def transport(request, *, timeout):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise HTTPError(request.full_url, 401, TOKEN, {}, None)
        return original(request, timeout=timeout)
    rows, state = exporter.collect_history(exporter.MaxClient(TOKEN, transport=transport), -123, 1000, 2000)
    assert len(rows) == 100 and state['error'] and not state['api_window_exhausted']
    destination = exporter.write_bundle(rows, state, tmp_path, -123, 1000, 2000)
    manifest = json.loads((destination / 'manifest.json').read_text())
    assert manifest['status'] == 'partial' and manifest['message_count'] == 100
    assert TOKEN not in json.dumps(manifest)
    assert parse_history((destination / 'messages-0001.json').read_bytes(), 'messages.json')['message_count'] == 100


def test_time_bounds_chat_boundaries_and_response_size_are_validated():
    class ForeignChat:
        def messages(self, *args):
            return [message('foreign', 1500, chat_id=-999)]
    rows, state = exporter.collect_history(ForeignChat(), -123, 1000, 2000)
    assert rows == [] and 'другого чата' in state['error']
    class IgnoredDates:
        def messages(self, *args):
            return [message('future', 9000)]
    assert 'вне запрошенного' in exporter.collect_history(IgnoredDates(), -123, 1000, 2000)[1]['error']
    transport = lambda *args, **kwargs: io.BytesIO(b'x' * (exporter.MAX_RESPONSE_BYTES + 1))
    with pytest.raises(exporter.MaxExportError, match='8 МиБ'):
        exporter.MaxClient(TOKEN, transport=transport).messages(-123, 1000, 2000)


def test_chunk_size_count_and_import_contract_for_large_transcript(tmp_path):
    rows = [exporter.normalize_message(message(f'mid.{index}', 1000 + index, 'а' * 3000), -123)[1] for index in range(10003)]
    state = {'api_window_exhausted': True, 'requests': 1, 'duplicates': 0, 'warnings': [], 'error': None, 'last_cursor_ms': 0}
    destination = exporter.write_bundle(rows, state, tmp_path, -123, 1000, 20000)
    manifest = json.loads((destination / 'manifest.json').read_text())
    assert len(manifest['chunks']) > 1
    parsed_count = 0
    for chunk in manifest['chunks']:
        path = destination / chunk['filename']
        assert path.stat().st_size <= 5 * 1024 * 1024 and chunk['messages'] <= 10000
        parsed_count += parse_history(path.read_bytes(), path.name)['message_count']
    assert parsed_count == len(rows)
    # Every run has a fresh directory and does not overwrite an earlier export.
    second = exporter.write_bundle([], state, tmp_path, -123, 1000, 20000)
    assert second != destination and (destination / 'messages-0001.json').exists()


def test_limits_yield_explicit_partial_export_and_no_silent_truncation():
    source = [message(f'mid.{index}', 1000 + index) for index in range(150)]
    client = exporter.MaxClient(TOKEN, transport=MockApi(source))
    rows, state = exporter.collect_history(client, -123, 1000, 2000, max_requests=1)
    assert len(rows) == 100 and 'лимит запросов' in state['error'].lower() and not state['api_window_exhausted']
    rows, state = exporter.collect_history(client, -123, 1000, 2000, max_messages=7)
    assert len(rows) == 7 and 'лимит объёма' in state['error'].lower()
    with pytest.raises(exporter.MaxExportError, match='не обрезается молча'):
        exporter.normalize_message(message('too-long', 1000, 'x' * 50001), -123)


def test_token_configuration_is_literal_uses_only_max_and_does_not_execute(tmp_path, monkeypatch):
    for key in ('MAX_BOT_TOKEN', 'STORAGE_DIR', 'SERVICE_STORAGE_ROOT'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('BOT_TOKEN', 'telegram-must-not-be-used')
    env = tmp_path / '.env'
    env.write_text("BOT_TOKEN=telegram-fixture\nMAX_BOT_TOKEN='$(not-executed)'\nSERVICE_STORAGE_ROOT=./private\n", encoding='utf-8')
    config = exporter.local_configuration(env)
    assert config == {'MAX_BOT_TOKEN': '$(not-executed)', 'SERVICE_STORAGE_ROOT': './private'}
    assert exporter.resolve_token(config) == '$(not-executed)'
    assert exporter.default_output(config) == exporter.ROOT / 'private/history_exports'
    assert exporter.resolve_token({}, prompt=lambda: TOKEN) == TOKEN
    with pytest.raises(exporter.MaxExportError, match='MAX_BOT_TOKEN не задан'):
        exporter.resolve_token({})
    monkeypatch.setenv('MAX_BOT_TOKEN', TOKEN)
    assert exporter.resolve_token(exporter.local_configuration(env)) == TOKEN


def test_iso_date_bounds_are_utc_and_cli_validation_prevents_network(monkeypatch, capsys):
    lower = exporter.parse_date('2026-10-22')
    upper = exporter.parse_date('2026-10-22', end=True)
    assert upper - lower == 24 * 60 * 60 * 1000 - 1
    assert exporter.parse_date('2026-10-22T05:00:00+05:00') == lower
    monkeypatch.setattr(exporter, 'MaxClient', lambda *args, **kwargs: pytest.fail('No network client for invalid arguments'))
    assert exporter.main(['--chat-id', '-123', '--from-date', '2026-10-23', '--to-date', '2026-10-22']) == 1
    assert '--from-date' in capsys.readouterr().err
    assert exporter.main(['--chat-id', '-123', '--from-date', 'invalid', '--to-date', '2026-10-22']) == 1


def test_missing_body_and_mid_are_preserved_as_explicit_unconfirmed_records():
    raw = {'timestamp': 1000, 'recipient': {'chat_id': -123}, 'body': None,
           'link': {'type': 'forward', 'message': {'body': {'mid': 'forwarded', 'text': 'Forward text'}}}}
    mid, normalized, warnings = exporter.normalize_message(raw, -123)
    assert mid.startswith('sha256:') and warnings
    assert normalized['max_source'] == raw and normalized['reply_to_message_id'] is None
    assert normalized['forwarded_message_id'] == 'forwarded'
    assert normalized['text'].startswith('[Сообщение MAX без текста')
