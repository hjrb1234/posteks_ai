"""Private reproducible dialogue tests; persisted memory with no business effects."""
from __future__ import annotations
from copy import deepcopy
import uuid
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from . import ai
from .auth import require_staff
from .config import settings, effective_mode
from .db import get_session, utcnow
from .models import AppSetting, ChatMessage, Conversation, User
from .orchestrator import conversation_lock

router = APIRouter(prefix='/api/playground', dependencies=[Depends(require_staff)])


class InputModel(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class PlaygroundBody(InputModel):
    message: str = Field(min_length=1, max_length=6000)
    conversation_id: str | None = Field(default=None, max_length=50)
    equipment_model: str | None = Field(default=None, max_length=100)
    model: str | None = Field(default=None, max_length=100)
    document_id: int | None = None
    use_knowledge: bool = True
    attachment_ids: list[str] = Field(default_factory=list, max_length=4)


@router.post('')
def playground(body: PlaygroundBody, request: Request, user: User = Depends(require_staff), session: Session = Depends(get_session)):
    from .main import message_dict, rate_limit
    from .media import owned_attachments
    rate_limit(request, 'playground', 20)
    scope = f'playground:user:{user.id}'
    with conversation_lock(body.conversation_id or uuid.uuid4().hex):
        conversation = session.get(Conversation, body.conversation_id) if body.conversation_id else None
        if body.conversation_id and (not conversation or conversation.owner_scope != scope):
            raise HTTPException(404, 'Тестовый диалог не найден')
        if conversation is None:
            conversation = Conversation(id=str(uuid.uuid4()), owner_scope=scope, title=body.message[:100])
            session.add(conversation)
            session.flush()
        attachments = owned_attachments(session, body.attachment_ids, f'user:{user.id}', conversation.id)
        history = session.scalars(select(ChatMessage).where(ChatMessage.conversation_id == conversation.id).order_by(ChatMessage.id.desc()).limit(10)).all()
        message = ChatMessage(conversation_id=conversation.id, role='user', content=body.message, sources=[])
        session.add(message)
        session.flush()
        if attachments:
            session.add(AppSetting(key='chat_message_attachments:' + str(message.id), value={'attachments': attachments}))
        session.commit()
        result = ai.answer(session, body.message, conversation=conversation, equipment_model=body.equipment_model,
            document_id=body.document_id, include_internal=True, ai_model=body.model, attachments=attachments,
            use_knowledge=body.use_knowledge, allow_actions=False,
            require_model=not (settings.reference_test_mode and effective_mode(session) == 'TEST'),
            history=[{'role': m.role, 'content': m.content[:2000]} for m in reversed(history)])
        _save_run(session, user, result, conversation.id)
        assistant = ChatMessage(conversation_id=conversation.id, role='assistant', content=result.pop('content'), sources=result.pop('sources'))
        session.add(assistant)
        session.flush()
        session.add(AppSetting(key='chat_message_meta:' + str(assistant.id), value={
            'engine': result.get('engine'), 'error': result.get('error'),
            'model': result.get('model'), 'provider_response_id': result.get('provider_response_id')}))
        conversation.updated_at = utcnow()
        session.commit()
        return {'conversation_id': conversation.id, 'message': message_dict(assistant, session, True), 'sources': assistant.sources, **result}


class ScenarioStep(InputModel):
    message: str = Field(min_length=1, max_length=6000)
    equipment_model: str | None = Field(default=None, max_length=100)
    document_id: int | None = None
    use_knowledge: bool = True
    expected_action: str | None = Field(default=None, max_length=50)
    notes: str = Field(default='', max_length=2000)


class ScenarioBody(InputModel):
    title: str = Field(min_length=1, max_length=160)
    steps: list[ScenarioStep] = Field(min_length=1, max_length=25)


def _scenario_setting(session, user):
    key = f'playground_scenarios:user:{user.id}'
    return session.get(AppSetting, key), key


@router.get('/scenarios')
def scenarios(user: User = Depends(require_staff), session: Session = Depends(get_session)):
    row, _ = _scenario_setting(session, user)
    return deepcopy(row.value.get('scenarios', [])) if row else []


@router.post('/scenarios', status_code=201)
def save_scenario(body: ScenarioBody, user: User = Depends(require_staff), session: Session = Depends(get_session)):
    row, key = _scenario_setting(session, user)
    items = deepcopy(row.value.get('scenarios', [])) if row else []
    if len(items) >= 50:
        raise HTTPException(422, 'Сохранено 50 сценариев. Удалите ненужные перед созданием нового.')
    item = {'id': str(uuid.uuid4()), **body.model_dump(), 'created_at': utcnow().isoformat() + 'Z'}
    items.insert(0, item)
    if row:
        row.value = {'scenarios': items}
    else:
        session.add(AppSetting(key=key, value={'scenarios': items}))
    session.commit()
    return item


@router.delete('/scenarios/{scenario_id}')
def delete_scenario(scenario_id: str, user: User = Depends(require_staff), session: Session = Depends(get_session)):
    row, _ = _scenario_setting(session, user)
    items = row.value.get('scenarios', []) if row else []
    remaining = [item for item in items if item['id'] != scenario_id]
    if len(remaining) == len(items):
        raise HTTPException(404, 'Сценарий не найден')
    row.value = {'scenarios': remaining}
    session.commit()
    return {'ok': True}


class CompareBody(InputModel):
    message: str = Field(min_length=1, max_length=6000)
    models: list[str] = Field(min_length=2, max_length=2)
    equipment_model: str | None = Field(default=None, max_length=100)
    document_id: int | None = None
    use_knowledge: bool = True


@router.post('/compare')
def compare_models(body: CompareBody, request: Request, user: User = Depends(require_staff), session: Session = Depends(get_session)):
    from .main import rate_limit
    rate_limit(request, 'playground-compare', 5)
    if not settings.openai_api_key:
        raise HTTPException(422, 'Для сравнения моделей задайте серверный ключ OpenAI и проверьте доступность моделей в настройках.')
    configuration = ai.model_config(session)
    allowed = {configuration[key] for key in ('main', 'fast', 'vision')}
    if len(set(body.models)) != 2 or any(model not in allowed for model in body.models):
        raise HTTPException(422, 'Выберите две разные модели из серверных настроек.')
    results = []
    for model in body.models:
        result = ai.answer(session, body.message, ai_model=model, equipment_model=body.equipment_model,
                           document_id=body.document_id, include_internal=True, use_knowledge=body.use_knowledge, allow_actions=False)
        _save_run(session, user, result, None)
        session.commit()
        content, sources = result.pop('content'), result.pop('sources')
        results.append({'model': model, 'message': {'role': 'assistant', 'content': content, 'sources': sources}, 'sources': sources, **result})
    return {'results': results, 'configuration_only': False}


class EvaluationBody(InputModel):
    request_id: str | None = Field(default=None, max_length=60)
    conversation_id: str | None = Field(default=None, max_length=50)
    rating: Literal['correct', 'incorrect']
    notes: str = Field(default='', max_length=2000)


@router.post('/evaluations', status_code=201)
def evaluation(body: EvaluationBody, user: User = Depends(require_staff), session: Session = Depends(get_session)):
    if body.conversation_id:
        conv = session.get(Conversation, body.conversation_id)
        if not conv or conv.owner_scope != f'playground:user:{user.id}':
            raise HTTPException(404, 'Тестовый диалог не найден')
    if body.request_id:
        run = session.get(AppSetting, 'playground_run:' + body.request_id)
        if not run or run.value.get('user_id') != user.id:
            raise HTTPException(404, 'Тестовый запрос не найден')
        if body.conversation_id and run.value.get('conversation_id') != body.conversation_id:
            raise HTTPException(422, 'Запрос относится к другому тестовому диалогу')
    record_id = str(uuid.uuid4())
    item = {'id': record_id, 'user_id': user.id, **body.model_dump(), 'created_at': utcnow().isoformat() + 'Z'}
    session.add(AppSetting(key='playground_evaluation:' + record_id, value=item))
    session.commit()
    return {key: value for key, value in item.items() if key != 'user_id'}


def _save_run(session, user, result, conversation_id):
    run_id = result['request_id']
    session.add(AppSetting(key='playground_run:' + run_id, value={
        'request_id': run_id, 'user_id': user.id, 'conversation_id': conversation_id,
        'action': result['action'], 'engine': result['engine'],
        'latency_ms': result['latency_ms'], 'usage': result.get('usage'),
        'created_at': utcnow().isoformat() + 'Z'}))
