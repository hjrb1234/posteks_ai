"""Real HTTP/application path, recorded SDK STUB: not live OpenAI quality tests."""
from copy import deepcopy
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from test_core import env, login, upload
from scripts.dialogue_diagnostics import BASIC_TURNS
from server import main
from server.config import settings
from server.models import AppSetting, ChatMessage, ConversationState
from server.orchestrator import local_plan
from test_media_memory import video_bytes


@pytest.fixture
def strict(env, monkeypatch):
    # Other legacy workflow tests deliberately opt into a reference-only local
    # planner. These tests must exercise the real production model requirement.
    monkeypatch.setattr(settings, 'reference_test_mode', False)
    monkeypatch.setattr(settings, 'openai_api_key', 'TEST_NOT_REAL_KEY')
    return env


def provider(monkeypatch, output):
    import openai
    calls = []
    class Client:
        def __init__(self, **kwargs):
            self.models = SimpleNamespace(retrieve=lambda model: SimpleNamespace(id=model))
            self.responses = self
        def create(self, **kwargs):
            context = json.loads(kwargs['input'])
            calls.append({'request': deepcopy(kwargs), 'context': deepcopy(context)})
            result = output(context, len(calls))
            return SimpleNamespace(id='STUB-response-' + str(len(calls)), status='completed',
                output_text=json.dumps(result, ensure_ascii=False),
                usage=SimpleNamespace(input_tokens=1, output_tokens=1))
        def close(self):
            pass
    monkeypatch.setattr(openai, 'OpenAI', Client)
    return calls


def natural(context, text, *, technical=False, quote=None):
    # local_plan supplies the shape of the required JSON fixture only. The
    # response under test is unique model text; production must preserve it.
    result = local_plan(context)
    result.update(response=text, action='answer', response_kind='technical' if technical else 'conversation',
        requires_human=False, requested_action='none', missing_information=[], technical_claims=[],
        attempted_actions=[], safety={'stop_remote': False, 'reason': ''})
    if technical:
        result['technical_claims'] = [{'source_index': 1, 'quote': quote, 'text': quote}]
    return result


def post(client, text, cid=None):
    response = client.post('/api/chat', json={'message': text, **({'conversation_id': cid} if cid else {})})
    assert response.status_code == 200, response.text
    return response.json()


def approve_material(client, text, filename='PE38-12.txt'):
    login(client)
    response = upload(client, filename, text)
    assert response.status_code == 200, response.text
    identifier = response.json()[0]['id']
    result = client.patch(f'/api/documents/{identifier}', json={'answer_access': 'client_answers'})
    assert result.status_code == 200, result.text
    client.post('/api/auth/logout')
    client.get('/api/auth/me')
    return identifier


def test_five_turns_send_actual_new_text_and_history_and_preserve_natural_response(strict, monkeypatch):
    replies = ['Добрый день! Чем могу помочь?', 'Давайте разберёмся. Что происходит с машиной?',
               'Начнём с модели и описания поведения машины.',
               'Проще: напишите название машины и что вы заметили.',
               'Модель нужна, чтобы выбрать подходящее руководство, а описание — чтобы понять симптом.']
    calls = provider(monkeypatch, lambda context, index: natural(context, replies[index - 1]))
    strict.client.get('/api/auth/me')
    cid = None
    for index, message in enumerate(BASIC_TURNS):
        result = post(strict.client, message, cid)
        cid = result['conversation_id']
        assert result['engine'] == 'openai'
        assert result['message']['content'] == replies[index]
        assert result['provider_response_id'] == 'STUB-response-' + str(index + 1)
        context = calls[index]['context']
        assert context['message'] == message
        assert [row['role'] for row in context['recent_history']] == ['user', 'assistant'] * index
        assert [row['content'] for row in context['recent_history'] if row['role'] == 'user'] == list(BASIC_TURNS[:index])
        assert [row['content'] for row in context['recent_history'] if row['role'] == 'assistant'] == replies[:index]
        assert calls[index]['request']['model'] == settings.openai_model
        assert calls[index]['request']['store'] is False
    history = strict.client.get('/api/conversations/' + cid).json()['messages']
    assert len(history) == 10
    assert [row['content'] for row in history if row['role'] == 'assistant'] == replies
    assert all(row.get('engine') == 'openai' for row in history if row['role'] == 'assistant')


def test_separate_owned_threads_never_receive_other_thread_history_or_cached_response(strict, monkeypatch):
    calls = provider(monkeypatch, lambda context, index: natural(context, 'Ответ только на: ' + context['message']))
    first = post(strict.client, 'PE38-12: TEST-ALPHA вопрос о подаче')
    second = post(strict.client, 'AZ01: TEST-BETA вопрос о ленте')
    again = post(strict.client, 'TEST-ALPHA продолжение', first['conversation_id'])
    assert first['conversation_id'] != second['conversation_id']
    assert calls[1]['context']['recent_history'] == []
    assert 'TEST-BETA' not in json.dumps(calls[2]['context']['recent_history'])
    assert again['message']['content'] == 'Ответ только на: TEST-ALPHA продолжение'
    with TestClient(main.app) as stranger:
        assert stranger.get('/api/conversations/' + first['conversation_id']).status_code == 404
        assert stranger.post('/api/chat', json={'message': 'Прочитай', 'conversation_id': first['conversation_id']}).status_code == 404
    assert len(calls) == 3


def test_actual_upload_found_and_sent_to_model_with_checks_preserved_on_followup(strict, monkeypatch):
    quote = 'Проверка датчика выполняется только после отключения питания.'
    identifier = approve_material(strict.client, 'PE38-12\nКонвейер подачи. ' + quote)
    checked = 'При отключённом питании проверили разъём датчика: он закреплён.'
    first_text = 'Формирователь РЕ38-12. Конвейер не подаёт короб. ' + checked
    responses = ['Вы уже проверили разъём. В руководстве описана проверка датчика после отключения питания [1]. Какие сообщения показывает панель?',
                 'Модель PE38-12 и результат проверки разъёма сохранены. Теперь уточняю только сообщение на панели.']
    def output(context, index):
        result = natural(context, responses[index - 1], technical=index == 1, quote=quote if index == 1 else None)
        if index == 1:
            result['attempted_actions'] = [checked]
        return result
    calls = provider(monkeypatch, output)
    first = post(strict.client, first_text)
    assert first['engine'] == 'openai', first
    assert first['message']['content'] == responses[0]
    assert any(source['document_id'] == identifier for source in calls[0]['context']['sources'])
    assert quote in json.dumps(calls[0]['context']['sources'], ensure_ascii=False)
    assert first['message']['sources'][0]['document_id'] == identifier
    assert first['message']['sources'][0]['original_private'] is True
    assert strict.client.get(f'/api/documents/{identifier}/file').status_code == 404
    second = post(strict.client, 'Эту проверку уже сделал. Объясни, что нужно дальше.', first['conversation_id'])
    assert second['message']['content'] == responses[1], second
    assert calls[1]['context']['state']['known_facts']['equipment_model'] == 'PE38-12'
    assert checked in calls[1]['context']['state']['attempted_actions']
    assert calls[1]['context']['recent_history'][0]['content'] == first_text
    with strict.factory() as session:
        assert checked in session.get(ConversationState, first['conversation_id']).state['attempted_actions']


def test_missing_model_key_with_real_index_does_not_forge_technical_answer(strict, monkeypatch):
    identifier = approve_material(strict.client, 'PE38-12\nРегулировка подачи: PRIVATE_TEST_FACT_SHOULD_NOT_BE_A_FALLBACK.')
    monkeypatch.setattr(settings, 'openai_api_key', '')
    def forbidden(*args, **kwargs):
        pytest.fail('OpenAI client must not be constructed without a key')
    import openai
    monkeypatch.setattr(openai, 'OpenAI', forbidden)
    result = post(strict.client, 'PE38-12: как отрегулировать подачу?')
    assert result['engine'] == 'unavailable'
    assert result['error']['code'] == 'missing_openai_key'
    assert result['provider_diagnostics']['provider_called'] is False
    assert result['message']['sources'] == []
    assert 'PRIVATE_TEST_FACT' not in result['message']['content']
    assert 'не подменяю' in result['message']['content']
    history = strict.client.get('/api/conversations/' + result['conversation_id']).json()['messages']
    assert history[-1]['engine'] == 'unavailable'
    assert history[-1]['error']['code'] == 'missing_openai_key'


def test_provider_api_exception_is_explicit_and_does_not_return_source_text(strict, monkeypatch):
    approve_material(strict.client, 'PE38-12\nРегулировка подачи: PRIVATE_TEST_FACT_SHOULD_NOT_BE_A_FALLBACK.')
    def failed(context, index):
        raise RuntimeError('PRIVATE_PROVIDER_EXCEPTION_MUST_NOT_LEAK')
    calls = provider(monkeypatch, failed)
    result = post(strict.client, 'PE38-12: как отрегулировать подачу?')
    assert len(calls) == 1
    assert result['engine'] == 'unavailable' and result['error']['code'] == 'provider_error'
    assert 'PRIVATE_' not in json.dumps(result)
    assert result['message']['sources'] == []
    assert result['provider_response_id'] is None


def test_conflicting_sources_cannot_be_silently_selected_by_provider(strict, monkeypatch):
    approve_material(strict.client, 'PE38-12\nДавление: 2 бар\nКонвейер подачи.', 'PE38-12-version-A.txt')
    approve_material(strict.client, 'PE38-12\nДавление: 3 бар\nКонвейер подачи.', 'PE38-12-version-B.txt')
    def blindly_choose(context, index):
        assert context['source_conflicts']
        source = context['sources'][0]
        quote = next(line for line in source['text'].splitlines() if line.startswith('Давление:'))
        return natural(context, 'Для конвейера используйте ' + quote + ' [1].', technical=True, quote=quote)
    provider(monkeypatch, blindly_choose)
    result = post(strict.client, 'PE38-12: какое давление для конвейера подачи?')
    assert result['engine'] == 'unavailable'
    assert result['error']['code'] == 'conflicting_sources'
    assert result['source_conflicts'] and result['message']['sources'] == []


def test_verified_case_is_available_without_exposing_raw_correspondence(strict, monkeypatch):
    from server.workflows import VerifiedSolution
    action = 'Проверили крепление разъёма при отключённом питании.'
    with strict.factory() as session:
        session.add(VerifiedSolution(title='PE38-12: подтверждённый случай остановки подачи', equipment_model='PE38-12',
            symptoms='Конвейер не подаёт короб', actions=action, limitations='Только аналогичный симптом после проверки модели.',
            source='PRIVATE_RAW_EMAIL_OR_CUSTOMER_MUST_NOT_LEAK', status='VERIFIED', visibility='public',
            customer_confirmed=True, verified_by='private_employee_name'))
        session.commit()
    calls = provider(monkeypatch, lambda context, index: natural(context,
        'В подтверждённом случае помогла проверка крепления разъёма [1]. Это не подтверждает причину на вашей машине.',
        technical=True, quote=action))
    result = post(strict.client, 'PE38-12: конвейер не подаёт короб, есть подтверждённый случай?')
    assert result['engine'] == 'openai', result
    assert calls[0]['context']['sources'][0]['source_type'] == 'verified_solution'
    assert result['message']['sources'][0]['solution_id']
    assert 'PRIVATE_RAW_' not in json.dumps(calls)
    assert 'private_employee_name' not in json.dumps(result)


def test_received_video_failure_is_remembered_in_next_model_request(strict, monkeypatch, video_bytes):
    from server import media
    monkeypatch.setattr(media, '_video_probe', lambda _: (None, 'video_probe_failed', 'TEST: обработка видео не удалась.'))
    uploaded = strict.client.post('/api/attachments', files={'file': ('machine.mp4', video_bytes, 'video/mp4')})
    assert uploaded.status_code == 200, uploaded.text
    video = uploaded.json()
    assert video['processing_status'] == 'failed' and not video['content_processed']
    calls = provider(monkeypatch, lambda context, index: natural(context,
        'Видео получено, но обработать его не удалось. Модель PE38-12 сохранена; повторно файл не запрашиваю.'))
    first = strict.client.post('/api/chat', json={'message': 'Модель PE38-12, видео прилагаю. Разъём проверили.',
        'attachment_ids': [video['id']]}).json()
    assert first['engine'] == 'openai', first
    second = post(strict.client, 'Что уже известно?', first['conversation_id'])
    attachment = calls[1]['context']['attachments'][0]
    assert attachment['id'] == video['id'] and attachment['processing_status'] == 'failed'
    assert attachment['content_processed'] is False and attachment['text'] == ''
    assert calls[1]['context']['state']['known_facts']['equipment_model'] == 'PE38-12'
    assert 'повторно файл не запрашиваю' in second['message']['content']


def test_no_source_cannot_be_replaced_with_invented_parameter(strict, monkeypatch):
    provider(monkeypatch, lambda context, index: natural(context, 'Установите зазор 999 мм.'))
    result = post(strict.client, 'PE38-12: какой зазор выставить?')
    assert result['engine'] == 'unavailable' and result['error']['code'] == 'missing_evidence'
    assert '999' not in result['message']['content'] and result['message']['sources'] == []


def test_invoice_request_does_not_authorize_claiming_a_send(strict, monkeypatch):
    from sqlalchemy import func
    from server.workflows import Approval, InternalTask
    provider(monkeypatch, lambda context, index: natural(context, 'Счёт уже отправлен.'))
    result = post(strict.client, 'Пришлите счёт на запчасть.')
    assert result['engine'] == 'unavailable' and result['error']['code'] == 'unperformed_action'
    with strict.factory() as session:
        assert session.scalar(select(func.count()).select_from(Approval)) == 0
        assert session.scalar(select(func.count()).select_from(InternalTask)) == 0
