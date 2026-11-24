"""Durable, deduplicated handoff to staff; all replies stay in the owned chat.

There is no network delivery here. A request for an onsite visit is a request for
approval, never a booking or a promise of an employee's availability.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .auth import get_actor_scope, require_staff
from .db import get_session, utcnow
from .models import ChatMessage, Conversation, ConversationState, Escalation, User
from . import workflows as wf

router = APIRouter(prefix='/api')
ACTIVE_STATUSES = {'queued', 'in_progress', 'waiting_client', 'waiting_approval'}
TERMINAL_STATUSES = {'resolved', 'cancelled'}
FACT_KEYS = {'equipment_model', 'model', 'serial_number', 'company', 'name', 'contact', 'contact_name', 'email',
             'phone', 'address', 'location', 'preferred_date', 'preferred_time', 'preferred_datetime',
             'symptoms', 'problem', 'timezone'}


def _iso(value):
    return value.isoformat() + 'Z' if value is not None else None


def _bounded(value, depth=0):
    """Persist bounded snapshots, never arbitrary executable objects."""
    if depth > 4:
        return None
    if isinstance(value, dict):
        return {str(key)[:100]: _bounded(item, depth + 1) for key, item in list(value.items())[:50]
                if not any(word in str(key).lower() for word in ('password', 'token', 'secret', 'credential'))}
    if isinstance(value, (list, tuple)):
        return [_bounded(item, depth + 1) for item in value[:40]]
    if isinstance(value, str):
        return value[:4000]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:4000]


def _fact(facts: dict, *keys: str) -> str:
    for key in keys:
        value = facts.get(key)
        if isinstance(value, dict):
            value = value.get('value')
        if value is not None and str(value).strip():
            return str(value).strip()
    return ''


def serialize_escalation(item: Escalation) -> dict:
    fields = ('id', 'conversation_id', 'case_id', 'task_id', 'status', 'reason', 'summary',
              'known_facts', 'missing_information', 'attempted_actions', 'document_sources',
              'attachments', 'requested_action', 'department', 'priority', 'assignee')
    return {**{field: getattr(item, field) for field in fields},
            'created_at': _iso(item.created_at), 'updated_at': _iso(item.updated_at)}


def public_escalation(item: Escalation) -> dict:
    return {'id': item.id, 'status': item.status, 'requested_action': item.requested_action,
            'created_at': _iso(item.created_at), 'updated_at': _iso(item.updated_at),
            'notice': ('Пожелание о выезде передано на согласование. Дата, стоимость и возможность выезда пока не подтверждены.'
                       if item.requested_action == 'onsite_visit' else 'Запрос передан специалисту. Ответ появится в этом диалоге.')}


def _ensure_write_transaction(session: Session):
    # sqlite3 legacy mode does not BEGIN for reads/SAVEPOINT. Start the actual
    # outer write transaction before nested deduplication, so caller rollback
    # remains atomic and concurrent handoffs see the first committed result.
    connection = session.connection()
    if connection.dialect.name == 'sqlite':
        driver = connection.connection.driver_connection
        if not driver.in_transaction:
            connection.exec_driver_sql('BEGIN IMMEDIATE')


def _state_row(session: Session, conversation_id: str, state: dict | None = None) -> ConversationState:
    _ensure_write_transaction(session)
    row = session.get(ConversationState, conversation_id)
    if row is None:
        try:
            with session.begin_nested():
                row = ConversationState(conversation_id=conversation_id, state=deepcopy(state or {}))
                session.add(row)
                session.flush()
        except IntegrityError:
            row = session.get(ConversationState, conversation_id)
            if row is None:
                raise
    return row


def _synchronize_state(session: Session, item: Escalation):
    row = _state_row(session, item.conversation_id)
    state = deepcopy(row.state or {})
    state.update({'escalation_id': item.id, 'requires_human': item.status in ACTIVE_STATUSES,
                  'task_status': item.status, 'requested_action': item.requested_action,
                  'stage': 'resolved' if item.status in TERMINAL_STATUSES else 'waiting_specialist'})
    if item.status == 'waiting_client':
        state['stage'] = 'waiting_client'
    if item.status == 'waiting_approval':
        state['stage'] = 'awaiting_approval'
    row.state = state
    row.revision = (row.revision or 0) + 1
    row.updated_at = utcnow()


def _synchronize_workflow(session: Session, item: Escalation):
    case = session.get(wf.ServiceCase, item.case_id)
    task = session.get(wf.InternalTask, item.task_id)
    if case:
        facts = item.known_facts or {}
        for field, keys, maximum in (('company', ('company',), 240), ('contact', ('contact_name', 'contact', 'name'), 240),
                                    ('email', ('email',), 320), ('equipment_model', ('equipment_model', 'model'), 120),
                                    ('serial_number', ('serial_number',), 160)):
            value = _fact(facts, *keys)
            if value:
                setattr(case, field, value[:maximum])
        case.status = {'resolved': 'resolved', 'cancelled': 'resolved', 'in_progress': 'in_progress',
                       'waiting_client': 'waiting', 'waiting_approval': 'waiting'}.get(item.status, 'new')
        case.priority = 'high' if item.priority in {'high', 'urgent'} else 'normal'
        case.updated_at = utcnow()
    if task:
        task.status = {'resolved': 'done', 'cancelled': 'cancelled', 'in_progress': 'in_progress',
                       'waiting_client': 'waiting', 'waiting_approval': 'waiting'}.get(item.status, 'open')
        task.assignee = item.assignee or ''
        task.department = item.department
        task.updated_at = utcnow()
    _synchronize_state(session, item)


def create_or_update_escalation(session: Session, conversation: Conversation, state: dict,
                                reason: str, requested_action: str = 'remote_consultation',
                                department: str = 'Сервис', priority: str = 'normal',
                                client_message: bool = False) -> Escalation:
    """Create one case+task per chat, or refresh its snapshot, in caller's transaction."""
    _ensure_write_transaction(session)
    if requested_action not in {'remote_consultation', 'onsite_visit', 'commercial'}:
        requested_action = 'remote_consultation'
    priority = priority if priority in {'normal', 'high', 'urgent'} else 'normal'
    department = str(department or 'Сервис')[:160]
    facts = _bounded(state.get('known_facts') or {})
    snapshot = {
        'reason': str(reason or 'Клиент запросил помощь специалиста')[:2000],
        'summary': str(state.get('summary') or state.get('goal') or conversation.title)[:8000],
        'known_facts': facts,
        'missing_information': _bounded(state.get('missing_information') or []),
        'attempted_actions': _bounded(state.get('attempted_actions') or []),
        'document_sources': _bounded(state.get('source_refs') or []),
        'attachments': _bounded(state.get('attachments') or []),
    }
    item = session.scalar(select(Escalation).where(Escalation.conversation_id == conversation.id))
    created = item is None
    if item is None:
        try:
            # The unique conversation_id is the race-safe idempotency key. If
            # another request wins, its case/task survive and ours roll back.
            with session.begin_nested():
                case = wf.ServiceCase(title=conversation.title[:240], company=_fact(facts, 'company')[:240],
                    contact=_fact(facts, 'contact_name', 'contact', 'name')[:240], email=_fact(facts, 'email')[:320],
                    equipment_model=_fact(facts, 'equipment_model', 'model')[:120],
                    serial_number=_fact(facts, 'serial_number')[:160], source='WEB_CHAT',
                    priority='high' if priority in {'high', 'urgent'} else 'normal')
                session.add(case)
                session.flush()
                task = wf.InternalTask(case_id=case.id,
                    title=('Согласовать выезд: ' if requested_action == 'onsite_visit' else 'Ответить клиенту: ') + conversation.title[:320],
                    department=department, status='waiting' if requested_action == 'onsite_visit' else 'open')
                session.add(task)
                session.flush()
                item = Escalation(conversation_id=conversation.id, case_id=case.id, task_id=task.id,
                    status='waiting_approval' if requested_action == 'onsite_visit' else 'queued',
                    requested_action=requested_action, department=department, priority=priority,
                    assignee=None, **snapshot)
                session.add(item)
                session.flush()
        except IntegrityError:
            item = session.scalar(select(Escalation).where(Escalation.conversation_id == conversation.id))
            if item is None:
                raise
            created = False
    for key, value in snapshot.items():
        setattr(item, key, value)
    if not created:
        was_action = item.requested_action
        item.requested_action = requested_action
        if item.status in TERMINAL_STATUSES or (client_message and item.status == 'waiting_client'):
            item.status = 'waiting_approval' if requested_action == 'onsite_visit' else 'queued'
        elif requested_action == 'onsite_visit' and was_action != requested_action:
            item.status = 'waiting_approval'
    item.updated_at = utcnow()
    row = _state_row(session, conversation.id, state)
    row.state = deepcopy(state)
    _synchronize_workflow(session, item)
    wf.record_event(session, 'AI_ORCHESTRATOR', 'escalation.created' if created else 'escalation.updated',
                    'escalation', item.id, {'case_id': item.case_id, 'task_id': item.task_id, 'requested_action': requested_action})
    return item


class InputModel(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class EscalationRequest(InputModel):
    reason: str = Field(default='Клиент запросил помощь специалиста', min_length=1, max_length=1000)
    requested_action: Literal['remote_consultation', 'onsite_visit', 'commercial'] = 'remote_consultation'
    facts: dict[str, str] = Field(default_factory=dict, max_length=30)

    @field_validator('facts')
    @classmethod
    def safe_facts(cls, value):
        if any(key not in FACT_KEYS or len(item) > 2000 for key, item in value.items()):
            raise ValueError('Допустимы только контактные и организационные сведения, до 2000 символов')
        return value


def _owned_conversation(conversation_id: str, request: Request, response: Response, session: Session):
    scope = get_actor_scope(request, response, session)
    conversation = session.get(Conversation, conversation_id)
    if not conversation or conversation.owner_scope != scope:
        raise HTTPException(404, 'Диалог не найден')
    return conversation


@router.post('/conversations/{conversation_id}/escalate')
def request_escalation(conversation_id: str, request: Request, response: Response,
                       data: EscalationRequest = Body(default_factory=EscalationRequest),
                       session: Session = Depends(get_session)):
    conversation = _owned_conversation(conversation_id, request, response, session)
    row = _state_row(session, conversation.id)
    state = deepcopy(row.state or {})
    facts = dict(state.get('known_facts') or {})
    facts.update(data.facts)
    state['known_facts'] = facts
    state['client_confirmed'] = True
    item = create_or_update_escalation(session, conversation, state, data.reason, data.requested_action, client_message=True)
    session.commit()
    return public_escalation(item)


@router.get('/conversations/{conversation_id}/escalation')
def client_escalation(conversation_id: str, request: Request, response: Response, session: Session = Depends(get_session)):
    conversation = _owned_conversation(conversation_id, request, response, session)
    item = session.scalar(select(Escalation).where(Escalation.conversation_id == conversation.id))
    if item is None:
        return None
    return public_escalation(item)


@router.get('/escalations', dependencies=[Depends(require_staff)])
def list_escalations(status: str | None = None, session: Session = Depends(get_session)):
    statement = select(Escalation)
    if status:
        if status not in ACTIVE_STATUSES | TERMINAL_STATUSES:
            raise HTTPException(422, 'Неизвестный статус передачи')
        statement = statement.where(Escalation.status == status)
    return [serialize_escalation(item) for item in session.scalars(statement.order_by(Escalation.updated_at.desc()).limit(500))]


def _get(session: Session, escalation_id: int):
    item = session.get(Escalation, escalation_id)
    if item is None:
        raise HTTPException(404, 'Заявка специалисту не найдена')
    return item


@router.get('/escalations/{escalation_id}', dependencies=[Depends(require_staff)])
def escalation_detail(escalation_id: int, session: Session = Depends(get_session)):
    item = _get(session, escalation_id)
    conversation = session.get(Conversation, item.conversation_id)
    state = session.get(ConversationState, item.conversation_id)
    messages = session.scalars(select(ChatMessage).where(ChatMessage.conversation_id == item.conversation_id).order_by(ChatMessage.id)).all()
    notes = session.scalars(select(wf.CaseMessage).where(wf.CaseMessage.case_id == item.case_id, wf.CaseMessage.kind == 'internal').order_by(wf.CaseMessage.id)).all()
    return {**serialize_escalation(item), 'conversation': {'id': conversation.id, 'title': conversation.title,
        'messages': [{'id': msg.id, 'role': msg.role, 'content': msg.content, 'sources': msg.sources,
                      'created_at': _iso(msg.created_at)} for msg in messages],
        'state': state.state if state else {}},
        'internal_notes': [{'id': note.id, 'author': note.author, 'body': note.body, 'created_at': _iso(note.created_at)} for note in notes]}


class EscalationPatch(InputModel):
    status: Literal['queued', 'in_progress', 'waiting_client', 'waiting_approval', 'resolved', 'cancelled'] | None = None
    assignee: str | None = Field(default=None, max_length=160)
    department: str | None = Field(default=None, min_length=1, max_length=160)
    priority: Literal['normal', 'high', 'urgent'] | None = None
    internal_note: str | None = Field(default=None, min_length=1, max_length=8000)


@router.patch('/escalations/{escalation_id}')
def update_escalation(escalation_id: int, data: EscalationPatch, actor: User = Depends(require_staff), session: Session = Depends(get_session)):
    item = _get(session, escalation_id)
    if data.assignee:
        assignee = session.scalar(select(User).where(User.username == data.assignee, User.active.is_(True)))
        if not assignee or assignee.role not in {'admin', 'operator', 'engineer'}:
            raise HTTPException(422, 'Ответственный должен быть активным сотрудником')
    for key, value in data.model_dump(exclude_unset=True, exclude={'internal_note'}).items():
        if value is None and key != 'assignee':
            continue
        setattr(item, key, (value or None) if key == 'assignee' else value)
    item.updated_at = utcnow()
    if data.internal_note:
        session.add(wf.CaseMessage(case_id=item.case_id, body=data.internal_note, kind='internal', author=actor.username))
    _synchronize_workflow(session, item)
    wf.record_event(session, actor, 'escalation.updated', 'escalation', item.id,
                    {'status': item.status, 'assigned': bool(item.assignee), 'department': item.department, 'internal_note_added': bool(data.internal_note)})
    session.commit()
    return serialize_escalation(item)


class SpecialistReply(InputModel):
    content: str = Field(min_length=1, max_length=12000)
    request_information: bool = False


@router.post('/escalations/{escalation_id}/reply')
def reply_to_client(escalation_id: int, data: SpecialistReply, actor: User = Depends(require_staff), session: Session = Depends(get_session)):
    item = _get(session, escalation_id)
    if item.status in TERMINAL_STATUSES:
        raise HTTPException(409, 'Сначала возобновите закрытую заявку')
    message = ChatMessage(conversation_id=item.conversation_id, role='specialist', content=data.content, sources=[])
    session.add(message)
    session.add(wf.CaseMessage(case_id=item.case_id, body=data.content, kind='client', author=actor.username))
    item.status = 'waiting_client'
    item.assignee = item.assignee or actor.username
    item.updated_at = utcnow()
    conversation = session.get(Conversation, item.conversation_id)
    conversation.updated_at = utcnow()
    _synchronize_workflow(session, item)
    wf.record_event(session, actor, 'escalation.client_reply', 'escalation', item.id,
                    {'request_information': data.request_information, 'delivery': 'in_app', 'sent_by_email': False})
    session.commit()
    return {'escalation': serialize_escalation(item), 'message': {'id': message.id, 'role': message.role,
        'content': message.content, 'sources': [], 'created_at': _iso(message.created_at)}}
