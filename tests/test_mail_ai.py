"""Mail is a separate task from chat: actual thread, bounded claims, staff review."""
import json
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import func, select

from test_workflows import workflow_app, eml, upload_mail
from server import mail_ai
from server.config import settings
from server.models import AIUsage
from server.workflows import Approval, InboxMessage, VerifiedSolution


def provider(monkeypatch, output):
    import openai
    calls = []
    class Client:
        def __init__(self, **kwargs):
            self.models = SimpleNamespace(retrieve=lambda model: None)
            self.responses = self
        def create(self, **kwargs):
            calls.append(kwargs)
            value = output(kwargs) if callable(output) else output
            return SimpleNamespace(output_text=json.dumps(value, ensure_ascii=False),
                usage=SimpleNamespace(input_tokens=200, output_tokens=90))
        def close(self):
            pass
    monkeypatch.setattr(openai, 'OpenAI', Client)
    monkeypatch.setattr(settings, 'openai_api_key', 'test-only-key')
    return calls


def plan(content, kind='administrative', intent='logistics', claims=None):
    return {'intent': intent, 'answer_kind': kind, 'content': content,
            'technical_claims': claims or [], 'missing_information': [], 'reason': 'Тестовая проверка цели письма'}


def first_mail(client):
    return client.get('/api/inbox?direction=all&kind=all').json()[0]


def test_address_notice_creates_mail_specific_reply_not_chat_acknowledgement(workflow_app, monkeypatch):
    client, _, factory, _ = workflow_app
    upload_mail(client, eml(subject='Изменение адреса доставки', body='Добрый день! Просим учесть новый адрес доставки в следующем заказе.'))
    message = first_mail(client)
    calls = provider(monkeypatch, plan('Здравствуйте!\n\nПолучили ваше уведомление. Уточните, пожалуйста, к какому заказу относятся новые реквизиты.\n\nС уважением, сервис ПОСТЭКС'))
    response = client.post(f"/api/inbox/{message['id']}/draft", json={'reply_goal': 'Уточнить номер заказа'})
    assert response.status_code == 201, response.text
    data = response.json()
    assert 'к какому заказу относятся новые реквизиты' in data['body']
    assert data['generation']['intent'] == 'logistics' and data['generation']['engine'] == 'openai'
    payload = json.loads(calls[0]['input'])
    assert payload['SELECTED_MESSAGE']['id'] == message['id']
    assert payload['SELECTED_MESSAGE']['subject'] == 'Изменение адреса доставки'
    assert payload['THREAD'][0]['sender'] == 'client@tests.invalid'
    assert payload['REPLY_GOAL'] == 'Уточнить номер заказа'
    assert 'не клиентский чат' in calls[0]['instructions']
    assert 'встреч' not in data['body'].casefold() and 'диалоге' not in data['body'].casefold()
    assert data['recipient'] == 'client@tests.invalid' and data['status'] == 'PENDING'
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(AIUsage)) == 1


def test_old_chat_filler_and_unsourced_actions_are_rejected_without_approval(workflow_app, monkeypatch):
    client, _, factory, _ = workflow_app
    upload_mail(client, eml(subject='Изменение адреса'))
    message = first_mail(client)
    for text in [
        'Сведения сохранены в этом диалоге. Пожелание по встрече можно передать сотруднику.',
        'Мы уже изменили адрес. Специалист приедет завтра.',
        'Стоимость составляет 5000 рублей.',
    ]:
        provider(monkeypatch, plan(text))
        response = client.post(f"/api/inbox/{message['id']}/draft", json={})
        assert response.status_code == 503, response.text
        assert response.json()['detail']['code'] == 'ai_draft_unavailable'
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Approval)) == 0
        assert session.scalar(select(func.count()).select_from(AIUsage)) == 3


def test_draft_technical_content_is_composed_only_from_verified_public_quotes(workflow_app, monkeypatch):
    client, _, factory, _ = workflow_app
    upload_mail(client, eml(subject='PE38-12 подача короба', body='Не работает подача PE38-12. Как проверить датчик?'))
    message = first_mail(client)
    with factory() as session:
        session.add(VerifiedSolution(title='PE38-12: подача', equipment_model='PE38-12', symptoms='Подача короба',
            actions='Проверку датчика выполняет квалифицированный специалист по руководству.',
            limitations='Для PE38-12, только при остановленном оборудовании.', source='synthetic',
            status='VERIFIED', visibility='public', verified_by='staff'))
        session.commit()
    def output(kwargs):
        payload = json.loads(kwargs['input'])
        assert payload['EQUIPMENT_MODEL'] == 'PE38-12'
        assert payload['SOURCES'] and 'Проверку датчика' in payload['SOURCES'][0]['text']
        return plan('НЕПОДТВЕРЖДЕННЫЕ 999 мм', kind='technical', intent='technical_support',
            claims=[{'source_index': 1, 'quote': 'Проверку датчика выполняет квалифицированный специалист по руководству.'}])
    provider(monkeypatch, output)
    response = client.post(f"/api/inbox/{message['id']}/draft", json={})
    assert response.status_code == 201, response.text
    assert '999' not in response.json()['body']
    assert 'Проверку датчика выполняет' in response.json()['body']


def test_revoked_solution_during_generation_does_not_enter_draft(workflow_app, monkeypatch):
    client, _, factory, _ = workflow_app
    upload_mail(client, eml(subject='PE38-12 подача', body='Не работает подача PE38-12.'))
    message = first_mail(client)
    with factory() as session:
        row = VerifiedSolution(title='PE38-12 подача', equipment_model='PE38-12', symptoms='Подача',
            actions='Закрытая инструкция', source='test', status='VERIFIED', visibility='public')
        session.add(row); session.commit(); sid = row.id
    def output(kwargs):
        with factory() as other:
            other.get(VerifiedSolution, sid).visibility = 'internal'
            other.commit()
        return plan('Технический текст', kind='technical', intent='technical_support',
            claims=[{'source_index': 1, 'quote': 'Закрытая инструкция'}])
    provider(monkeypatch, output)
    response = client.post(f"/api/inbox/{message['id']}/draft", json={})
    assert response.status_code == 503 and response.json()['detail']['code'] == 'ai_draft_unavailable'
    assert client.get('/api/approvals').json() == []


def test_learning_preview_proposes_but_does_not_save_or_verify_experience(workflow_app, monkeypatch):
    client, app, factory, _ = workflow_app
    app.include_router(mail_ai.router)
    body = 'PE38-12: конвейер остановился.\nПроверили датчик и восстановили разъём.\nПосле проверки подача работает.'
    upload_mail(client, eml(body=body))
    message = first_mail(client)
    provider(monkeypatch, {'found_solution': True, 'title': 'Восстановление подачи', 'equipment_model': 'PE38-12',
        'symptoms': 'PE38-12: конвейер остановился.', 'actions': 'Проверили датчик и восстановили разъём.',
        'limitations': 'Непроверенная история; нужна проверка сотрудником.',
        'evidence': [{'message_id': message['id'], 'quote': body}], 'warnings': []})
    response = client.post(f"/api/inbox/{message['id']}/learning-preview")
    assert response.status_code == 200, response.text
    assert response.json()['saved'] is False and response.json()['candidate']['equipment_model'] == 'PE38-12'
    assert response.json()['warnings'] and response.json()['status'] == 'CANDIDATE_PREVIEW'
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(VerifiedSolution)) == 0


def test_thanks_or_unproven_candidate_does_not_become_experience(workflow_app, monkeypatch):
    client, app, _, _ = workflow_app
    app.include_router(mail_ai.router)
    upload_mail(client, eml(body='Спасибо за ответ.'))
    message = first_mail(client)
    provider(monkeypatch, {'found_solution': False, 'title': '', 'equipment_model': '', 'symptoms': '', 'actions': '',
        'limitations': '', 'evidence': [], 'warnings': []})
    response = client.post(f"/api/inbox/{message['id']}/learning-preview")
    assert response.status_code == 422 and response.json()['detail']['code'] == 'no_confirmed_experience'


def test_missing_key_is_explicit_and_does_not_create_placeholder(workflow_app, monkeypatch):
    client, _, _, _ = workflow_app
    upload_mail(client, eml())
    message = first_mail(client)
    monkeypatch.setattr(settings, 'openai_api_key', '')
    assert client.post(f"/api/inbox/{message['id']}/draft", json={}).status_code == 503
    assert client.get('/api/approvals').json() == []


def test_changed_draft_recipient_is_saved_and_requires_review_again(workflow_app):
    client, _, _, _ = workflow_app
    upload_mail(client, eml())
    message = first_mail(client)
    created = client.post(f"/api/inbox/{message['id']}/draft", json={'body': 'Тестовый ответ.'}).json()
    url = f"/api/approvals/{created['id']}"
    assert client.patch(url, json={'status': 'APPROVED'}).json()['status'] == 'APPROVED'
    changed = client.patch(url, json={'recipient': 'other@customer.example'}).json()
    assert changed['recipient'] == 'other@customer.example' and changed['status'] == 'PENDING'
    assert not changed['approved_by']
    assert client.patch(url, json={'recipient': 'bad-address'}).status_code == 422
    assert client.get('/api/approvals').json()[0]['recipient'] == 'other@customer.example'
