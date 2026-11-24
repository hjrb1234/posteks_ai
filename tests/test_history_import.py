from types import SimpleNamespace
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from server import history_import, knowledge
from server.auth import require_staff
from server.config import settings
from server.db import Base, get_session
from server.models import AppSetting, Document


@pytest.fixture
def history_app(tmp_path, monkeypatch):
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(settings, 'storage_dir', tmp_path)
    (tmp_path / 'documents').mkdir()
    monkeypatch.setattr(knowledge, 'SessionLocal', factory)
    app = FastAPI()
    app.include_router(history_import.router)
    def session_dependency():
        with factory() as session:
            yield session
    app.dependency_overrides[get_session] = session_dependency
    client = TestClient(app)
    yield client, app, factory, tmp_path
    engine.dispose()


def test_history_parser_formats_and_no_screenshot_guessing():
    text = 'Иван: Проверили привод.\nМария: Нужна проверка инженера.'
    parsed = history_import.parse_history(text.encode('cp1251'), 'history.txt')
    assert parsed['messages'][0]['text'] == text
    assert parsed['messages'][0]['sender'] == ''
    assert parsed['conversation_count'] is None
    rows = {'messages': [{'from': 'Иван', 'date': '2026-10-20', 'chat_id': 'A', 'text': ['Добрый ', {'text': 'день'}]}]}
    parsed = history_import.parse_history(json.dumps(rows).encode(), 'history.json')
    assert parsed['messages'][0]['text'] == 'Добрый день'
    assert parsed['conversation_count'] == 1
    parsed = history_import.parse_history('автор;дата;сообщение\nИван;2026-10-20;Привод остановлен'.encode(), 'history.csv')
    assert parsed['messages'][0]['sender'] == 'Иван'
    for raw, filename in [(b'PNG', 'screen.png'), (b'{}', 'history.json'), (b'\x00data', 'history.txt'), (b'', 'history.txt')]:
        with pytest.raises(ValueError):
            history_import.parse_history(raw, filename)


def test_private_import_requires_explicit_confirmation_and_never_verifies(history_app):
    client, app, factory, tmp_path = history_app
    assert client.get('/api/history/imports').status_code == 401
    app.dependency_overrides[require_staff] = lambda: SimpleNamespace(username='reviewer', role='admin')
    file = {'file': ('history.json', json.dumps({'messages': [{'text': 'Не утверждённый совет из истории', 'sender': 'Инженер'}]}).encode(), 'application/json')}
    preview = client.post('/api/history/preview', files=file)
    assert preview.status_code == 200 and preview.json()['message_count'] == 1
    with factory() as session:
        assert not session.scalars(select(Document)).all()
    assert not (tmp_path / 'history').exists()
    assert client.post('/api/history/import', files=file).status_code == 409
    response = client.post('/api/history/import', files=file, data={'confirmed': 'true', 'source': 'MAX'})
    assert response.status_code == 201, response.text
    result = response.json()
    assert result['status'] == 'unreviewed' and result['visibility'] == 'internal'
    with factory() as session:
        document = session.get(Document, result['document_id'])
        assert document.status == 'indexed' and document.visibility == 'internal'
        assert document.indexing_details.get('source_kind') == 'unreviewed_history'
        assert session.get(AppSetting, history_import.PREFIX + result['sha256'])
    assert client.get(result['raw_url']).headers['content-disposition'].startswith('attachment;')
    again = client.post('/api/history/import', files=file, data={'confirmed': 'true', 'source': 'MAX'}).json()
    assert again['duplicate'] and again['document_id'] == result['document_id']
    assert len(client.get('/api/history/imports').json()) == 1
    app.dependency_overrides.pop(require_staff)
    assert client.get(result['raw_url']).status_code == 401
