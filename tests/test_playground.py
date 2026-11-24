"""Private multi-turn Playground: durable memory without service side effects."""
from datetime import timedelta
from types import SimpleNamespace
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from server import main, playground, workflows as wf
from server.auth import AUTH_COOKIE, token_hash
from server.config import settings
from server.db import Base, get_session, utcnow
from server.models import (AppSetting, AuthSession, ChatMessage, Conversation, ConversationState,
                           Document, DocumentPage, Escalation, KnowledgeChunk, User)


@pytest.fixture
def playground_app(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'storage_dir', tmp_path)
    monkeypatch.setattr(settings, 'cookie_secure', False)
    monkeypatch.setattr(settings, 'openai_api_key', '')
    monkeypatch.setattr(settings, 'app_mode', 'TEST')
    # Explicit offline workflow fixtures, never evidence of a live model.
    monkeypatch.setattr(settings, 'reference_test_mode', True)
    main._rate_buckets.clear()
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    app = FastAPI()
    app.include_router(playground.router)
    app.add_api_route('/api/conversations', main.conversations, methods=['GET'])
    app.add_api_route('/api/conversations/{conversation_id}', main.conversation_detail, methods=['GET'])
    def sessions():
        with factory() as session:
            yield session
    app.dependency_overrides[get_session] = sessions
    with factory() as session:
        users = [User(username='tester-one', password_hash='not-a-login-hash', role='operator'),
                 User(username='tester-two', password_hash='not-a-login-hash', role='engineer')]
        session.add_all(users)
        session.flush()
        for number, user in enumerate(users, 1):
            session.add(AuthSession(user_id=user.id, token_hash=token_hash(f'playground-test-{number}'), expires_at=utcnow() + timedelta(hours=1)))
        session.commit()
    with TestClient(app) as client, TestClient(app) as other:
        client.cookies.set(AUTH_COOKIE, 'playground-test-1')
        other.cookies.set(AUTH_COOKIE, 'playground-test-2')
        yield SimpleNamespace(client=client, other=other, factory=factory, app=app)
    engine.dispose()


def turn(env, message, conversation_id=None, **options):
    payload = {'message': message, **options}
    if conversation_id:
        payload['conversation_id'] = conversation_id
    response = env.client.post('/api/playground', json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def test_playground_persists_memory_and_never_creates_real_handoffs(playground_app):
    env = playground_app
    first = turn(env, 'PE38-12: не получается проверить ремень подачи', use_knowledge=False)
    cid = first['conversation_id']
    assert first['state']['known_facts']['equipment_model'] == 'PE38-12'
    second = turn(env, 'Мне нужен специалист', cid, use_knowledge=False)
    assert second['state']['requires_human'] is True and second['escalation'] is None
    assert 'реальная заявка не создана' in second['message']['content']
    with TestClient(env.app) as after_restart:
        after_restart.cookies.update(env.client.cookies)
        response = after_restart.post('/api/playground', json={
            'message': 'Хочу выезд. Адрес: Екатеринбург, улица Тестовая, дом 10',
            'conversation_id': cid, 'use_knowledge': False})
        assert response.status_code == 200, response.text
        third = response.json()
    assert third['state']['requested_action'] == 'onsite_visit'
    assert third['state']['known_facts']['equipment_model'] == 'PE38-12'
    assert 'Тестовая' in third['state']['known_facts']['address']
    assert third['escalation'] is None
    fourth = turn(env, '25 октября в 14:00', cid, use_knowledge=False)
    assert fourth['state']['turn_count'] == 4
    assert fourth['state']['known_facts']['preferred_datetime'] == '25 октября в 14:00'
    with env.factory() as session:
        assert session.get(Conversation, cid).owner_scope == 'playground:user:1'
        persisted = session.get(ConversationState, cid).state
        assert persisted['turn_count'] == 4 and persisted['goal'] == first['state']['goal']
        assert session.scalar(select(func.count()).select_from(ChatMessage).where(ChatMessage.conversation_id == cid)) == 8
        for model in (Escalation, wf.ServiceCase, wf.InternalTask, wf.VerifiedSolution):
            assert session.scalar(select(func.count()).select_from(model)) == 0


def test_playground_success_never_creates_solution_candidate(playground_app):
    env = playground_app
    first = turn(env, 'PE38-12: вопрос по подаче', use_knowledge=False)
    turn(env, 'Проверка не помогла', first['conversation_id'], use_knowledge=False)
    result = turn(env, 'Спасибо, всё удалось исправить, теперь работает', first['conversation_id'], use_knowledge=False)
    assert result['conversation_id'] == first['conversation_id']
    with env.factory() as session:
        assert session.scalar(select(func.count()).select_from(wf.VerifiedSolution)) == 0
        assert session.scalar(select(func.count()).select_from(Escalation)) == 0


def test_playground_scope_is_separate_from_public_client_and_other_staff(playground_app):
    env = playground_app
    result = turn(env, 'PE38-12: тестовое описание', use_knowledge=False)
    cid = result['conversation_id']
    assert env.client.get(f'/api/conversations/{cid}').status_code == 404
    assert env.client.get('/api/conversations').json() == []
    assert env.other.post('/api/playground', json={'conversation_id': cid, 'message': 'чужое продолжение'}).status_code == 404
    with TestClient(env.app) as guest:
        assert guest.post('/api/playground', json={'message': 'анонимный доступ'}).status_code == 401


def test_playground_knowledge_switch_controls_actual_retrieval(playground_app):
    env = playground_app
    text = 'PE38-12. Ремень подачи: проверьте читаемость маркировки на неподвижном оборудовании.'
    with env.factory() as session:
        document = Document(title='PE38-12 руководство', filename='PE38-12.txt', storage_name='playground-manual.txt',
            sha256='a' * 64, mime_type='text/plain', size_bytes=len(text.encode()), category='Документация',
            models=['PE38-12'], visibility='internal', status='indexed', pages=1, chunks=1)
        session.add(document)
        session.flush()
        session.add(DocumentPage(document_id=document.id, page=1, text=text))
        session.add(KnowledgeChunk(document_id=document.id, page=1, text=text, models=['PE38-12']))
        session.commit()
        document_id = document.id
    disabled = turn(env, 'PE38-12: ремень подачи', use_knowledge=False)
    assert disabled['sources'] == [] and disabled['message']['sources'] == []
    enabled = turn(env, 'PE38-12: ремень подачи', use_knowledge=True, document_id=document_id)
    assert enabled['sources'] and enabled['sources'][0]['document_id'] == document_id
    assert enabled['sources'][0]['page'] == 1
    assert enabled['message']['sources'] == enabled['sources']
    assert 'маркировки' in enabled['message']['content']


def test_scenario_create_list_delete_are_staff_owned(playground_app):
    env = playground_app
    payload = {'title': 'Дистанционная помощь и передача человеку', 'steps': [
        {'message': 'PE38-12: вопрос по оборудованию', 'equipment_model': 'PE38-12', 'use_knowledge': True},
        {'message': 'Мне нужен специалист', 'expected_action': 'escalate', 'notes': 'В Playground заявка не создаётся'}]}
    created = env.client.post('/api/playground/scenarios', json=payload)
    assert created.status_code == 201
    scenario = created.json()
    assert env.client.get('/api/playground/scenarios').json()[0]['id'] == scenario['id']
    assert env.other.get('/api/playground/scenarios').json() == []
    assert env.other.delete('/api/playground/scenarios/' + scenario['id']).status_code == 404
    with env.factory() as session:
        assert session.get(AppSetting, 'playground_scenarios:user:1').value['scenarios'][0]['steps'] == scenario['steps']
    assert env.client.delete('/api/playground/scenarios/' + scenario['id']).json()['ok'] is True
    assert env.client.get('/api/playground/scenarios').json() == []


def test_evaluation_is_persisted_and_rejects_foreign_request_or_conversation(playground_app):
    env = playground_app
    result = turn(env, 'PE38-12: первый тест', use_knowledge=False)
    request_id, cid = result['request_id'], result['conversation_id']
    response = env.client.post('/api/playground/evaluations', json={
        'request_id': request_id, 'conversation_id': cid, 'rating': 'incorrect', 'notes': 'Нужно объяснить проще'})
    assert response.status_code == 201, response.text
    assert 'user_id' not in response.json()
    with env.factory() as session:
        stored = session.get(AppSetting, 'playground_evaluation:' + response.json()['id']).value
        assert stored['request_id'] == request_id and stored['user_id'] == 1 and stored['rating'] == 'incorrect'
        run = session.get(AppSetting, 'playground_run:' + request_id).value
        assert run['conversation_id'] == cid and run['user_id'] == 1
    assert env.other.post('/api/playground/evaluations', json={'request_id': request_id, 'rating': 'correct'}).status_code == 404
    assert env.other.post('/api/playground/evaluations', json={'conversation_id': cid, 'rating': 'correct'}).status_code == 404
    second = turn(env, 'Другой тест', use_knowledge=False)
    assert env.client.post('/api/playground/evaluations', json={
        'request_id': request_id, 'conversation_id': second['conversation_id'], 'rating': 'correct'}).status_code == 422


def test_compare_requires_key_and_two_configured_text_models(playground_app, monkeypatch):
    env = playground_app
    assert env.client.post('/api/playground/compare', json={'message': 'Сравнение', 'models': ['test-main', 'test-fast']}).status_code == 422
    monkeypatch.setattr(settings, 'openai_api_key', 'test-server-only-key')
    with env.factory() as session:
        session.add(AppSetting(key='models', value={'main': 'test-main', 'fast': 'test-fast', 'vision': 'test-vision',
            'audio': 'test-audio', 'embedding': 'test-embedding'}))
        session.commit()
    calls = []
    def fake_answer(session, message, **kwargs):
        calls.append(kwargs)
        return {'content': 'Результат проверки ' + kwargs['ai_model'], 'sources': [], 'request_id': str(uuid.uuid4()),
                'action': 'answer', 'engine': 'openai', 'latency_ms': 7, 'usage': {'input_tokens': 10, 'output_tokens': 5}}
    monkeypatch.setattr(playground.ai, 'answer', fake_answer)
    comparison = env.client.post('/api/playground/compare', json={
        'message': 'Сравнение', 'models': ['test-main', 'test-fast'], 'use_knowledge': False})
    assert comparison.status_code == 200, comparison.text
    results = comparison.json()['results']
    assert [item['model'] for item in results] == ['test-main', 'test-fast']
    assert [item['request_id'] for item in results][0] != results[1]['request_id']
    assert all(call['allow_actions'] is False and call['include_internal'] is True and call['use_knowledge'] is False for call in calls)
    with env.factory() as session:
        runs = session.scalars(select(AppSetting).where(AppSetting.key.like('playground_run:%'))).all()
        assert len(runs) == 2 and all(run.value['user_id'] == 1 for run in runs)
        assert session.scalar(select(func.count()).select_from(wf.ServiceCase)) == 0
    main._rate_buckets.clear()  # Validate model policy independently of the separately covered request limit.
    assert env.client.post('/api/playground/compare', json={'message': 'Тест', 'models': ['test-main', 'test-main']}).status_code == 422
    assert env.client.post('/api/playground/compare', json={'message': 'Тест', 'models': ['test-main', 'not-configured']}).status_code == 422
    assert env.client.post('/api/playground/compare', json={'message': 'Тест', 'models': ['test-main', 'test-audio']}).status_code == 422
    assert env.client.post('/api/playground/compare', json={'message': 'Тест', 'models': ['test-main', 'test-embedding']}).status_code == 422
    assert len(calls) == 2
