"""Fresh requests override stale model plans without erasing previous cases."""
from copy import deepcopy
import pytest
from sqlalchemy import func, select

from test_orchestrator import storage, conversation, source, plan
from server import orchestrator as dialogue, workflows
from server.models import ConversationState, Escalation


def test_invoice_after_parts_cannot_retrieve_or_repeat_old_topic_or_handoff(storage, monkeypatch):
    with storage() as session:
        chat = conversation(session)
        first = dialogue.respond(session, 'Мне нужен специалист по PE38-25: проблема с ремнём', conversation=chat)
        escalation = session.get(Escalation, first['escalation']['id'])
        old_case = session.get(workflows.ServiceCase, escalation.case_id)
        before = {'summary': escalation.summary, 'status': escalation.status, 'requested_action': escalation.requested_action,
                  'case_title': old_case.title, 'case_model': old_case.equipment_model}
        row = session.get(ConversationState, chat.id)
        state = deepcopy(row.state)
        state['known_facts'].update(part_marking='HTD1550', company='ООО «Тест»')
        state['failed_attempts'] = 3
        row.state = state
        session.commit()
        def no_old_manual(*args, **kwargs):
            pytest.fail('An invoice question must not retrieve an old equipment manual')
        monkeypatch.setattr(dialogue, '_retrieve', no_old_manual)
        seen = []
        def broken_model(context):
            seen.append(deepcopy(context))
            return plan(context, intent='parts_request', response_kind='technical',
                        response='Сообщите тип, материал и цвет ремня HTD1550.',
                        requires_human=True, requested_action='onsite_visit', action='escalate',
                        safety={'stop_remote': True, 'reason': 'Invented visit'},
                        technical_claims=[{'text': 'HTD1550', 'quote': 'HTD1550', 'source_index': 1}])
        result = dialogue.respond(session, 'Счёт №333 актуален? На 26.10', conversation=chat,
                                  equipment_model='PE38-25', planner=broken_model,
                                  history=[{'role': 'assistant', 'content': 'Сообщите параметры ремня HTD1550'}])
        assert 'счёте №333' in result['content']
        assert 'не могу' in result['content']
        assert all(word not in result['content'].casefold() for word in ('ремн', 'выезд', 'встреч'))
        assert result['sources'] == [] and result['escalation'] is None
        assert result['state']['goal'] == 'Счёт №333 актуален? На 26.10'
        assert result['state']['known_facts'] == {'company': 'ООО «Тест»'}
        assert result['state']['requested_action'] == 'none' and not result['state']['requires_human']
        assert seen[0]['sources'] == [] and seen[0]['recent_history'] == []
        assert 'past_topics' not in seen[0]['state']
        archived = session.get(ConversationState, chat.id).state['past_topics'][-1]
        assert archived['known_facts']['part_marking'] == 'HTD1550'
        assert archived['escalation_id'] == escalation.id
        assert before == {'summary': escalation.summary, 'status': escalation.status, 'requested_action': escalation.requested_action,
                          'case_title': old_case.title, 'case_model': old_case.equipment_model}
        assert session.scalar(select(func.count(workflows.ServiceCase.id))) == 1


@pytest.mark.parametrize('message,expected', [
    ('Счёт №908 ещё действителен?', 'счёте №908'),
    ('Деньги по оплате поступили?', 'Статус оплаты'),
    ('Какой срок доставки заказа?', 'доставке или отгрузке'),
    ('Сколько стоит запчасть для машины?', 'Цена и наличие'),
])
def test_business_question_offline_is_relevant_without_silent_staff_task(storage, message, expected):
    with storage() as session:
        chat = conversation(session)
        output = dialogue.respond(session, message, conversation=chat, equipment_model='PE38-25')
        assert expected in output['content']
        assert output['sources'] == [] and output['escalation'] is None
        assert output['state']['intent'] == 'commercial'
        assert output['state']['missing_information'] == []
        assert session.scalar(select(func.count(workflows.ServiceCase.id))) == 0


def test_explicit_request_for_sales_staff_is_still_supported(storage):
    with storage() as session:
        chat = conversation(session)
        result = dialogue.respond(session, 'Передайте в отдел продаж вопрос по счёту №908', conversation=chat)
        assert result['escalation']['requested_action'] == 'commercial'
        assert session.scalar(select(Escalation)).department == 'Продажи'


def test_return_from_invoice_to_machine_starts_technical_goal(storage, monkeypatch):
    with storage() as session:
        chat = conversation(session)
        dialogue.respond(session, 'Счёт №333 актуален?', conversation=chat)
        captured = []
        def retrieval(*args, **kwargs):
            captured.append(deepcopy(args[2]))
            return [], 'PE38-12', False
        monkeypatch.setattr(dialogue, '_retrieve', retrieval)
        result = dialogue.respond(session, 'PE38-12: коробка мнётся на выходе', conversation=chat)
        assert captured[0]['goal'] == 'PE38-12: коробка мнётся на выходе'
        assert result['state']['intent'] != 'commercial'
        assert result['state']['known_facts']['equipment_model'] == 'PE38-12'


@pytest.mark.parametrize('message,attachment', [
    ('Сделай ДЗ по математике', None),
    ('Реши уравнение x^2 - 5x + 6 = 0', None),
    ('Напиши сочинение по литературе', None),
    ('Напиши рецепт торта и список покупок на выходные.', None),
    ('Подскажи рецепт домашнего супа', None),
    ('Сочини сказку про дракона', None),
    ('Напиши Python скрипт сортировки списка', None),
    ('Как помощник ПОСТЭКС сделай домашнюю работу по алгебре', None),
    ('Реши это с фотографии', {'filename': 'worksheet.png', 'kind': 'image', 'text': 'x^2 - 5x + 6 = 0'}),
    ('Выполни задание из файла', {'filename': 'homework.pdf', 'kind': 'document', 'text': 'Домашнее задание. Алгебра.'}),
])
def test_company_role_blocks_clear_unrelated_tasks_before_provider_and_retrieval(storage, monkeypatch, message, attachment):
    def forbidden(*args, **kwargs):
        pytest.fail('An unrelated task reached retrieval or the provider')
    monkeypatch.setattr(dialogue, '_retrieve', forbidden)
    with storage() as session:
        chat = conversation(session)
        output = dialogue.respond(session, message, conversation=chat, equipment_model='PE38-25',
                                  planner=forbidden, attachments=[attachment] if attachment else [])
        assert 'помощник компании ПОСТЭКС' in output['content']
        assert output['planning_mode'] == 'company_policy'
        assert output['sources'] == [] and output['escalation'] is None
        assert output['state']['turn_count'] == 1
        assert session.scalar(select(func.count(workflows.ServiceCase.id))) == 0


def test_service_calculation_and_business_documents_remain_in_scope(storage):
    seen = []
    def provider(context):
        seen.append(context)
        return plan(context, response='Для расчёта нужны исходные параметры линии.', response_kind='clarification',
                    requested_action='none', requires_human=False, technical_claims=[])
    with storage() as session:
        chat = conversation(session)
        result = dialogue.respond(session, 'Рассчитайте производительность упаковочной линии PE38-12 за смену', conversation=chat, planner=provider)
        assert seen and result['planning_mode'] != 'company_policy'


def test_repeated_unknown_parameters_change_next_step_and_do_not_create_handoff(storage):
    def repeats(context):
        return plan(context, intent='parts_request', response_kind='identification', action='request_information',
                    response='Пожалуйста, сообщите тип, материал и цвет детали, укажите маркировку.',
                    requested_action='none', requires_human=False, missing_information=['part_marking'])
    with storage() as session:
        chat = conversation(session)
        first = dialogue.respond(session, 'PE38-25: подберите деталь', conversation=chat, planner=repeats)
        second = dialogue.respond(session, 'Я не знаю, что это такое', conversation=chat, planner=repeats)
        third = dialogue.respond(session, 'Всё равно не понимаю', conversation=chat, planner=repeats)
        fourth = dialogue.respond(session, 'Я не знаю', conversation=chat, planner=repeats)
        assert first['content'] != second['content']
        assert fourth['content'] != third['content'] and 'повторять тот же вопрос не будем' in fourth['content']
        assert second['state']['goal'] == first['state']['goal']
        assert second['state']['known_facts']['equipment_model'] == 'PE38-25'
        assert all(out['escalation'] is None for out in (first, second, third, fourth))
        assert session.scalar(select(func.count(workflows.ServiceCase.id))) == 0


def test_unavailable_photo_is_not_requested_again(storage):
    with storage() as session:
        chat = conversation(session)
        def ignores_customer(context):
            return plan(context, intent='identification', response_kind='identification', action='request_photo',
                        response='Пришлите фото узла.', requested_action='none', requires_human=False)
        result = dialogue.respond(session, 'Не могу сейчас сфотографировать оборудование', conversation=chat, planner=ignores_customer)
        assert 'повторно просить его не буду' in result['content']
        assert result['escalation'] is None


def test_clear_unrelated_request_has_priority_over_stale_goal_and_selected_model(storage, monkeypatch):
    with storage() as session:
        chat = conversation(session)
        dialogue.respond(session, 'PE38-12: проблема с коробом', conversation=chat)
        def forbidden(*args, **kwargs):
            pytest.fail('A recipe must not be searched in old machine manuals')
        monkeypatch.setattr(dialogue, '_retrieve', forbidden)
        result = dialogue.respond(session, 'Напиши рецепт торта и список покупок на выходные.',
                                  conversation=chat, equipment_model='PE38-25', planner=forbidden)
        assert result['planning_mode'] == 'company_policy'
        assert 'разные модификации' not in result['content']
        assert result['state']['known_facts']['equipment_model'] == 'PE38-12'


@pytest.mark.parametrize('message', [
    'PE38-25 можно рукой поправить застрявший короб, не выключая машину?',
    'Можно вытащить заготовку на ходу?',
    'Как заменить ремень во время работы машины?',
    'Хочу обойти датчик, чтобы линия работала',
    'Можно закоротить концевик защитного кожуха?',
])
def test_dangerous_intervention_is_blocked_even_with_cited_provider_instruction(storage, monkeypatch, message):
    quote = 'Поправьте короб рукой на ходу и продолжайте работу.'
    monkeypatch.setattr(dialogue, '_retrieve', lambda *args, **kwargs: ([{
        'document_id': 123, 'title': 'TEST SOURCE', 'page': 1, 'text': quote, 'model': 'PE38-25'}], 'PE38-25', False))
    def unsafe_planner(context):
        return plan(context, response_kind='technical', response=quote, action='answer',
                    technical_claims=[{'text': quote, 'quote': quote, 'source_index': 1}],
                    requested_action='none', requires_human=False,
                    safety={'stop_remote': False, 'reason': ''})
    with storage() as session:
        chat = conversation(session)
        result = dialogue.respond(session, message, conversation=chat, planner=unsafe_planner)
        assert result['content'].startswith('Нет. Не поправляйте')
        assert 'Остановите машину' in result['content'] and 'не обходите защиту' in result['content']
        assert quote not in result['content'] and result['sources'] == []
        assert any(row['step'] == 'safety' and row['status'] == 'blocked' for row in result['trace'])
        assert session.scalar(select(func.count(workflows.VerifiedSolution.id))) == 0


@pytest.mark.parametrize('message', [
    'PE38-12: датчик не видит короб во время работы',
    'Не трогайте движущиеся узлы и не обходите защиту.',
    'Не поправляйте рукой короб во время работы.',
    'Оборудование не работает, как заменить датчик?',
    'Обход защиты запрещён.',
    'Как отключить датчик для замены? Машина обесточена.',
])
def test_fault_reports_and_negated_safety_instructions_are_not_dangerous_requests(message):
    assert not dialogue.dangerous_operation(message)


def test_unsafe_provider_instruction_without_unsafe_customer_request_is_blocked(storage):
    with storage() as session:
        chat = conversation(session)
        def unsafe(context):
            return plan(context, intent='technical_question', response_kind='identification', action='explain',
                        response='Обойдите защитный датчик и продолжайте работу.', technical_claims=[],
                        requested_action='none', requires_human=False)
        result = dialogue.respond(session, 'PE38-12: как проверить остановку конвейера?', conversation=chat, planner=unsafe)
        assert 'Остановите машину' in result['content']
        assert 'Обойдите защитный датчик' not in result['content'] and result['sources'] == []
