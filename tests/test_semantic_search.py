"""Semantic retrieval uses paid-provider vectors only for reviewed, fresh sources."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from server import semantic_search as sem, workflows as wf
from server.auth import require_staff
from server.config import settings
from server.db import Base, get_session
from server.models import AIUsage, AppSetting, Document, KnowledgeChunk


@pytest.fixture
def semantic_env(tmp_path, monkeypatch):
    engine = create_engine('sqlite:///' + str(tmp_path / 'semantic.db'))
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(settings, 'openai_api_key', '')
    monkeypatch.setattr(settings, 'max_ai_daily_tokens', 100000)
    actor = SimpleNamespace(username='semantic-test', role='admin')
    app = FastAPI()
    app.include_router(sem.router)
    def sessions():
        with factory() as session:
            yield session
    app.dependency_overrides[get_session] = sessions
    app.dependency_overrides[require_staff] = lambda: actor
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, factory=factory, actor=actor, app=app)
    engine.dispose()


def solution(session, *, title='Конвейер PE38-12', symptoms='Повреждение транспортерной ленты',
             status='VERIFIED', visibility='public', model='PE38-12'):
    row = wf.VerifiedSolution(title=title, symptoms=symptoms, actions='Проверенное действие',
        limitations='Только после остановки', source='Проверено инженером', equipment_model=model,
        status=status, visibility=visibility)
    session.add(row)
    session.commit()
    return row


def document(session, *, visibility='public', marker=None):
    row = Document(title='Руководство PE38-12', filename='manual.txt', storage_name='manual.txt',
        sha256='a' * 64, size_bytes=100, status='indexed', visibility=visibility,
        models=['PE38-12'], indexing_details=marker or {})
    session.add(row)
    session.flush()
    session.add(KnowledgeChunk(document_id=row.id, page=1, models=['PE38-12'],
        text='Повреждение транспортерной ленты. Применяется только PE38-12.'))
    session.commit()
    return row


def provider(monkeypatch, callback=None, malformed=False):
    import openai
    calls = []
    class FakeClient:
        def __init__(self, **kwargs):
            self.models = SimpleNamespace(retrieve=lambda model: None)
            self.embeddings = self
        def create(self, model, input):
            calls.append(list(input))
            if callback:
                callback(input)
            data = []
            for index, text in enumerate(input):
                vector = [1, 0, 0] if ('ленты' in text or 'Коробка' in text) else [0, 1, 0]
                data.append(SimpleNamespace(index=index, embedding=[float('nan'), 0] if malformed else vector))
            return SimpleNamespace(data=data, usage=SimpleNamespace(prompt_tokens=7 * len(input)))
        def close(self):
            pass
    monkeypatch.setattr(openai, 'OpenAI', FakeClient)
    monkeypatch.setattr(settings, 'openai_api_key', 'synthetic-local-test')
    return calls


def test_paraphrase_retrieves_verified_solution_without_literal_match(semantic_env, monkeypatch):
    e = semantic_env
    with e.factory() as session:
        row = solution(session)
        assert wf.search_verified_solutions(session, 'Коробка перестала двигаться', public_only=True) == []
    calls = provider(monkeypatch)
    built = e.client.post('/api/knowledge/semantic/rebuild', json={})
    assert built.status_code == 200 and built.json()['indexed_public'] == 1
    with e.factory() as session:
        found = sem.search_semantic(session, 'Коробка перестала двигаться', equipment_model='PE38-12')
        assert [source['solution_id'] for source in found] == [row.id]
        assert found[0]['retrieval'] == 'semantic' and 'Только после остановки' in found[0]['text']
        assert sem.search_semantic(session, 'Коробка перестала двигаться', equipment_model='PE38-17') == []
        assert len(session.scalars(select(AIUsage)).all()) == 2
    assert len(calls) == 2  # One index request and one authorized query.


@pytest.mark.parametrize('origin', ['marker', 'ledger'])
def test_raw_history_and_candidate_are_never_semantic_evidence(semantic_env, monkeypatch, origin):
    e = semantic_env
    with e.factory() as session:
        solution(session, status='CANDIDATE')
        raw = document(session, visibility='internal', marker={'source_kind': 'unreviewed_history'} if origin == 'marker' else {})
        raw.category = 'Переименовано как руководство'
        session.commit()
        # The immutable ledger also protects an old index lacking source_kind.
        if origin == 'ledger':
            session.add(AppSetting(key='history_import:' + 'd' * 64, value={'document_id': raw.id}))
        session.commit()
    calls = provider(monkeypatch)
    response = e.client.post('/api/knowledge/semantic/rebuild', json={'include_internal': True})
    assert response.status_code == 200 and response.json()['built'] == 0
    assert response.json()['indexed'] == 0 and response.json()['ready'] is False
    assert calls == []


@pytest.mark.parametrize('kind', ['document', 'solution'])
def test_internal_vectors_and_revoked_sources_cannot_escape(semantic_env, monkeypatch, kind):
    e = semantic_env
    with e.factory() as session:
        internal = solution(session, visibility='internal')
        public = document(session) if kind == 'document' else solution(session)
    state = {'revoke': False}
    def revoke(_):
        if state['revoke']:
            with e.factory() as session:
                if kind == 'document':
                    session.get(Document, public.id).visibility = 'internal'
                else:
                    session.get(wf.VerifiedSolution, public.id).status = 'CANDIDATE'
                session.commit()
    calls = provider(monkeypatch, callback=revoke)
    assert e.client.post('/api/knowledge/semantic/rebuild', json={'include_internal': True}).status_code == 200
    state['revoke'] = True
    with e.factory() as session:
        found = sem.search_semantic(session, 'Коробка перестала двигаться')
        assert found == []  # Internal solution + manual withdrawn during I/O.
        assert sem.search_semantic(session, 'Коробка перестала двигаться') == []
    assert len(calls) == 2  # No paid query when zero public sources remain.


def test_stale_content_or_model_never_uses_old_vectors(semantic_env, monkeypatch):
    e = semantic_env
    with e.factory() as session:
        row = solution(session)
    calls = provider(monkeypatch)
    assert e.client.post('/api/knowledge/semantic/rebuild', json={}).status_code == 200
    with e.factory() as session:
        session.get(wf.VerifiedSolution, row.id).actions = 'Изменённое действие требует новой индексации'
        session.commit()
        assert sem.search_semantic(session, 'Коробка перестала двигаться') == []
        assert sem.semantic_status(session)['stale'] == 1
        session.add(AppSetting(key='models', value={'embedding': 'different-embedding-model'}))
        session.commit()
        assert sem.search_semantic(session, 'Коробка перестала двигаться') == []
        assert sem.semantic_status(session)['ready'] is False
    assert len(calls) == 1


def test_no_key_budget_and_permissions_are_honest(semantic_env, monkeypatch):
    e = semantic_env
    with e.factory() as session:
        solution(session)
        assert sem.search_semantic(session, 'Подача') == []
    assert e.client.post('/api/knowledge/semantic/rebuild', json={}).status_code == 503
    calls = provider(monkeypatch)
    monkeypatch.setattr(settings, 'max_ai_daily_tokens', 1)
    assert e.client.post('/api/knowledge/semantic/rebuild', json={}).status_code == 429
    assert calls == []
    e.actor.role = 'operator'
    assert e.client.post('/api/knowledge/semantic/rebuild', json={}).status_code == 403
    assert e.client.get('/api/knowledge/semantic/status').status_code == 200
    e.app.dependency_overrides.pop(require_staff)
    assert e.client.get('/api/knowledge/semantic/status').status_code == 401


def test_invalid_embeddings_are_metered_but_not_installed(semantic_env, monkeypatch):
    e = semantic_env
    with e.factory() as session:
        solution(session)
    provider(monkeypatch, malformed=True)
    response = e.client.post('/api/knowledge/semantic/rebuild', json={})
    assert response.status_code == 502
    with e.factory() as session:
        assert session.get(AppSetting, sem.INDEX_KEY).value['entries'] == []
        assert sem.semantic_status(session)['ready'] is False
        assert sem.semantic_status(session)['last_error']
        usage = session.scalar(select(AIUsage))
        assert usage.input_tokens == 7


def test_incremental_rebuild_preserves_current_vectors_and_indexes_next_batch(semantic_env, monkeypatch):
    e = semantic_env
    with e.factory() as session:
        solution(session)
        document(session)
    calls = provider(monkeypatch)
    first = e.client.post('/api/knowledge/semantic/rebuild', json={'max_items': 1}).json()
    assert first['indexed'] == 1 and first['remaining_up_to_limit'] == 1
    second = e.client.post('/api/knowledge/semantic/rebuild', json={'max_items': 1}).json()
    assert second['indexed'] == 2 and second['complete'] is True
    third = e.client.post('/api/knowledge/semantic/rebuild', json={'max_items': 1}).json()
    assert third['built'] == 0 and len(calls) == 2
