"""Real local handoff, ownership, persistence, deduplication and in-app delivery."""
from datetime import timedelta
from types import SimpleNamespace
import uuid

import pytest
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from server import escalations as es, workflows as wf
from server.auth import AUTH_COOKIE, get_actor_scope, token_hash
from server.config import settings
from server.db import Base, get_session, utcnow
from server.models import AuthSession, ChatMessage, Conversation, ConversationState, Escalation, User


@pytest.fixture
def escalation_app(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'storage_dir', tmp_path)
    monkeypatch.setattr(settings, 'cookie_secure', False)
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    app = FastAPI()
    app.include_router(es.router)
    def sessions():
        with factory() as session:
            yield session
    app.dependency_overrides[get_session] = sessions
    with factory() as session:
        admin = User(username='engineer', password_hash='not-a-login-hash', role='engineer')
        session.add(admin)
        session.flush()
        session.add(AuthSession(user_id=admin.id, token_hash=token_hash('staff-test-session'), expires_at=utcnow() + timedelta(hours=1)))
        session.commit()
    @app.post('/test/conversation')
    def create_conversation(request: Request, response: Response, session=Depends(get_session)):
        scope = get_actor_scope(request, response, session)
        conversation = Conversation(id=str(uuid.uuid4()), owner_scope=scope, title='Нужна дистанционная помощь с оборудованием')
        session.add(conversation)
        session.flush()
        session.add(ConversationState(conversation_id=conversation.id, state={
            'goal': 'Уточнить неисправность подачи', 'known_facts': {'equipment_model': 'PE38-12'},
            'missing_information': ['Фото шильдика'], 'attempted_actions': ['Проверено описание ошибки'],
            'source_refs': [{'document_id': 10, 'page': 12}], 'attachments': [{'id': 7, 'name': 'photo.jpg'}]}))
        session.add(ChatMessage(conversation_id=conversation.id, role='user', content='Нужна помощь с подачей коробов', sources=[]))
        session.commit()
        return {'id': conversation.id}
    @app.get('/test/conversation/{conversation_id}')
    def own_history(conversation_id: str, request: Request, response: Response, session=Depends(get_session)):
        scope = get_actor_scope(request, response, session)
        conversation = session.get(Conversation, conversation_id)
        if not conversation or conversation.owner_scope != scope:
            raise HTTPException(404)
        return [{'role': msg.role, 'content': msg.content} for msg in session.scalars(select(ChatMessage).where(ChatMessage.conversation_id == conversation_id).order_by(ChatMessage.id))]
    with TestClient(app) as client, TestClient(app) as staff:
        staff.cookies.set(AUTH_COOKIE, 'staff-test-session')
        yield SimpleNamespace(client=client, staff=staff, app=app, factory=factory)
    engine.dispose()


def new_conversation(env):
    return env.client.post('/test/conversation').json()['id']


def escalate(env, cid, **values):
    return env.client.post(f'/api/conversations/{cid}/escalate', json=values)


def test_client_handoff_creates_real_case_task_once_and_keeps_context(escalation_app):
    env = escalation_app
    cid = new_conversation(env)
    created = escalate(env, cid, reason='Хочу консультацию специалиста')
    assert created.status_code == 200, created.text
    assert created.json()['status'] == 'queued'
    assert 'summary' not in created.json() and 'department' not in created.json()
    again = escalate(env, cid, reason='Дополняю запрос', facts={'serial_number': 'SN-123', 'company': 'Тестовый завод', 'contact_name': 'Тестовый клиент'})
    assert again.json()['id'] == created.json()['id']
    with env.factory() as session:
        assert session.scalar(select(func.count()).select_from(Escalation)) == 1
        assert session.scalar(select(func.count()).select_from(wf.ServiceCase)) == 1
        assert session.scalar(select(func.count()).select_from(wf.InternalTask)) == 1
        row = session.scalar(select(Escalation))
        assert row.known_facts['equipment_model'] == 'PE38-12'
        assert row.known_facts['serial_number'] == 'SN-123'
        assert row.attempted_actions == ['Проверено описание ошибки']
        assert row.document_sources == [{'document_id': 10, 'page': 12}]
        assert row.attachments == [{'id': 7, 'name': 'photo.jpg'}]
        state = session.get(ConversationState, cid).state
        assert state['goal'] == 'Уточнить неисправность подачи' and state['requires_human'] is True
        assert state['escalation_id'] == row.id and state['task_status'] == 'queued'
        assert session.get(wf.ServiceCase, row.case_id).source == 'WEB_CHAT'
        assert session.get(wf.ServiceCase, row.case_id).serial_number == 'SN-123'
        assert session.get(wf.ServiceCase, row.case_id).company == 'Тестовый завод'
        assert session.get(wf.ServiceCase, row.case_id).contact == 'Тестовый клиент'
    # A new HTTP client with the same owned cookie can still retrieve persisted status.
    with TestClient(env.app) as after_restart:
        after_restart.cookies.update(env.client.cookies)
        assert after_restart.get(f'/api/conversations/{cid}/escalation').json()['id'] == created.json()['id']


def test_handoff_owner_isolation_and_staff_queue_guard(escalation_app):
    env = escalation_app
    cid = new_conversation(env)
    escalation_id = escalate(env, cid).json()['id']
    assert env.client.get('/api/escalations').status_code == 401
    assert env.client.get(f'/api/escalations/{escalation_id}').status_code == 401
    assert env.client.post(f'/api/escalations/{escalation_id}/reply', json={'content': 'forged'}).status_code == 401
    with TestClient(env.app) as outsider:
        assert outsider.get(f'/api/conversations/{cid}/escalation').status_code == 404
        assert outsider.post(f'/api/conversations/{cid}/escalate', json={}).status_code == 404
        assert outsider.get(f'/test/conversation/{cid}').status_code == 404
    # Being a staff member is not ownership of the client-facing conversation API.
    assert env.staff.post(f'/api/conversations/{cid}/escalate', json={}).status_code == 404
    assert len(env.staff.get('/api/escalations').json()) == 1
    assert escalate(env, cid, facts={'role': 'admin'}).status_code == 422
    assert escalate(env, cid, facts={'contact': 'x' * 2001}).status_code == 422


def test_onsite_is_approval_request_with_persisted_preferences(escalation_app):
    env = escalation_app
    cid = new_conversation(env)
    response = escalate(env, cid, requested_action='onsite_visit', facts={
        'address': 'Екатеринбург, тестовый адрес', 'preferred_datetime': '2026-10-25 14:00', 'timezone': 'Asia/Yekaterinburg'})
    assert response.status_code == 200
    assert response.json()['status'] == 'waiting_approval'
    assert 'пока не подтверждены' in response.json()['notice']
    with env.factory() as session:
        row = session.get(Escalation, response.json()['id'])
        assert row.known_facts['preferred_datetime'] == '2026-10-25 14:00'
        assert session.get(wf.InternalTask, row.task_id).status == 'waiting'
        assert session.get(ConversationState, cid).state['stage'] == 'awaiting_approval'
        assert session.scalar(select(func.count()).select_from(wf.AuditEvent)) >= 1


def test_staff_queue_assignment_private_notes_and_lifecycle(escalation_app):
    env = escalation_app
    cid = new_conversation(env)
    escalation_id = escalate(env, cid).json()['id']
    assert env.staff.patch(f'/api/escalations/{escalation_id}', json={'assignee': 'missing-user'}).status_code == 422
    updated = env.staff.patch(f'/api/escalations/{escalation_id}', json={
        'status': 'in_progress', 'assignee': 'engineer', 'department': 'КБ', 'priority': 'urgent', 'internal_note': 'Служебная заметка инженера'})
    assert updated.status_code == 200, updated.text
    assert updated.json()['assignee'] == 'engineer'
    detail = env.staff.get(f'/api/escalations/{escalation_id}').json()
    assert detail['internal_notes'][0]['body'] == 'Служебная заметка инженера'
    assert detail['conversation']['state']['goal'] == 'Уточнить неисправность подачи'
    public = env.client.get(f'/api/conversations/{cid}/escalation').json()
    assert 'Служебная' not in str(public) and 'assignee' not in public
    with env.factory() as session:
        row = session.get(Escalation, escalation_id)
        assert session.get(wf.InternalTask, row.task_id).department == 'КБ'
        assert session.get(wf.InternalTask, row.task_id).status == 'in_progress'
        assert session.get(wf.ServiceCase, row.case_id).priority == 'high'
    assert env.staff.patch(f'/api/escalations/{escalation_id}', json={'status': 'resolved'}).json()['status'] == 'resolved'
    with env.factory() as session:
        assert session.get(ConversationState, cid).state['requires_human'] is False
    assert env.staff.post(f'/api/escalations/{escalation_id}/reply', json={'content': 'Новый ответ'}).status_code == 409
    # Explicitly asking again reopens the same case, not a duplicate queue item.
    reopened = escalate(env, cid, reason='Нужна дальнейшая помощь')
    assert reopened.json()['id'] == escalation_id and reopened.json()['status'] == 'queued'


def test_specialist_reply_is_in_same_client_history_without_outgoing_mail(escalation_app):
    env = escalation_app
    cid = new_conversation(env)
    escalation_id = escalate(env, cid).json()['id']
    sent = env.staff.post(f'/api/escalations/{escalation_id}/reply', json={
        'content': 'Здравствуйте. Пришлите фото шильдика целиком, чтобы проверить модификацию.', 'request_information': True})
    assert sent.status_code == 200, sent.text
    assert sent.json()['message']['role'] == 'specialist'
    assert sent.json()['escalation']['status'] == 'waiting_client'
    history = env.client.get(f'/test/conversation/{cid}').json()
    assert history[-1]['content'] == sent.json()['message']['content']
    with env.factory() as session:
        row = session.get(Escalation, escalation_id)
        assert session.get(wf.InternalTask, row.task_id).status == 'waiting'
        audit = session.scalar(select(wf.AuditEvent).where(wf.AuditEvent.action == 'escalation.client_reply'))
        assert audit.detail['sent_by_email'] is False and audit.detail['delivery'] == 'in_app'
        assert 'Здравствуйте' not in str(audit.detail)
        assert session.get(ConversationState, cid).state['stage'] == 'waiting_client'


def test_handoff_transaction_rolls_back_as_one_unit(escalation_app):
    env = escalation_app
    cid = new_conversation(env)
    with env.factory() as session:
        conversation = session.get(Conversation, cid)
        es.create_or_update_escalation(session, conversation, {'known_facts': {'equipment_model': 'PE38-12'}}, 'Temporary handoff')
        session.rollback()
    with env.factory() as session:
        assert session.scalar(select(func.count()).select_from(Escalation)) == 0
        assert session.scalar(select(func.count()).select_from(wf.ServiceCase)) == 0
        assert session.scalar(select(func.count()).select_from(wf.InternalTask)) == 0


def test_concurrent_handoffs_deduplicate_and_separate_clients(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    engine = create_engine('sqlite:///' + str(tmp_path / 'concurrency.db'), connect_args={'check_same_thread': False, 'timeout': 10})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        for number in range(2):
            session.add(Conversation(id=f'conversation-{number}', owner_scope=f'guest:{number}', title=f'Client {number}'))
        session.commit()
    def handoff(number):
        with factory() as session:
            conversation = session.get(Conversation, f'conversation-{number}')
            item = es.create_or_update_escalation(session, conversation,
                {'known_facts': {'company': f'Company {number}'}}, 'Requested expert')
            session.commit()
            return item.id, item.case_id
    with ThreadPoolExecutor(max_workers=4) as pool:
        result = list(pool.map(handoff, [0, 0, 1, 1]))
    assert result[0] == result[1] and result[2] == result[3] and result[0] != result[2]
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Escalation)) == 2
        assert session.scalar(select(func.count()).select_from(wf.ServiceCase)) == 2
        assert {row.known_facts['company'] for row in session.scalars(select(Escalation))} == {'Company 0', 'Company 1'}
    engine.dispose()


def test_client_response_requeues_same_specialist_case_and_task(escalation_app):
    env = escalation_app
    cid = new_conversation(env)
    escalation_id = escalate(env, cid).json()['id']
    original = env.staff.get(f'/api/escalations/{escalation_id}').json()
    replied = env.staff.post(f'/api/escalations/{escalation_id}/reply', json={
        'content': 'Пришлите серийный номер с шильдика', 'request_information': True})
    assert replied.json()['escalation']['status'] == 'waiting_client'
    with env.factory() as session:
        conversation = session.get(Conversation, cid)
        state = dict(session.get(ConversationState, cid).state)
        state['known_facts'] = dict(state.get('known_facts') or {}) | {'serial_number': 'CLIENT-ANSWER-123'}
        item = es.create_or_update_escalation(session, conversation, state, 'Клиент прислал сведения', client_message=True)
        session.commit()
        assert item.id == escalation_id and item.case_id == original['case_id'] and item.task_id == original['task_id']
        assert item.status == 'queued'
        assert session.get(wf.InternalTask, item.task_id).status == 'open'
        assert session.get(wf.ServiceCase, item.case_id).serial_number == 'CLIENT-ANSWER-123'
        assert session.get(ConversationState, cid).state['task_status'] == 'queued'
        assert session.scalar(select(func.count()).select_from(Escalation)) == 1
