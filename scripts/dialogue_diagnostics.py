"""Compare direct model dialogue with the actual application request path.

Reports are PRIVATE: generated answers can quote internal material. The default
run makes no provider calls. --live is an explicit, bounded integration check;
it never changes the application's production prompts or configuration.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import secrets
import tempfile
from unittest.mock import patch
from urllib.parse import urlparse


BASIC_TURNS = (
    'Здравствуйте',
    'У меня проблема с оборудованием',
    'Какие сведения тебе нужны?',
    'Объясни предыдущий ответ проще',
    'Почему ты это спрашиваешь?',
)
TECHNICAL_TURNS = (
    'Формирователь РЕ38-12. Конвейер не подаёт короб. При отключённом питании проверили разъём датчика: он закреплён.',
    'Модель и проверку я уже сообщил. Что проверять дальше?',
    'Объясни последний шаг проще.',
)
DIRECT_INSTRUCTIONS = 'Ведите свободный, связный диалог на русском языке. Учитывайте предыдущие реплики и отвечайте на последнее сообщение. Если данных недостаточно, задайте уместный вопрос. Не выдумывайте неизвестные факты.'
SECRET_FIELDS = {'api_key', 'authorization', 'password', 'session_secret', 'bot_token', 'access_token', 'refresh_token'}


def fingerprint(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def redact_transport_secrets(value):
    if isinstance(value, dict):
        return {key: '[REDACTED]' if str(key).casefold() in SECRET_FIELDS else redact_transport_secrets(item)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_transport_secrets(item) for item in value]
    return value


def source_references(value):
    """Record evidence identity without copying private source text."""
    result = []
    if isinstance(value, dict):
        if any(value.get(key) is not None for key in ('document_id', 'solution_id', 'equipment_id')):
            result.append({key: value[key] for key in ('document_id', 'solution_id', 'equipment_id', 'page', 'version') if key in value})
        for item in value.values():
            result.extend(source_references(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            result.extend(source_references(item))
    return list({fingerprint(item): item for item in result}.values())


def summarize_request(request, *, capture_content=False):
    clean = redact_transport_secrets(request)
    payload = clean.get('input')
    if isinstance(payload, str):
        try:
            parsed = json.loads(payload)
        except (ValueError, TypeError):
            parsed = payload
    else:
        parsed = payload
    history = parsed.get('recent_history', parsed.get('history', [])) if isinstance(parsed, dict) else parsed if isinstance(parsed, list) else []
    result = {'model': clean.get('model'), 'request_sha256': fingerprint(clean),
              'instructions_sha256': fingerprint(clean.get('instructions')),
              'input_sha256': fingerprint(payload),
              'input_characters': len(json.dumps(payload, ensure_ascii=False, default=str)),
              'history_items': len(history) if isinstance(history, list) else None,
              'source_references': source_references(parsed),
              'response_format': clean.get('text', {}).get('format', {}).get('type') if isinstance(clean.get('text'), dict) else None,
              'store': clean.get('store')}
    if isinstance(parsed, dict):
        result['current_message'] = parsed.get('message')
        result['history_roles'] = [item.get('role') for item in history if isinstance(item, dict)] if isinstance(history, list) else []
    elif isinstance(parsed, list):
        roles = [item.get('role') for item in parsed if isinstance(item, dict)]
        result['input_roles'] = roles
        result['history_roles'] = roles[:-1] if roles and roles[-1] == 'user' else roles
        result['history_items'] = len(result['history_roles'])
        users = [item.get('content') for item in parsed if isinstance(item, dict) and item.get('role') == 'user']
        result['current_message'] = users[-1] if users else None
    else:
        result['current_message'] = parsed
    if capture_content:
        result['private_request_payload'] = clean
    return result


class ProviderRecorder:
    def __init__(self, *, live=False, limit=24, capture_content=False):
        self.live, self.limit, self.capture_content = live, limit, capture_content
        self.path, self.turn = 'application', None
        self.requests, self.contexts = [], []

    def wrap_constructor(self, constructor):
        recorder = self

        class RecordedOpenAI:
            def __init__(self, *args, **kwargs):
                # Each counted diagnostic generation must represent one
                # network attempt; the SDK must not add hidden paid retries.
                # Production constructors outside this wrapper are unchanged.
                kwargs['max_retries'] = 0
                self._inner = constructor(*args, **kwargs)
                self.responses = self

            def __getattr__(self, key):
                return getattr(self._inner, key)

            def create(self, **kwargs):
                record = {'path': recorder.path, 'turn': recorder.turn,
                          **summarize_request(kwargs, capture_content=recorder.capture_content)}
                recorder.requests.append(record)
                if not recorder.live:
                    record['status'] = 'blocked_without_live_flag'
                    raise RuntimeError('Live provider calls require --live')
                started = sum(item.get('provider_call_started') is True for item in recorder.requests)
                if started >= recorder.limit:
                    record['status'] = 'blocked_call_limit'
                    raise RuntimeError('Diagnostic provider call limit reached')
                record['provider_call_started'] = True
                try:
                    response = self._inner.responses.create(**kwargs)
                    usage = getattr(response, 'usage', None)
                    record.update(status='returned', provider_response_id=getattr(response, 'id', None),
                                  provider_status=getattr(response, 'status', None),
                                  input_tokens=getattr(usage, 'input_tokens', None),
                                  output_tokens=getattr(usage, 'output_tokens', None))
                    return response
                except Exception as error:
                    from server.ai import safe_api_error
                    record.update(status='failed', error_type=type(error).__name__, error=safe_api_error(error))
                    raise

        return RecordedOpenAI

    def observe_context(self, function):
        recorder = self

        def observed(*args, **kwargs):
            context = kwargs.get('context')
            if context is None and len(args) > 1 and isinstance(args[1], dict):
                context = args[1]
            if isinstance(context, dict) and 'message' in context:
                row = {'path': recorder.path, 'turn': recorder.turn,
                       'function': function.__name__, 'current_message': context.get('message'),
                       'history_items': len(context.get('recent_history') or []),
                       'history_roles': [item.get('role') for item in context.get('recent_history') or []],
                       'source_references': source_references(context.get('sources')),
                       'context_sha256': fingerprint(context)}
                if recorder.capture_content:
                    row['private_context'] = redact_transport_secrets(deepcopy(context))
                recorder.contexts.append(row)
            return function(*args, **kwargs)
        return observed


@contextmanager
def isolated_application(*, live, recorder, configured_models=None):
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from server import ai, knowledge, main, mail_sync
    from server.config import settings
    from server.db import Base, get_session
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(engine)
    if configured_models:
        from server.models import AppSetting
        with factory() as session:
            session.add(AppSetting(key='models', value=dict(configured_models)))
            session.commit()

    def dependency():
        with factory() as session:
            yield session

    previous_overrides = dict(main.app.dependency_overrides)
    try:
        with tempfile.TemporaryDirectory(prefix='posteks-dialogue-check-') as directory, ExitStack() as stack:
            (Path(directory) / 'documents').mkdir()
            for obj, name, value in ((settings, 'storage_dir', Path(directory)), (settings, 'app_mode', 'TEST'),
                    (settings, 'cookie_secure', False), (settings, 'reference_test_mode', False), (main, 'init_db', lambda: None),
                    (main, 'SessionLocal', factory), (knowledge, 'SessionLocal', factory), (mail_sync, 'SessionLocal', factory)):
                stack.enter_context(patch.object(obj, name, value))
            if not live:
                stack.enter_context(patch.object(settings, 'openai_api_key', ''))
            # Index only files explicitly passed by the user, synchronously;
            # no background work may outlive this private temporary database.
            stack.enter_context(patch.object(main._indexer, 'submit', lambda fn, *args: fn(*args)))
            for name in ('generate_plan', 'generate_response', 'generate_reply', 'generate_dialogue', 'generate_answer'):
                if callable(getattr(ai, name, None)):
                    stack.enter_context(patch.object(ai, name, recorder.observe_context(getattr(ai, name))))
            main.app.dependency_overrides[get_session] = dependency
            main._rate_buckets.clear()
            with TestClient(main.app, raise_server_exceptions=False) as client:
                yield client, factory
    finally:
        main.app.dependency_overrides.clear()
        main.app.dependency_overrides.update(previous_overrides)
        engine.dispose()


def install_material(client, factory, path):
    from server.auth import hash_password
    from server.models import User
    if path.stat().st_size > 25 * 1024 * 1024:
        raise ValueError('Материал больше 25 МиБ; выберите небольшой документ для проверки.')
    password = secrets.token_urlsafe(24)
    with factory() as session:
        session.add(User(username='diagnostic-owner', password_hash=hash_password(password), role='admin'))
        session.commit()
    result = client.post('/api/auth/login', json={'username': 'diagnostic-owner', 'password': password})
    if result.status_code != 200:
        raise RuntimeError('Не удалось открыть временную учётную запись диагностики.')
    response = client.post('/api/documents', files={'files': (path.name, path.read_bytes())}, data={'visibility': 'internal'})
    if response.status_code != 200:
        raise RuntimeError('Материал не принят: HTTP ' + str(response.status_code))
    document = response.json()[0]
    approved = client.patch(f'/api/documents/{document["id"]}', json={'answer_access': 'client_answers'})
    if approved.status_code != 200:
        raise RuntimeError('Не удалось разрешить ответы по временному материалу.')
    original_status = client.get(f'/api/documents/{document["id"]}/file').status_code
    client.post('/api/auth/logout')
    client.get('/api/auth/me')
    private_status = client.get(f'/api/documents/{document["id"]}/file').status_code
    return {'filename': path.name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'document_id': document['id'], 'indexing_status': approved.json().get('status'),
            'pages': approved.json().get('pages'), 'chunks': approved.json().get('chunks'),
            'staff_original_http_status': original_status, 'guest_original_http_status': private_status,
            'only_in_temporary_database': True}


def run_application(client, scenarios, recorder):
    results = []
    client.get('/api/auth/me')  # Establish cookie before the first message.
    for name, turns in scenarios:
        conversation = None
        rows = []
        recorder.path = 'application'
        for index, message in enumerate(turns, 1):
            recorder.turn = f'{name}:{index}'
            payload = {'message': message}
            if conversation:
                payload['conversation_id'] = conversation
            response = client.post('/api/chat', json=payload)
            try:
                body = response.json()
            except ValueError:
                body = {}
            conversation = body.get('conversation_id', conversation)
            rows.append({'input': message, 'http_status': response.status_code,
                'response': body.get('message', {}).get('content'), 'error': body.get('ai_error') or body.get('detail'),
                'engine': body.get('engine'), 'planning_mode': body.get('planning_mode'),
                'conversation_id': conversation, 'request_id': body.get('request_id') or response.headers.get('X-Request-ID'),
                'provider_response_id': body.get('provider_response_id'), 'model': body.get('model'),
                'state': body.get('state'), 'source_references': source_references(body.get('message', {}).get('sources')),
                'trace': body.get('trace')})
        results.append({'name': name, 'turns': rows})
    return results


def run_direct(scenarios, recorder, *, model, configured):
    if not recorder.live or not configured:
        return {'status': 'not_run', 'reason': 'Для настоящего сравнения нужны --live и настроенный серверный ключ OpenAI.',
                'scenarios': [], 'responses_invented': False}
    from openai import OpenAI
    from server.ai import _check_response_complete, provider_failure
    from server.config import settings
    results = []
    client = OpenAI(api_key=settings.openai_api_key, timeout=45, max_retries=0)
    try:
        for name, turns in scenarios:
            messages, rows = [], []
            recorder.path = 'direct'
            for index, message in enumerate(turns, 1):
                recorder.turn = f'{name}:{index}'
                messages.append({'role': 'user', 'content': message})
                response = None
                try:
                    response = client.responses.create(model=model, instructions=DIRECT_INSTRUCTIONS,
                        input=deepcopy(messages), max_output_tokens=settings.max_ai_output_tokens, store=False)
                    _check_response_complete(response)
                    text = str(getattr(response, 'output_text', '') or '').strip()
                    rows.append({'input': message, 'response': text or None, 'model': model,
                                 'provider_response_id': getattr(response, 'id', None), 'status': 'returned' if text else 'empty_response',
                                 'provider_status': getattr(response, 'status', None),
                                 'usage': {key: getattr(getattr(response, 'usage', None), key, None)
                                           for key in ('input_tokens', 'output_tokens')}})
                    if not text:
                        break
                    messages.append({'role': 'assistant', 'content': text})
                except Exception as error:
                    code, safe_message, diagnostic = provider_failure(error, 'response' if response is not None else 'generation')
                    rows.append({'input': message, 'response': None, 'status': 'failed', 'error': safe_message,
                        'error_code': code, 'error_type': type(error).__name__,
                        'provider_response_id': getattr(response, 'id', None),
                        'provider_status': getattr(response, 'status', None), 'provider_diagnostics': diagnostic,
                        'usage': {key: getattr(getattr(response, 'usage', None), key, None)
                                  for key in ('input_tokens', 'output_tokens')}})
                    break
            results.append({'name': name, 'turns': rows})
    finally:
        client.close()
    return {'status': 'executed', 'scenarios': results, 'corporate_search': False, 'production_configuration_modified': False}


def comparison_complete(report):
    """A single successful provider turn is not a completed comparison."""
    direct = report.get('direct_model', {})
    application = report.get('application', {})
    if direct.get('status') != 'executed' or application.get('status') != 'executed':
        return False
    left, right = direct.get('scenarios', []), application.get('scenarios', [])
    if len(left) != len(right) or not left:
        return False
    for before, after in zip(left, right):
        a, b = before.get('turns', []), after.get('turns', [])
        if before.get('name') != after.get('name') or not a or len(a) != len(b):
            return False
        if any(x.get('input') != y.get('input') or not x.get('response') or not y.get('response')
               or x.get('status') != 'returned' or y.get('http_status') != 200
               or y.get('engine') != 'openai' or not x.get('provider_response_id')
               or not y.get('provider_response_id') for x, y in zip(a, b)):
            return False
        ids = {row.get('conversation_id') for row in b}
        if len(ids) != 1 or None in ids:
            return False
        if (len({row.get('provider_response_id') for row in a}) != len(a)
                or len({row.get('provider_response_id') for row in b}) != len(b)):
            return False
    return True


def application_observations(application):
    observations = []
    for scenario in application.get('scenarios', []):
        groups, identifiers = {}, {}
        for index, row in enumerate(scenario.get('turns', []), 1):
            text, identity = row.get('response'), row.get('provider_response_id')
            if text:
                groups.setdefault(fingerprint(text), []).append(index)
            if identity:
                identifiers.setdefault(identity, []).append(index)
        observations.append({'scenario': scenario.get('name'),
            'verbatim_repeated_response_turns': [indices for indices in groups.values() if len(indices) > 1],
            'reused_provider_response_id_turns': [indices for indices in identifiers.values() if len(indices) > 1],
            'note': 'Повтор текста зафиксирован, но сам по себе не устанавливает причину: нужны запросы, история и результат провайдера.'})
    return observations


def main(argv=None):
    parser = argparse.ArgumentParser(description='Сравнение настоящей модели и пути приложения; приватный отчёт, без заглушек ответов.')
    parser.add_argument('--live', action='store_true', help='Разрешить платные вызовы OpenAI для этого сравнения; без флага вызовов нет.')
    parser.add_argument('--base-url', help='Проверить уже запущенный локальный сервер вместо временного приложения; SDK-запросы внешнего сервера не перехватываются.')
    parser.add_argument('--material', type=Path, help='Небольшой реальный материал: импортируется только во временную БД, исходник не меняется.')
    parser.add_argument('--technical-first-message', default=TECHNICAL_TURNS[0])
    parser.add_argument('--technical-followup', default=TECHNICAL_TURNS[1])
    parser.add_argument('--capture-content', action='store_true', help='Включить точные запросы/контекст в приватный отчёт; они могут содержать внутренние выдержки.')
    parser.add_argument('--max-provider-calls', type=int, default=24)
    parser.add_argument('--output', type=Path, default=Path('artifacts/dialogue-diagnostics/comparison.json'))
    args = parser.parse_args(argv)
    if not 1 <= args.max_provider_calls <= 24:
        parser.error('Для короткой диагностики допустим лимит от 1 до 24 вызовов генерации.')
    if args.material and args.base_url:
        parser.error('--material применяется только к временной БД; рабочая база автоматически не изменяется.')
    if args.material and not args.material.is_file():
        parser.error('Файл материала не найден.')
    from server import main as application, ai
    from server.config import settings
    from server.db import SessionLocal
    # Read the effective model selection, never print secret values.
    with SessionLocal() as session:
        models = ai.model_config(session)
    configured = bool(settings.openai_api_key)
    recorder = ProviderRecorder(live=args.live, limit=args.max_provider_calls, capture_content=args.capture_content)
    scenarios = [('basic', list(BASIC_TURNS)), ('technical', [args.technical_first_message, args.technical_followup, TECHNICAL_TURNS[2]])]
    report = {'created_at': datetime.now(timezone.utc).isoformat(), 'private_report_do_not_publish': True,
        'configured_models': models, 'openai_key_present': configured, 'live_requested': args.live,
        'provider_calls_disabled_for_offline_run': not args.live,
        'method': 'external_http' if args.base_url else 'isolated_actual_fastapi_routes',
        'scope': 'diagnostic_only_no_production_rule_changes', 'technical_accuracy_requires_source_review': True}
    from server import orchestrator
    report['source_hashes'] = {f'server/{Path(module.__file__).name}': hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
                               for module in (application, ai, orchestrator)}
    import openai
    with patch.object(openai, 'OpenAI', recorder.wrap_constructor(openai.OpenAI)):
        report['direct_model'] = run_direct(scenarios, recorder, model=models['main'], configured=configured)
        if args.base_url:
            import httpx
            url = urlparse(args.base_url)
            if url.scheme not in {'http', 'https'} or not url.netloc or url.username or url.password or url.query or url.fragment:
                parser.error('Нужен HTTP(S) адрес без пароля или токена в URL.')
            with httpx.Client(base_url=args.base_url.rstrip('/'), timeout=120, follow_redirects=False) as client:
                health = client.get('/api/health')
                report['server_health_http_status'] = health.status_code
                state = health.json() if health.status_code == 200 else {}
                if state.get('ai_configured') and not args.live:
                    report['application'] = {'status': 'not_run', 'reason': 'На сервере включена модель; для вызовов нужен явный --live.'}
                else:
                    report['application'] = {'status': 'executed', 'scenarios': run_application(client, scenarios, recorder)}
                report['server_sdk_request_capture_available'] = False
        else:
            with isolated_application(live=args.live and configured, recorder=recorder, configured_models=models) as (client, factory):
                if args.material:
                    report['material'] = install_material(client, factory, args.material)
                report['application'] = {'status': 'executed', 'scenarios': run_application(client, scenarios, recorder)}
    report['provider_requests'] = recorder.requests
    report['application_contexts'] = recorder.contexts
    report['provider_calls_started'] = sum(row.get('provider_call_started') is True for row in recorder.requests)
    report['provider_calls_returned'] = sum(row.get('status') == 'returned' for row in recorder.requests)
    report['live_comparison_completed'] = comparison_complete(report)
    report['application_observations'] = application_observations(report['application'])
    report['sdk_capture_covers_external_server'] = False if args.base_url else True
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({key: report[key] for key in ('configured_models', 'openai_key_present', 'live_requested', 'method',
        'provider_calls_started', 'provider_calls_returned', 'live_comparison_completed')}, ensure_ascii=False, indent=2))
    print('Приватный отчёт: ' + str(args.output.resolve()))
    return 0 if not args.live or report['live_comparison_completed'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
