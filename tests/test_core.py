"""Integration checks use an isolated in-memory database and temporary files only."""
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
import hashlib
import io
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from server import main, knowledge, workflows
from server.auth import hash_password
from server.config import settings
from server.db import Base, get_session, utcnow
from server.models import Document, User


@pytest.fixture()
def env(tmp_path, monkeypatch):
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(engine)
    docs = tmp_path / 'documents'
    docs.mkdir()
    monkeypatch.setattr(settings, 'storage_dir', tmp_path)
    monkeypatch.setattr(settings, 'cookie_secure', False)
    monkeypatch.setattr(settings, 'openai_api_key', '')
    monkeypatch.setattr(settings, 'app_mode', 'TEST')
    # Existing ownership/search/workflow checks explicitly exercise reference
    # mode. Model success/failure is covered separately with this disabled.
    monkeypatch.setattr(settings, 'reference_test_mode', True)
    monkeypatch.setattr(main, 'init_db', lambda: None)
    monkeypatch.setattr(main, 'SessionLocal', factory)
    monkeypatch.setattr(knowledge, 'SessionLocal', factory)
    from server import mail_sync
    monkeypatch.setattr(mail_sync, 'SessionLocal', factory)
    monkeypatch.setattr(main._indexer, 'submit', lambda fn, *args: fn(*args))
    main._rate_buckets.clear()
    def session_dependency():
        with factory() as session:
            yield session
    main.app.dependency_overrides[get_session] = session_dependency
    with factory() as session:
        session.add_all([User(username='admin', password_hash=hash_password('Test-local-password-123'), role='admin'),
                         User(username='operator', password_hash=hash_password('Test-local-password-123'), role='operator')])
        session.commit()
    with TestClient(main.app) as client:
        yield SimpleNamespace(client=client, factory=factory, tmp_path=tmp_path)
    main.app.dependency_overrides.clear()
    engine.dispose()


def login(client, username='admin'):
    result = client.post('/api/auth/login', json={'username': username, 'password': 'Test-local-password-123'})
    assert result.status_code == 200, result.text


def upload(client, name='PE38-12.txt', text='PE38-12\nРегулировка подачи короба выполняется по руководству.', visibility='internal'):
    return client.post('/api/documents', files={'files': (name, text.encode(), 'text/plain')}, data={'visibility': visibility})


def test_auth_and_cookie_guard(env):
    client = env.client
    assert client.get('/api/documents').status_code == 401
    assert client.get('/api/auth/me').json()['authenticated'] is False
    assert client.post('/api/auth/login', json={'username': 'admin', 'password': 'wrong'}).status_code == 401
    response = client.post('/api/auth/login', json={'username': 'admin', 'password': 'Test-local-password-123'})
    assert 'HttpOnly' in response.headers['set-cookie']
    assert 'SameSite=lax' in response.headers['set-cookie']
    assert client.get('/api/auth/me').json()['user']['role'] == 'admin'
    assert client.post('/api/auth/logout').status_code == 200
    assert client.get('/api/documents').status_code == 401


def test_guest_conversations_are_isolated(env):
    first = env.client
    first.get('/api/auth/me')
    created = first.post('/api/chat', json={'message': 'Ошибка оборудования'}).json()
    cid = created['conversation_id']
    assert created['engine'] == 'search_only'
    assert created['action'] == 'request_information'
    with TestClient(main.app) as other:
        assert other.get(f'/api/conversations/{cid}').status_code == 404
        assert other.post('/api/chat', json={'message': 'продолжить', 'conversation_id': cid}).status_code == 404
        assert other.delete(f'/api/conversations/{cid}').status_code == 404
        assert other.get('/api/conversations').json() == []
    assert first.get(f'/api/conversations/{cid}').json()['messages'][0]['content'] == 'Ошибка оборудования'


def test_upload_defaults_private_and_publication_controls_sources(env):
    client = env.client
    login(client)
    response = upload(client)
    assert response.status_code == 200, response.text
    document_id = response.json()[0]['id']
    indexed = client.get('/api/documents').json()[0]
    assert indexed['status'] == 'indexed' and indexed['pages'] == 1 and indexed['chunks'] > 0
    duplicate = upload(client).json()[0]
    assert duplicate['id'] == document_id and duplicate['duplicate'] is True
    assert len(client.get('/api/documents').json()) == 1
    assert client.get('/api/knowledge/search', params={'q': 'PE38-12 регулировка'}).json()['results']
    client.post('/api/auth/logout')
    assert client.get('/api/knowledge/search', params={'q': 'PE38-12 регулировка'}).json()['results'] == []
    assert client.get(f'/api/documents/{document_id}/file').status_code == 404
    login(client)
    assert client.patch(f'/api/documents/{document_id}', json={'visibility': 'public'}).status_code == 200
    client.post('/api/auth/logout')
    source = client.get('/api/knowledge/search', params={'q': 'PE38-12 регулировка'}).json()['results'][0]
    assert source['page'] == 1 and source['url'].endswith('#page=1')
    assert client.get(f'/api/documents/{document_id}/pages/1').json()['text'].startswith('PE38-12')
    assert client.get(f'/api/documents/{document_id}/file').status_code == 200


def test_model_filter_and_ambiguity_prevent_cross_machine_answers(env):
    client = env.client
    login(client)
    upload(client, 'PE38-12.txt', 'PE38-12\nРемень привода — модель A.', 'public')
    upload(client, 'PE38-17.txt', 'PE38-17\nРемень привода — модель B.', 'public')
    client.post('/api/auth/logout')
    rows = client.get('/api/knowledge/search', params={'q': 'РЕ38-12 ремень'}).json()['results']
    assert len(rows) == 1 and rows[0]['models'] == ['PE38-12']
    result = client.post('/api/chat', json={'message': 'PE38-12 и PE38-17: ремень?'}).json()
    assert result['action'] == 'request_information' and result['message']['sources'] == []
    result = client.post('/api/chat', json={'message': 'PE38-17 ремень?', 'model': 'PE38-12'}).json()
    assert result['action'] == 'request_information' and result['message']['sources'] == []
    result = client.post('/api/chat', json={'message': 'Какой ремень привода?'}).json()
    assert result['action'] == 'request_information' and result['message']['sources'] == []


def test_history_revokes_public_sources_and_filters_model_context(env, monkeypatch):
    client = env.client
    login(client)
    document_id = upload(client, visibility='public').json()[0]['id']
    client.post('/api/auth/logout')
    answer = client.post('/api/chat', json={'message': 'PE38-12 регулировка'}).json()
    cid = answer['conversation_id']
    assert answer['message']['sources']
    login(client)
    client.patch(f'/api/documents/{document_id}', json={'visibility': 'internal'})
    client.post('/api/auth/logout')
    history = client.get(f'/api/conversations/{cid}').json()['messages']
    assert history[-1]['sources'] == []
    assert 'больше не доступны' in history[-1]['content']
    seen = {}
    def fake_answer(session, message, **kwargs):
        seen.update(kwargs)
        return {'content': 'Уточните модель', 'sources': [], 'engine': 'search_only', 'action': 'request_information', 'trace': [], 'missing_information': []}
    monkeypatch.setattr(main.ai, 'answer', fake_answer)
    assert client.post('/api/chat', json={'message': 'повтори', 'conversation_id': cid}).status_code == 200
    assert 'Регулировка подачи' not in str(seen['history'])


def test_upload_validation_size_and_csrf(env, monkeypatch):
    client = env.client
    login(client)
    assert client.post('/api/documents', files={'files': ('legacy.doc', b'not a document')}).status_code == 422
    assert client.post('/api/documents', files={'files': ('fake.pdf', b'not a PDF', 'application/pdf')}).status_code == 422
    monkeypatch.setattr(settings, 'max_file_bytes', 8)
    assert upload(client).status_code == 413
    assert client.post('/api/auth/logout', headers={'Origin': 'https://attacker.example'}).status_code == 403
    assert client.post('/api/auth/logout', headers={'Sec-Fetch-Site': 'cross-site'}).status_code == 403
    assert client.get('/api/auth/me').json()['authenticated'] is True
    assert list((env.tmp_path / 'documents').iterdir()) == []


def test_pdf_pages_render_and_soft_delete(env):
    import pymupdf as fitz
    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_text((72, 72), 'PE38-12 conveyor belt adjustment manual')
    payload = pdf.tobytes()
    pdf.close()
    client = env.client
    login(client)
    response = client.post('/api/documents', files={'files': ('PE38-12.pdf', payload, 'application/pdf')})
    document_id = response.json()[0]['id']
    assert client.get('/api/documents').json()[0]['status'] == 'indexed'
    image = client.get(f'/api/documents/{document_id}/pages/1/image')
    assert image.status_code == 200 and image.content.startswith(b'\x89PNG')
    assert client.delete(f'/api/documents/{document_id}').status_code == 200
    assert client.get('/api/documents').json() == []
    assert client.get(f'/api/documents/{document_id}/file').status_code == 404


def test_settings_role_and_outgoing_safety(env):
    client = env.client
    login(client, 'operator')
    assert client.patch('/api/settings', json={'mode': 'SAFE'}).status_code == 403
    client.post('/api/auth/logout')
    login(client)
    assert client.patch('/api/settings', json={'mode': 'SAFE'}).status_code == 200
    health = client.get('/api/health').json()
    assert health['mode'] == 'SAFE' and health['safe_mode'] is True and health['mail_send_enabled'] is False
    assert client.patch('/api/settings', json={'mode': 'PRODUCTION'}).status_code == 422
    settings_data = client.get('/api/settings').json()
    assert 'api_key' not in str(settings_data)
    assert client.post('/api/settings/check-openai').json()['configured'] is False


def test_verified_solutions_are_grounded_and_model_scoped(env):
    client = env.client
    with env.factory() as session:
        session.add(workflows.VerifiedSolution(title='Ремень PE38-12', equipment_model='PE38-12', symptoms='Ремень провисает', actions='Проверить ремень A', source='internal-email-private', status='VERIFIED', visibility='public', customer_confirmed=True, verified_by='admin'))
        session.add(workflows.VerifiedSolution(title='Ремень PE38-17', equipment_model='PE38-17', symptoms='Ремень провисает', actions='Проверить ремень B', source='private-note', status='VERIFIED', visibility='internal', customer_confirmed=True, verified_by='admin'))
        session.commit()
    result = client.post('/api/chat', json={'message': 'PE38-12 ремень'}).json()
    source = result['message']['sources'][0]
    assert source['model'] == 'PE38-12' and 'ремень A' in source['text']
    assert 'internal-email-private' not in str(result)
    assert client.get(source['url']).status_code == 200
    assert client.get('/api/knowledge/solutions/2').status_code == 404
    assert client.post('/api/chat', json={'message': 'PE38-17 ремень'}).json()['message']['sources'] == []


def test_openai_failure_never_fakes_a_technical_consultation(env, monkeypatch):
    import openai
    client = env.client
    login(client)
    upload(client)
    monkeypatch.setattr(settings, 'reference_test_mode', False)
    monkeypatch.setattr(settings, 'openai_api_key', 'server-only-test-key')
    class FailedClient:
        def __init__(self, **kwargs):
            self.responses = self
            self.models = self
        def retrieve(self, model):
            return SimpleNamespace(id=model)
        def create(self, **kwargs):
            raise TimeoutError('never expose server-only-test-key')
    monkeypatch.setattr(openai, 'OpenAI', FailedClient)
    result = client.post('/api/playground', json={'message': 'PE38-12 регулировка'}).json()
    assert result['engine'] == 'unavailable' and result['ai_error']
    assert result['sources'] == [] and 'Регулировка' not in result['message']['content']
    assert result['provider_diagnostics']['provider_called'] is True
    assert 'server-only-test-key' not in str(result)


def test_ai_usage_is_recorded_even_when_generation_has_no_evidence(env, monkeypatch):
    import openai
    from server.models import AIUsage
    client = env.client
    login(client)
    upload(client)
    monkeypatch.setattr(settings, 'reference_test_mode', False)
    monkeypatch.setattr(settings, 'openai_api_key', 'test-server-key')
    class UnsupportedAnswerClient:
        def __init__(self, **kwargs):
            self.responses = self
            self.models = self
        def retrieve(self, model):
            return SimpleNamespace(id=model)
        def create(self, **kwargs):
            return SimpleNamespace(output_text='{"content":"Неподтверждённый параметр 999 мм","action":"answer","missing_information":[],"summary":"Готово"}',
                usage=SimpleNamespace(input_tokens=80, output_tokens=30))
        def close(self):
            pass
    monkeypatch.setattr(openai, 'OpenAI', UnsupportedAnswerClient)
    result = client.post('/api/playground', json={'message': 'PE38-12 регулировка'}).json()
    assert result['engine'] == 'unavailable' and result['ai_error']
    assert '999' not in result['message']['content']
    with env.factory() as session:
        usage = session.scalars(select(AIUsage)).all()
        assert len(usage) == 1 and usage[0].input_tokens == 80 and usage[0].output_tokens == 30


def test_ai_budget_stops_chargeable_calls_before_request(env, monkeypatch):
    import openai
    client = env.client
    login(client)
    upload(client)
    monkeypatch.setattr(settings, 'reference_test_mode', False)
    monkeypatch.setattr(settings, 'openai_api_key', 'test-server-key')
    monkeypatch.setattr(settings, 'max_ai_daily_tokens', 10)
    def unexpected_call(**kwargs):
        pytest.fail('OpenAI must not be constructed when token budget is insufficient')
    monkeypatch.setattr(openai, 'OpenAI', unexpected_call)
    result = client.post('/api/playground', json={'message': 'PE38-12 регулировка'}).json()
    assert result['engine'] == 'unavailable' and 'лимита' in result['ai_error']


def test_public_chat_stays_public_for_logged_in_staff(env):
    client = env.client
    login(client)
    upload(client)
    public_response = client.post('/api/chat', json={'message': 'PE38-12 регулировка'}).json()
    assert public_response['message']['sources'] == []
    assert public_response['action'] == 'request_information'
    playground = client.post('/api/playground', json={'message': 'PE38-12 регулировка'}).json()
    assert playground['sources'] and playground['message']['sources']


@pytest.mark.parametrize('url', [
    'sqlite:////tmp/posteks.db?timeout=30',
    'sqlite:////tmp/POSTEKS.DB',
    'sqlite:///C:\\backup\\POSTEKS.DB',
    'sqlite:///file:/tmp/posteks.db?uri=true&timeout=30',
])
def test_legacy_database_url_cannot_bypass_protection(url):
    from server.config import Settings
    with pytest.raises(ValueError, match='protected'):
        Settings(database_url=url)


def test_startup_migrates_new_database_and_manage_remains_idempotent(tmp_path):
    import os
    import sqlite3
    import subprocess
    import sys
    root = Path(__file__).resolve().parent.parent
    database = tmp_path / 'fresh_ai.db'
    process_env = os.environ | {'STORAGE_DIR': str(tmp_path), 'DATABASE_URL': 'sqlite:///' + str(database),
        'APP_MODE': 'TEST', 'COOKIE_SECURE': 'false', 'OPENAI_API_KEY': '', 'POSTEKS_OPENAI_API_KEY': ''}
    code = "from fastapi.testclient import TestClient\nfrom server.main import app\nwith TestClient(app) as client:\n assert client.get('/api/health').status_code == 200\n"
    first = subprocess.run([sys.executable, '-c', code], env=process_env, cwd=root, capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    second = subprocess.run([sys.executable, '-m', 'scripts.manage', 'init-db'], env=process_env, cwd=root, capture_output=True, text=True)
    assert second.returncode == 0, second.stderr
    with sqlite3.connect(database) as connection:
        assert connection.execute('select version_num from alembic_version').fetchone()[0]
        assert connection.execute("select name from sqlite_master where name='users'").fetchone()


def test_unknown_unversioned_schema_is_not_blindly_stamped(monkeypatch):
    from server import db
    from sqlalchemy import text
    engine = create_engine('sqlite://')
    with engine.begin() as connection:
        connection.execute(text('CREATE TABLE unknown_existing_data (id INTEGER PRIMARY KEY, value TEXT)'))
    monkeypatch.setattr(db, 'engine', engine)
    with pytest.raises(RuntimeError, match='без истории миграций'):
        db.init_db()
    with engine.connect() as connection:
        assert connection.execute(text("SELECT name FROM sqlite_master WHERE name='unknown_existing_data'")).fetchone()
        assert not connection.execute(text("SELECT name FROM sqlite_master WHERE name='alembic_version'")).fetchone()
    engine.dispose()


def test_authenticated_nonstaff_cannot_read_internal_sources(env):
    client = env.client
    login(client)
    document_id = upload(client).json()[0]['id']
    client.post('/api/auth/logout')
    with env.factory() as session:
        session.add(User(username='customer', password_hash=hash_password('Test-local-password-123'), role='customer'))
        session.commit()
    login(client, 'customer')
    assert client.get('/api/documents').status_code == 401
    assert client.get(f'/api/documents/{document_id}/file').status_code == 404
    assert client.get('/api/knowledge/search', params={'q': 'PE38-12 регулировка'}).json()['results'] == []
    assert client.post('/api/playground', json={'message': 'PE38-12 регулировка'}).status_code == 401
