"""State, evidence and routing tests inject plans, not pretend live OpenAI calls."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import threading
import uuid
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from server import workflows as wf
from server.config import settings
from server.db import Base
from server.models import AppSetting, ChatMessage, Conversation, ConversationState, Document, Escalation, KnowledgeChunk
from server.orchestrator import initial_state, local_plan, public_state, respond


@pytest.fixture()
def storage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'openai_api_key', '')
    engine = create_engine('sqlite:///' + str(tmp_path / 'state.db'), connect_args={'check_same_thread': False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    engine.dispose()


def conversation(session, owner='guest:1', title='Вопрос клиента'):
    item = Conversation(id=str(uuid.uuid4()), owner_scope=owner, title=title)
    session.add(item)
    session.commit()
    return item


def chat_attachment(session, item, text, filename='photo.png'):
    """Seed the canonical owner-bound upload that the real endpoint persists."""
    identifier = uuid.uuid4().hex
    attachment = {'id': identifier, 'filename': filename, 'kind': 'image',
        'text': text, 'engine': 'ocr', 'url': f'/api/attachments/{identifier}/file',
        'received': True, 'processing_status': 'processed', 'content_processed': True,
        'analysis_kind': 'text_extraction', 'warnings': []}
    session.add(AppSetting(key='chat_attachment:' + identifier, value={**attachment,
        'owner_scope': item.owner_scope.removeprefix('playground:'), 'conversation_id': item.id}))
    session.flush()
    return attachment


def source(session, *, model='PE38-12', visibility='public', category='Документация', text='PE38-12. Размер регулировки 15 мм.'):
    item = Document(title='Руководство ' + model, filename=model + '.txt', storage_name=uuid.uuid4().hex + '.txt',
                    sha256=uuid.uuid4().hex, size_bytes=len(text), visibility=visibility, category=category,
                    status='indexed', pages=1, chunks=1, models=[model])
    session.add(item)
    session.flush()
    session.add(KnowledgeChunk(document_id=item.id, page=1, text=text, models=[model]))
    session.commit()
    return item


def plan(context, **changes):
    output = local_plan(context)
    output.update(changes)
    return output


def test_drive_model_followup_keeps_machine_and_alarm_separate(storage, monkeypatch):
    from server import ai

    turns = []

    def provider(session, context, selected_model=None):
        turns.append(deepcopy(context))
        return plan(context, response='Уточните точную модель привода.', missing_information=['component_model']), {}

    monkeypatch.setattr(ai, 'generate_plan', provider)
    with storage() as session:
        item = conversation(session)
        respond(session, 'PE38-12: как установить ноль энкодера при Er.810?', conversation=item, require_model=True)
        outcome = respond(session, 'Модель Veichi SD700', conversation=item, require_model=True)

        assert turns[-1]['current_turn']['component_model'] == 'Veichi SD700'
        assert turns[-1]['current_turn']['alarm_code'] == 'Er.810'
        assert outcome['state']['known_facts']['equipment_model'] == 'PE38-12'
        assert outcome['state']['known_facts']['component_model'] == 'Veichi SD700'
        assert outcome['state']['known_facts']['alarm_code'] == 'Er.810'
        assert not outcome['missing_information']
        assert 'Уточните точную модель привода' not in outcome['content']
        assert 'при включении' in outcome['content']
        assert 'не меняйте ноль энкодера' in outcome['content']


def test_drive_model_followup_without_readable_alarm_does_not_repeat_model_question(storage):
    with storage() as session:
        item = conversation(session)
        respond(session, 'Нужна помощь с нулём энкодера', conversation=item)
        outcome = respond(session, 'Модель Veichi SD700', conversation=item)

        assert outcome['state']['known_facts']['component_model'] == 'Veichi SD700'
        assert outcome['state']['known_facts'].get('equipment_model') is None
        assert outcome['missing_information'] == []
        assert 'Модель привода Veichi SD700 зафиксирована' in outcome['content']
        assert 'код или сообщение' in outcome['content']


def test_intermittent_drive_stop_offers_safe_observation_without_a_model(storage):
    with storage() as session:
        outcome = respond(session,
            'Периодически, привод останавливается после захвата брикетов. Как будто чего то ждёт. '
            'Пока вручную влево вправо руками не подвигать, сброс, запуск машина начинает работать. Укладка в коробку',
            conversation=conversation(session), require_model=True)

        assert outcome['engine'] == 'unavailable'
        assert 'аварийный код' in outcome['content'] and 'ожидание шага' in outcome['content']
        assert 'до сброса' in outcome['content']
        assert 'Не сдвигайте механизм руками' in outcome['content']
        assert outcome['sources'] == []


def test_drive_observation_keeps_unsafe_operation_guard(storage):
    with storage() as session:
        outcome = respond(session,
            'Привод периодически останавливается после захвата. Можно поправить его рукой на ходу?',
            conversation=conversation(session), planner=local_plan)

        assert 'Не поправляйте' in outcome['content']
        assert 'панель машины показывает ожидание' not in outcome['content']


def test_provider_answer_sources_are_used_by_grounding_and_returned(storage, monkeypatch):
    from server import ai

    web = {'source_type': 'official_web', 'title': 'Руководство привода',
           'url': 'https://example.test/drive.pdf', 'text': 'Зафиксируйте сообщение панели.'}

    def provider(session, context, selected_model=None):
        claim = {'source_index': 1, 'text': web['text'], 'quote': web['text']}
        return plan(context, action='answer', response_kind='technical',
                    response=web['text'] + ' [1]', technical_claims=[claim]), {'answer_sources': [web]}

    monkeypatch.setattr(ai, 'generate_plan', provider)
    with storage() as session:
        item = conversation(session)
        outcome = respond(session, 'Модель Veichi SD700', conversation=item, require_model=True)

        assert outcome['engine'] == 'openai'
        assert outcome['sources'] == [web]
        assert session.get(ConversationState, item.id).state['source_refs'][0]['url'] == web['url']


def test_unknown_parameter_changes_explanation_without_guess_or_first_visit(storage):
    seen = []
    def provider(context):
        seen.append(deepcopy(context))
        if context['state']['turn_count'] == 0:
            return plan(context, intent='parts_request', action='explain', response_kind='identification',
                response='Начнём с маркировки: сфотографируйте надпись на детали и общий вид узла. Совместимость проверим отдельно.',
                missing_information=['part_marking'])
        return plan(context, intent='identification', action='explain', response_kind='identification',
            response='Не требуется знать специальное название. Покажите деталь целиком и отдельно участок с буквами и цифрами; не снимайте защитные кожухи.',
            missing_information=['part_marking'])
    with storage() as session:
        item = conversation(session)
        first = respond(session, 'PE38-12: нужен ремень, не знаю его тип и параметры', conversation=item, planner=provider)
        second = respond(session, 'Я не понимаю, как это определить', conversation=item, planner=provider)
        assert first['content'] != second['content']
        assert 'маркировк' in first['content'] and 'буквами' in second['content']
        assert second['state']['goal'] == 'PE38-12: нужен ремень, не знаю его тип и параметры'
        assert seen[1]['state']['known_facts']['equipment_model'] == 'PE38-12'
        assert first['escalation'] is None and second['escalation'] is None
        assert session.scalar(select(func.count(wf.ServiceCase.id))) == 0
        assert all('выезд' not in output['content'] for output in (first, second))


def test_no_key_no_documents_keeps_organizational_state_across_restart(storage):
    with storage() as session:
        item = conversation(session)
        cid = item.id
        first = respond(session, 'Мне нужен специалист по PE38-12', conversation=item)
        assert first['engine'] == 'search_only' and first['escalation']
        assert 'дистанционной' in first['content']
        respond(session, 'Хочу согласовать выезд', conversation=item)
        respond(session, 'Адрес: Москва, ул. Ленина, дом 5', conversation=item)
    with storage() as session:
        item = session.get(Conversation, cid)
        confirmed = respond(session, 'Подтверждаю адрес', conversation=item)
        assert confirmed['state']['known_facts']['address'] == 'Москва, ул. Ленина, дом 5'
        result = respond(session, '23 октября в 14:00', conversation=item)
        state = result['state']
        assert state['known_facts']['equipment_model'] == 'PE38-12'
        assert state['known_facts']['preferred_datetime'] == '23 октября в 14:00'
        assert state['client_confirmed'] is True and state['stage'] == 'awaiting_approval'
        assert 'Выезд пока не подтверждён' in result['content']
        assert 'не найден' not in result['content']
        assert session.scalar(select(func.count(wf.ServiceCase.id))) == 1
        assert session.scalar(select(func.count(wf.InternalTask.id))) == 1
        assert session.scalar(select(func.count(Escalation.id))) == 1


def test_twenty_four_turns_keep_goal_facts_files_and_one_handoff(storage):
    observations = []
    def provider(context):
        observations.append(deepcopy(context))
        return local_plan(context)
    turns = ['PE38-12: требуется помощь с приводом', 'Как найти обозначение?', 'Фото прилагаю',
             'Не знаю, как это определить', 'Посмотрел шильдик', 'Серийный номер SN-008123',
             'Проверил надпись', 'Маркировка стёрта', 'Не помогло', 'Мне нужен специалист',
             'Я хочу дистанционную консультацию', 'ООО «Фабрика»', 'Email: client@example.test',
             'Могу прислать общий вид', 'Дополнительный снимок прилагаю', 'Жду уточнения',
             'Нужен также выезд', 'Адрес: Казань, ул. Складская, дом 10', 'Подтверждаю адрес',
             '24 октября в 10:30', 'Уточнение по времени: 24 октября в 11:00',
             'Фото маркировки отправлено', 'Контактная почта прежняя', 'Какие сведения ещё нужны?']
    with storage() as session:
        item = conversation(session)
        cid = item.id
    for index, message in enumerate(turns):
        # New SQLAlchemy session on every turn simulates worker/restart boundaries.
        with storage() as session:
            item = session.get(Conversation, cid)
            user_message = ChatMessage(conversation_id=cid, role='user', content=message, sources=[])
            session.add(user_message)
            session.flush()
            attachments = [chat_attachment(session, item, 'PE38-12 SN-008123', 'plate.png')] if index == 2 else []
            if attachments:
                image_id = attachments[0]['id']
                session.add(AppSetting(key='chat_message_attachments:' + str(user_message.id),
                    value={'attachments': attachments}))
            session.commit()
            result = respond(session, message, conversation=item, planner=provider, attachments=attachments)
            session.add(ChatMessage(conversation_id=cid, role='assistant', content=result['content'], sources=result['sources']))
            session.commit()
    assert result['state']['turn_count'] == 24
    assert result['state']['goal'] == turns[0]
    assert result['state']['known_facts']['serial_number'] == 'SN-008123'
    assert result['state']['known_facts']['address'] == 'Казань, ул. Складская, дом 10'
    assert '11:00' in result['state']['known_facts']['preferred_datetime']
    assert observations[-1]['state']['known_facts']['email'] == 'client@example.test'
    assert observations[-1]['attachments'][0]['id'] == image_id
    with storage() as session:
        state = session.get(ConversationState, cid).state
        assert len(state['attachments']) == 1 and 'text' not in state['attachments'][0]
        assert session.scalar(select(func.count(wf.ServiceCase.id))) == 1
        assert session.scalar(select(func.count(wf.InternalTask.id))) == 1
        assert session.scalar(select(func.count(ChatMessage.id))) == 48


def test_evidence_and_fact_gates_reject_invented_specs_and_booking(storage):
    with storage() as session:
        item = conversation(session)
        source(session)
        def malicious(context):
            return plan(context, response_kind='technical', response='Замените деталь на 999 мм',
                technical_claims=[{'text': '999 мм', 'source_index': 1, 'quote': '999 мм'}],
                facts=[{'key': 'address', 'value': 'Москва', 'quote': 'Клиент сообщил Москва'}])
        output = respond(session, 'PE38-12: какая регулировка?', conversation=item, planner=malicious)
        assert '999' not in output['content'] and output['sources'] == []
        assert 'address' not in output['state']['known_facts']
        assert any(t['step'] == 'safety' and t['status'] == 'blocked' for t in output['trace'])
        def fabricated_booking(context):
            return plan(context, response_kind='organizational', action='answer', response='Выезд подтверждён, специалист точно приедет завтра',
                        requires_human=False, requested_action='none', technical_claims=[])
        output = respond(session, 'Что дальше?', conversation=item, planner=fabricated_booking)
        assert 'точно приедет' not in output['content'] and output['escalation'] is None


def test_exact_claim_is_cited_and_unverified_history_cannot_support_it(storage):
    with storage() as session:
        item = conversation(session)
        source(session)
        def grounded(context):
            return plan(context, response_kind='technical', action='answer', response='',
                technical_claims=[{'text': 'Размер регулировки 15 мм.', 'quote': 'Размер регулировки 15 мм.', 'source_index': 1}])
        output = respond(session, 'PE38-12: размер регулировки?', conversation=item, planner=grounded)
        assert '15 мм' in output['content'] and '[1]' in output['content'] and output['sources']
        for document in session.scalars(select(Document)):
            document.category = 'История переписки (непроверено)'
        session.commit()
        output = respond(session, 'PE38-12: размер регулировки?', conversation=item, planner=grounded, include_internal=True)
        assert '15 мм' not in output['content'] and output['sources'] == []


def test_onsite_plan_without_customer_request_becomes_remote_only(storage):
    with storage() as session:
        item = conversation(session)
        def premature(context):
            return plan(context, requested_action='onsite_visit', requires_human=True,
                response_kind='organizational', action='escalate', response='Направляю инженера на осмотр')
        output = respond(session, 'PE38-12: не могу определить маркировку', conversation=item, planner=premature)
        assert output['escalation']['requested_action'] == 'remote_consultation'
        assert 'обязательный выезд' in output['content']
        assert session.scalar(select(wf.InternalTask)).title.startswith('Ответить клиенту')


def test_playground_never_creates_cases_or_tasks(storage):
    with storage() as session:
        output = respond(session, 'Мне нужен специалист, хочу выезд', planner=lambda c: local_plan(c))
        assert output['escalation'] is None and 'не создана' in output['content']
        assert session.scalar(select(func.count(wf.ServiceCase.id))) == 0
        assert session.scalar(select(func.count(ConversationState.conversation_id))) == 0


def test_success_creates_review_candidate_once_and_thanks_is_not_training(storage):
    with storage() as session:
        item = conversation(session)
        respond(session, 'PE38-12: пропала подача', conversation=item)
        respond(session, 'Спасибо', conversation=item)
        assert session.scalar(select(func.count(wf.VerifiedSolution.id))) == 0
        statement = 'Проверил положение датчика по руководству, всё заработало'
        def successful(context):
            return plan(context, action='propose_solution_candidate', response_kind='conversation',
                        response='Результат сохранён для проверки.', attempted_actions=[statement])
        result = respond(session, statement, conversation=item, planner=successful)
        respond(session, statement, conversation=item, planner=successful)
        candidates = session.scalars(select(wf.VerifiedSolution)).all()
        assert len(candidates) == 1
        assert candidates[0].status == 'CANDIDATE' and candidates[0].visibility == 'internal'
        assert candidates[0].verified_by is None and candidates[0].customer_confirmed is True
        assert wf.search_verified_solutions(session, 'датчика', public_only=False) == []


def test_parallel_distinct_clients_do_not_share_state(storage):
    with storage() as session:
        a = conversation(session, 'guest:1')
        b = conversation(session, 'guest:2')
        ids = [a.id, b.id]
    barrier = threading.Barrier(2)
    def worker(index):
        with storage() as session:
            item = session.get(Conversation, ids[index])
            barrier.wait()
            return respond(session, f'Мне нужен специалист по {"PE38-12" if index == 0 else "AZ01"}', conversation=item)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outputs = list(pool.map(worker, [0, 1]))
    assert outputs[0]['escalation']['id'] != outputs[1]['escalation']['id']
    assert outputs[0]['state']['known_facts']['equipment_model'] == 'PE38-12'
    assert outputs[1]['state']['known_facts']['equipment_model'] == 'AZ01'


def test_concurrent_same_conversation_preserves_turn_count(storage):
    with storage() as session:
        item = conversation(session)
        cid = item.id
    def worker(index):
        with storage() as session:
            return respond(session, f'Уточнение {index}', conversation=session.get(Conversation, cid), planner=lambda c: local_plan(c))
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(worker, range(8)))
    with storage() as session:
        assert session.get(ConversationState, cid).state['turn_count'] == 8


def test_attachment_claims_remain_unconfirmed_and_bounded(storage):
    captured = []
    def provider(context):
        captured.append(context)
        return plan(context, response_kind='identification', technical_claims=[], action='request_information',
                    response='На снимке распознана возможная модель. Подтвердите обозначение по шильдику.',
                    unconfirmed_facts=[{'key': 'equipment_model', 'value': 'AZ01', 'quote': 'AZ01'}])
    with storage() as session:
        item = conversation(session)
        attachments = [chat_attachment(session, item, 'AZ01 ' + 'x' * 10000) for _ in range(12)]
        session.commit()
        output = respond(session, 'Вот фотография', conversation=item, planner=provider,
            attachments=attachments)
        assert len(captured[0]['attachments']) == 12
        assert sum(len(a['text']) for a in captured[0]['attachments']) == 12000
        assert output['state']['known_facts'].get('equipment_model') is None
        assert output['state']['unconfirmed_facts']['equipment_model'] == 'AZ01'
        stored = session.get(ConversationState, item.id).state
        assert all('text' not in a for a in stored['attachments'])
        assert all('owner_scope' not in a and 'storage_name' not in a for a in stored['attachments'])


def test_resolved_handoff_does_not_reopen_on_thanks(storage):
    with storage() as session:
        item = conversation(session)
        output = respond(session, 'Мне нужен специалист', conversation=item)
        escalation = session.get(Escalation, output['escalation']['id'])
        escalation.status = 'resolved'
        from server.escalations import _synchronize_workflow
        _synchronize_workflow(session, escalation)
        session.commit()
        result = respond(session, 'Спасибо', conversation=item)
        assert result['escalation'] is None and escalation.status == 'resolved'
        assert result['state']['requires_human'] is False
        assert result['state']['stage'] == 'diagnosing'
        assert not set(result['state']['missing_information']).intersection({'address', 'preferred_datetime'})


@pytest.mark.parametrize('status', ['resolved', 'cancelled'])
@pytest.mark.parametrize('message', ['Сообщение получил', 'Добрый день', 'Информация принята'])
def test_terminal_handoff_retires_old_form_and_failure_counter(storage, status, message):
    with storage() as session:
        item = conversation(session)
        first = respond(session, 'Мне нужен выезд специалиста по PE38-12', conversation=item)
        escalation = session.get(Escalation, first['escalation']['id'])
        escalation.status = status
        from server.escalations import _synchronize_workflow
        _synchronize_workflow(session, escalation)
        row = session.get(ConversationState, item.id)
        row.state = {**row.state, 'failed_attempts': 3}
        session.commit()
        result = respond(session, message, conversation=item)
        assert result['escalation'] is None and escalation.status == status
        assert result['state']['requested_action'] == 'none'
        assert result['state']['stage'] == 'diagnosing'
        assert result['state']['missing_information'] == []
        assert result['state']['known_facts']['equipment_model'] == 'PE38-12'
        assert result['state']['goal'] == first['state']['goal']
        assert 'фото' not in result['content'].lower()
        assert session.scalar(select(func.count(wf.ServiceCase.id))) == 1
        renewed = respond(session, 'Теперь нужен специалист для дистанционной консультации', conversation=item)
        assert renewed['escalation']['id'] == escalation.id
        assert renewed['state']['requires_human'] is True
        assert renewed['state']['requested_action'] == 'remote_consultation'
        assert session.scalar(select(func.count(wf.ServiceCase.id))) == 1


def test_playground_persisted_state_blocks_actions_and_success_candidates(storage):
    with storage() as session:
        item = conversation(session, 'playground:user:1')
        first = respond(session, 'Мне нужен специалист по PE38-12', conversation=item, allow_actions=False)
        assert first['state']['turn_count'] == 1 and first['escalation'] is None
        statement = 'Проверил датчик, всё заработало'
        def fake(context):
            return plan(context, attempted_actions=[statement], action='propose_solution_candidate', requires_human=False,
                        requested_action='none', response_kind='conversation', response='Сохранено для теста')
        respond(session, statement, conversation=item, allow_actions=False, planner=fake)
        assert session.scalar(select(func.count(wf.ServiceCase.id))) == 0
        assert session.scalar(select(func.count(wf.VerifiedSolution.id))) == 0
        assert session.get(ConversationState, item.id).state['turn_count'] == 2


def test_combined_model_table_requires_exact_column_provenance(storage):
    with storage() as session:
        item = conversation(session)
        document = source(session, model='PE38-17', text='PE38-17 PE38-25 Размеры: 15 мм и 25 мм')
        document.models = ['PE38-17', 'PE38-25']
        chunk = session.scalar(select(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id))
        chunk.models = ['PE38-17', 'PE38-25']
        session.commit()
        def uncertain(context):
            return plan(context, response_kind='technical', technical_claims=[
                {'text': '15 мм', 'quote': 'Размеры: 15 мм и 25 мм', 'source_index': 1}], action='answer')
        output = respond(session, 'PE38-17: какие размеры?', conversation=item, planner=uncertain)
        assert '15 мм' not in output['content']
        assert 'столбцом' in output['content'] and output['sources'][0]['document_id'] == document.id
        assert output['sources'][0]['text'] == ''


def test_knowledge_toggle_prevents_retrieval_but_keeps_memory(storage):
    with storage() as session:
        item = conversation(session)
        source(session)
        output = respond(session, 'PE38-12: размер регулировки?', conversation=item, use_knowledge=False)
        assert output['sources'] == [] and '15 мм' not in output['content']
        assert output['state']['known_facts']['equipment_model'] == 'PE38-12'
        output = respond(session, 'Размер регулировки?', conversation=item, use_knowledge=True)
        assert '15 мм' in output['content'] and output['sources']


@pytest.mark.parametrize('provenance', ['source_kind', 'import_registry'])
def test_renamed_history_stays_untrusted_from_immutable_import_origin(storage, provenance):
    with storage() as session:
        item = conversation(session)
        history = source(session, visibility='internal', category='Руководство',
                         text='PE38-12. Размер регулировки 999 мм.')
        if provenance == 'source_kind':
            history.indexing_details = {'source_kind': 'unreviewed_history', 'stage': 'complete'}
        else:
            session.add(AppSetting(key='history_import:' + 'a' * 64, value={'document_id': history.id}))
        session.commit()
        received = []
        def provider(context):
            received.extend(context['sources'])
            return plan(context, response_kind='technical', technical_claims=[
                {'text': '999 мм.', 'quote': 'Размер регулировки 999 мм.', 'source_index': 1}])
        result = respond(session, 'PE38-12: размер регулировки?', conversation=item,
                         include_internal=True, planner=provider)
        assert received == [] and result['sources'] == []
        assert '999' not in result['content']


@pytest.mark.parametrize('failure_stage', ['availability', 'inference'])
def test_first_turn_provider_failure_preserves_state_for_new_session(storage, monkeypatch, failure_stage):
    import openai
    from server.models import AIUsage
    monkeypatch.setattr(settings, 'openai_api_key', 'test-server-only-key')
    class FailedProvider:
        def __init__(self, **kwargs):
            self.models = self.responses = self
        def retrieve(self, model):
            if failure_stage == 'availability':
                raise ConnectionError('simulated network failure')
        def create(self, **kwargs):
            raise ConnectionError('simulated network failure')
        def close(self):
            pass
    monkeypatch.setattr(openai, 'OpenAI', FailedProvider)
    with storage() as session:
        item = conversation(session)
        cid = item.id
        first = respond(session, 'PE38-12: вопрос о приводе', conversation=item)
        assert first['engine'] == 'search_only' and first['ai_error']
    with storage() as session:
        row = session.get(ConversationState, cid)
        assert row is not None
        assert row.state['turn_count'] == 1 and row.state['known_facts']['equipment_model'] == 'PE38-12'
        assert session.scalar(select(func.count(AIUsage.id))) == 0
        second = respond(session, 'Адрес: улица Тестовая, дом 5', conversation=session.get(Conversation, cid))
        assert second['state']['turn_count'] == 2
        assert second['state']['known_facts']['equipment_model'] == 'PE38-12'
        assert 'Тестовая' in second['state']['known_facts']['address']


def test_waiting_provider_releases_first_turn_sqlite_write_lock(storage):
    entered, release = threading.Event(), threading.Event()
    with storage() as session:
        first_id = conversation(session, 'guest:slow').id
        second_id = conversation(session, 'guest:other').id
    def slow_provider(context):
        entered.set()
        assert release.wait(10), 'Test failed to release the simulated provider'
        return local_plan(context)
    def worker(cid, message, provider):
        with storage() as session:
            return respond(session, message, conversation=session.get(Conversation, cid), planner=provider)
    with ThreadPoolExecutor(max_workers=2) as pool:
        slow = pool.submit(worker, first_id, 'PE38-12: вопрос о подаче', slow_provider)
        try:
            assert entered.wait(2)
            other = pool.submit(worker, second_id, 'AZ01: вопрос по упаковке', local_plan)
            result = other.result(timeout=2)
            assert result['state']['known_facts']['equipment_model'] == 'AZ01'
            assert not slow.done(), 'The other client must save while the first provider is still waiting'
        finally:
            release.set()
        assert slow.result(timeout=2)['state']['known_facts']['equipment_model'] == 'PE38-12'
    with storage() as session:
        assert session.get(ConversationState, first_id).state['turn_count'] == 1
        assert session.get(ConversationState, second_id).state['turn_count'] == 1


def test_external_state_change_during_provider_raises_conflict_without_actions(storage):
    from sqlalchemy.orm.exc import StaleDataError
    with storage() as session:
        item = conversation(session)
        cid = item.id
        def concurrently_updated(context):
            with storage() as staff_session:
                row = staff_session.get(ConversationState, cid)
                assert row is not None, 'The initial snapshot must be durable before calling the provider'
                row.state = {**row.state, 'known_facts': {'equipment_model': 'AZ01'}, 'turn_count': 9}
                staff_session.commit()
            return plan(context, requested_action='remote_consultation', requires_human=True,
                        response_kind='organizational', action='escalate')
        with pytest.raises(StaleDataError):
            respond(session, 'Мне нужен специалист по PE38-12', conversation=item, planner=concurrently_updated)
        session.rollback()
    with storage() as session:
        row = session.get(ConversationState, cid)
        assert row.state['turn_count'] == 9 and row.state['known_facts']['equipment_model'] == 'AZ01'
        assert session.scalar(select(func.count(Escalation.id))) == 0
        assert session.scalar(select(func.count(wf.ServiceCase.id))) == 0
        assert session.scalar(select(func.count(wf.InternalTask.id))) == 0


@pytest.mark.parametrize('message', [
    'Мне не нужен специалист, выезд не нужен',
    'Не приглашайте инженера, хочу разобраться сам',
    'Специалист пока не нужен',
    'Я не хочу выезда',
    'Выезд нам сейчас не нужен',
    'Хочу отменить выезд',
])
def test_explicit_service_decline_overrules_model_action_without_new_handoff(storage, message):
    def mistaken(context):
        return plan(context, intent='onsite_visit', requested_action='onsite_visit', requires_human=True,
                    response_kind='organizational', response='Мастер будет у вас завтра', action='escalate')
    with storage() as session:
        item = conversation(session)
        result = respond(session, message, conversation=item, planner=mistaken)
        assert result['escalation'] is None and result['state']['requested_action'] == 'none'
        assert result['state']['requires_human'] is False
        assert 'Мастер будет' not in result['content']
        assert result['engine'] == 'search_only' and result['planning_mode'] == 'injected'
        assert session.scalar(select(func.count(wf.ServiceCase.id))) == 0
        assert session.scalar(select(func.count(wf.InternalTask.id))) == 0


def test_remote_consultation_with_declined_visit_is_not_misread_as_declining_human(storage):
    with storage() as session:
        item = conversation(session)
        result = respond(session, 'Мне нужен специалист без выезда', conversation=item)
        assert result['escalation']['requested_action'] == 'remote_consultation'
        assert result['escalation']['status'] == 'queued'
        assert result['state']['requires_human'] is True


@pytest.mark.parametrize('kind', ['organizational', 'conversation'])
def test_scheduling_reply_is_composed_from_real_state_without_model_booking_or_cost_claim(storage, kind):
    def invented(context):
        return plan(context, intent='scheduling', response_kind=kind, requested_action='none',
                    requires_human=False, action='answer',
                    response='Встреча назначена на завтра, мастер будет у вас в 14:00. Стоимость 5000 рублей. Письмо я отправил.')
    with storage() as session:
        item = conversation(session)
        result = respond(session, 'Как продолжить обсуждение?', conversation=item, planner=invented)
        assert result['escalation'] is None
        assert all(text not in result['content'] for text in ('5000', '14:00', 'назначена', 'отправил', 'мастер будет'))
        assert 'согласование' in result['content']
        assert any(step['step'] == 'composition' and step['status'] == 'server_composed' for step in result['trace'])
        assert session.scalar(select(func.count(wf.ServiceCase.id))) == 0


def test_customer_withdrawal_cancels_existing_handoff_without_creating_another(storage):
    with storage() as session:
        item = conversation(session)
        first = respond(session, 'Мне нужен специалист по PE38-12', conversation=item)
        original_id = first['escalation']['id']
        declined = respond(session, 'Специалист больше не нужен, я разберусь сам', conversation=item)
        assert declined['escalation'] is None and declined['state']['requires_human'] is False
        escalation = session.get(Escalation, original_id)
        assert escalation.status == 'cancelled'
        assert session.get(wf.InternalTask, escalation.task_id).status == 'cancelled'
        assert 'отменён' in declined['content']
        assert session.scalar(select(func.count(Escalation.id))) == 1
        assert session.scalar(select(func.count(wf.ServiceCase.id))) == 1


def test_customer_can_change_onsite_request_to_remote_in_same_case(storage):
    with storage() as session:
        item = conversation(session)
        first = respond(session, 'Хочу согласовать выезд по PE38-12', conversation=item)
        changed = respond(session, 'Выезд не нужен, помогите дистанционно', conversation=item)
        assert changed['escalation']['id'] == first['escalation']['id']
        assert changed['escalation']['requested_action'] == 'remote_consultation'
        assert session.scalar(select(func.count(wf.ServiceCase.id))) == 1


@pytest.mark.parametrize('message', ['Приезжайте на площадку', 'Нужен выезд', 'Я не против выезда', 'Не хочу дистанционную консультацию, хочу выезд'])
def test_explicit_positive_visit_wish_is_still_recorded_for_approval(storage, message):
    with storage() as session:
        result = respond(session, message, conversation=conversation(session))
        assert result['escalation']['requested_action'] == 'onsite_visit'
        assert result['escalation']['status'] == 'waiting_approval'
        assert 'не подтверждён' in result['content']
